"""Findings: the interesting things runs turn up (crashes, xania's own BUG lines, and later surprises), grouped so
the same problem seen ten times is one finding with ten occurrences, and tied to the code version it was seen on.

Stored in the lore database next to the skills/spells lore (tables ``runs``, ``findings``, ``occurrences``).
A crash is identified by its signal and the first frame in Xania's own source (``do_get``), so two ways of
triggering the same faulty function group together; each occurrence keeps its own backtrace, line numbers and
transcript excerpts. Each finding has a status (new, known, ignored) and an optional link (e.g. a GitHub issue).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, started REAL NOT NULL, ended REAL,
    commit_hash TEXT NOT NULL DEFAULT '', dirty INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS findings (
    signature TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, severity TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '', repro TEXT NOT NULL DEFAULT '', first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    count INTEGER NOT NULL DEFAULT 0, first_commit TEXT NOT NULL DEFAULT '', last_commit TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new', link TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS occurrences (
    id INTEGER PRIMARY KEY, signature TEXT NOT NULL, run_id TEXT NOT NULL, at REAL NOT NULL,
    commit_hash TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '{}');
"""
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
STATUSES = ("new", "known", "ignored")

_FRAME = re.compile(r"^#(?P<n>\d+)\s+(?:0x[0-9a-f]+ in )?(?P<func>.+?) \(.*\) at (?P<path>\S+):(?P<line>\d+)\s*$", re.M)
_SIGNAL = re.compile(r"Program (?:received|terminated with) signal (SIG\w+)")
_ASAN = re.compile(r"ERROR: AddressSanitizer: (?P<kind>[\w-]+)")
_ASAN_FRAME = re.compile(r"^\s*#(?P<n>\d+) 0x[0-9a-f]+ in (?P<func>.+?) (?P<path>/\S+):(?P<line>\d+)(?::\d+)?\s*$", re.M)
_BUG = re.compile(r"\[\*\*\*\*\*\] BUG: (?P<msg>.*)$", re.M)


@dataclass
class Finding:
    kind: str  # crash, xania-bug, ...
    signature: str
    title: str
    severity: str  # high, medium, low
    summary: str = ""
    repro: str = ""
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Frame:
    func: str
    file: str  # path under src/, e.g. act_obj.cpp
    line: int


def project_frames(report: str) -> list[Frame]:
    """Backtrace frames in Xania's own source (src/), innermost first; library and std:: frames are skipped."""
    out = []
    for m in _FRAME.finditer(report):
        path = m.group("path")
        if "/src/" not in path or path.startswith("/usr/"):
            continue
        out.append(Frame(m.group("func"), path.split("/src/", 1)[1], int(m.group("line"))))
    return out


def asan_stacks(report: str) -> list[list[Frame]]:
    """The stacks of an AddressSanitizer report (the bad access, then "freed by", then "previously allocated by"),
    each as frames in Xania's own source, innermost first."""
    stacks: list[list[Frame]] = []
    for block in re.split(r"\n(?=freed by thread|previously allocated by thread)", report):
        frames = [Frame(m.group("func").split("(")[0].strip(), m.group("path").split("/src/", 1)[1], int(m.group("line")))
                  for m in _ASAN_FRAME.finditer(block) if "/src/" in m.group("path") and not m.group("path").startswith("/usr/")]
        stacks.append(frames)
    return stacks


def _asan_finding(report: str, kind: str, repro: str, context: str, transcripts) -> Finding:
    stacks = asan_stacks(report)
    access = stacks[0] if stacks else []
    if access:
        top = access[0]
        signature, where = f"crash:asan-{kind}:{top.file}:{top.func}", f"{top.func} ({top.file})"
    else:
        signature, where = f"crash:asan-{kind}:{hashlib.sha1(report[-400:].encode()).hexdigest()[:8]}", "unknown code"
    names = ["Access", "Freed by", "Allocated by"]
    chains = [f"{names[i] if i < 3 else 'Stack'}: " + " <- ".join(f"{f.func} ({f.file}:{f.line})" for f in st[:6])
              for i, st in enumerate(stacks) if st]
    return Finding("crash", signature, f"xania crashed (AddressSanitizer: {kind}) in {where}", "high",
                   "\n".join(chains), repro,
                   {"backtrace": report, "frames": [f.__dict__ for f in access[:12]], "context": context,
                    "transcripts": transcripts or {}})


def crash_finding(report: str, repro: str = "", context: str = "", transcripts: dict[str, str] | None = None) -> Finding:
    """A Finding for a crash report (TestMud.crash_report): a gdb backtrace, an assertion, or an AddressSanitizer
    report (identified by its kind and the first frame in Xania's source)."""
    if (asan := _ASAN.search(report)) is not None:
        return _asan_finding(report, asan.group("kind"), repro, context, transcripts)
    sig_m = _SIGNAL.search(report)
    signal = sig_m.group(1) if sig_m else "unknown signal"
    frames = project_frames(report)
    assertion = next((ln.strip() for ln in report.splitlines() if "Assertion" in ln), "")
    if frames:
        top = frames[0]
        where = f"{top.func} ({top.file})"  # lines vary between triggers; each occurrence keeps its own
        signature = f"crash:{signal}:{top.file}:{top.func}"
    else:
        digest = hashlib.sha1(report[-400:].encode()).hexdigest()[:8]
        where, signature = "unknown code", f"crash:{signal}:{digest}"
    title = f"xania crashed ({signal}{', failed assertion' if assertion else ''}) in {where}"
    chain = " <- ".join(f"{f.func} ({f.file}:{f.line})" for f in frames[:6])
    summary = (f"{assertion}\n\nCall chain: {chain}" if assertion else f"Call chain: {chain}").strip()
    return Finding("crash", signature, title, "high", summary, repro,
                   {"backtrace": report, "frames": [f.__dict__ for f in frames[:12]], "context": context,
                    "transcripts": transcripts or {}})


def bug_line_findings(log: str, repro: str = "") -> list[Finding]:
    """xania's own ``[*****] BUG:`` log lines (Logger::bug): one finding per message, numbers ignored."""
    out: dict[str, Finding] = {}
    for m in _BUG.finditer(log):
        msg = m.group("msg").strip()
        key = re.sub(r"\d+", "#", msg)
        f = out.setdefault(key, Finding("xania-bug", f"bug:{key}", f"xania logged a BUG: {msg[:90]}", "medium",
                                        msg, repro, {"lines": []}))
        f.evidence["lines"].append(msg)
    return list(out.values())


def transcript_tails(run_dir: Path, lines: int = 30, newest: int = 2) -> dict[str, str]:
    """The last lines of the most recently written bot transcripts in a run directory."""
    if not run_dir.is_dir():
        return {}
    logs = sorted(run_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:newest]
    return {p.stem: "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]) for p in logs}


class Journal:
    """Runs and findings, in the lore database."""

    def __init__(self, db, clock=time.time):
        self.db = db
        self.db.executescript(SCHEMA)
        self.clock = clock

    # -- runs ----------------------------------------------------------------------------------------
    def start_run(self, kind: str, title: str, commit: str = "", dirty: bool = False) -> str:
        now = self.clock()
        run_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{kind}"
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO runs (id, kind, title, started, commit_hash, dirty) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (run_id, kind, title, now, commit, int(dirty)))
        return run_id

    def finish_run(self, run_id: str, **summary) -> None:
        with self.db:
            self.db.execute("UPDATE runs SET ended = ?, summary = ? WHERE id = ?",
                            (self.clock(), json.dumps(summary), run_id))

    def runs(self, limit: int = 50) -> list[dict]:
        cur = self.db.execute("SELECT id, kind, title, started, ended, commit_hash, dirty, summary FROM runs "
                              "ORDER BY started DESC LIMIT ?", (limit,))
        keys = ["id", "kind", "title", "started", "ended", "commit", "dirty", "summary"]
        out = [dict(zip(keys, row)) for row in cur]
        for r in out:
            r["summary"] = json.loads(r["summary"])
            r["findings"] = [s for (s,) in self.db.execute(
                "SELECT DISTINCT signature FROM occurrences WHERE run_id = ?", (r["id"],))]
        return out

    # -- findings --------------------------------------------------------------------------------------
    def record(self, run_id: str, f: Finding, commit: str = "") -> bool:
        """Add an occurrence. True if the finding is new."""
        now = self.clock()
        with self.db:
            new = self.db.execute("SELECT 1 FROM findings WHERE signature = ?", (f.signature,)).fetchone() is None
            if new:
                self.db.execute("INSERT INTO findings (signature, kind, title, severity, summary, repro, first_seen, "
                                "last_seen, count, first_commit, last_commit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                                (f.signature, f.kind, f.title, f.severity, f.summary, f.repro, now, now, commit, commit))
            else:
                self.db.execute("UPDATE findings SET last_seen = ?, count = count + 1, last_commit = ?, title = ?, "
                                "repro = CASE WHEN repro = '' THEN ? ELSE repro END WHERE signature = ?",
                                (now, commit, f.title, f.repro, f.signature))
            self.db.execute("INSERT INTO occurrences (signature, run_id, at, commit_hash, evidence) VALUES (?, ?, ?, ?, ?)",
                            (f.signature, run_id, now, commit, json.dumps(f.evidence | {"title": f.title})))
        return new

    def findings(self) -> list[dict]:
        keys = ["signature", "kind", "title", "severity", "summary", "repro", "first_seen", "last_seen", "count",
                "first_commit", "last_commit", "status", "link"]
        rows = [dict(zip(keys, r)) for r in self.db.execute(f"SELECT {', '.join(keys)} FROM findings")]
        return sorted(rows, key=lambda r: (r["status"] != "new", SEVERITY_ORDER.get(r["severity"], 9), -r["last_seen"]))

    def occurrences(self, signature: str, limit: int = 20) -> list[dict]:
        cur = self.db.execute("SELECT run_id, at, commit_hash, evidence FROM occurrences WHERE signature = ? "
                              "ORDER BY at DESC LIMIT ?", (signature, limit))
        return [{"run": r, "at": a, "commit": c, "evidence": json.loads(e)} for r, a, c, e in cur]

    def find(self, prefix: str) -> str:
        """The signature matching a prefix or a substring (for the CLI)."""
        sigs = [s for (s,) in self.db.execute("SELECT signature FROM findings")]
        hits = [s for s in sigs if s.startswith(prefix)] or [s for s in sigs if prefix in s]
        if len(hits) != 1:
            raise KeyError(f"{len(hits)} findings match {prefix!r}: {hits[:5]}")
        return hits[0]

    def set_status(self, signature: str, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        with self.db:
            self.db.execute("UPDATE findings SET status = ? WHERE signature = ?", (status, signature))

    def set_link(self, signature: str, link: str) -> None:
        with self.db:
            self.db.execute("UPDATE findings SET link = ? WHERE signature = ?", (link, signature))
