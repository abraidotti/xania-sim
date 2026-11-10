"""The two contracts other tools build on: ``Outcome`` (what one activity did) and ``TrialResult`` (what a whole run
found and measured).

``TrialResult.to_json()`` is what ``--json`` prints, and ``exit_code()`` is the process's exit status:

- **0**: the trial ran and found nothing that needs attention;
- **1**: it ran and found something new (a finding whose status isn't ``known`` or ``ignored``);
- **2**: it couldn't run (no build, the MUD wouldn't start, setup failed, an error in the sim).

Known findings (``python -m xania_sim findings SIG --status known``) are still listed but don't fail a run, so a
pipeline only stops on something it hasn't seen before. ``SCHEMA`` changes when the JSON's shape does.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field

SCHEMA = 1


@dataclass
class Outcome:
    """What one activity did: whether it worked, a one-line summary, and the game's own words as evidence."""

    activity: str
    ok: bool | None = None  # None: not applicable / undecided
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)


@dataclass
class TrialResult:
    trial: str  # the command: smoke, hunt, churn, stage, level, ...
    title: str
    ran: bool = True
    error: str = ""
    started: float = field(default_factory=time.time)
    seconds: float = 0.0
    commit: str = ""  # the Xania checkout's commit, and whether it had uncommitted changes
    dirty: bool = False
    run_id: str = ""  # the run in the lore database ("" when not recorded)
    measurements: dict = field(default_factory=dict)
    findings: list[dict] = field(default_factory=list)  # signature, kind, title, severity, status, first_time, repro
    outcomes: list[Outcome] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)  # name -> path (transcripts, crash reports, ...)

    @property
    def failing(self) -> list[dict]:
        return [f for f in self.findings if f["status"] == "new"]

    def exit_code(self) -> int:
        if not self.ran:
            return 2
        return 1 if self.failing else 0

    def to_dict(self) -> dict:
        return {"schema": SCHEMA, **asdict(self), "exit_code": self.exit_code()}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=1, default=str)

    def text(self) -> str:
        head = f"{self.trial}: {self.title}"
        if not self.ran:
            return f"{head}\nCOULDN'T RUN: {self.error}"
        lines = [head + f" ({self.seconds:.0f} s" + (f", Xania {self.commit[:10]}" if self.commit else "")
                 + (" dirty" if self.dirty else "") + ")"]
        if self.measurements:
            lines.append("  " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in self.measurements.items()
                                          if not isinstance(v, (dict, list))))
        for o in self.outcomes:
            if o.ok is False:
                lines.append(f"  failed: {o.activity}: {o.summary}")
        for f in self.findings:
            lines.append(f"  [{f['status']}] {f['severity']} {f['title']}" + ("  (first time)" if f["first_time"] else ""))
        for name, path in self.artifacts.items():
            lines.append(f"  {name}: {path}")
        lines.append("  result: " + {0: "clean", 1: f"{len(self.failing)} new finding(s)", 2: "couldn't run"}
                     [self.exit_code()])
        return "\n".join(lines)


def add_findings(result: TrialResult, found: list, record: bool = True) -> TrialResult:
    """Attach findings (``findings.Finding``) to the result; with ``record``, also store the run and its findings in
    the lore database, which tells each finding's status (new, known, ignored) and whether it's the first time."""
    if not record:  # still read what the database knows (read-only): a known crash isn't "new" just unrecorded
        known = known_statuses([f.signature for f in found])
        result.findings += [_ref(f, *known.get(f.signature, ("new", True))) for f in found]
        return result
    from xania_sim.record.findings import Journal
    from xania_sim.record.store import Lore

    lore = Lore()
    try:
        j = Journal(lore.db)
        result.run_id = j.start_run(result.trial, result.title, result.commit, result.dirty)
        for f in found:
            first = j.record(result.run_id, f, result.commit)
            row = j.db.execute("SELECT status FROM findings WHERE signature = ?", (f.signature,)).fetchone()
            result.findings.append(_ref(f, row[0] if row else "new", first))
        summary = {k: v for k, v in result.measurements.items() if isinstance(v, (int, float, str, bool))}
        j.finish_run(result.run_id, **summary, findings=len(found))
    finally:
        lore.close()
    return result


def known_statuses(signatures: list[str]) -> dict[str, tuple[str, bool]]:
    """{signature: (status, first time)} for findings already in the lore database, opened read-only (no
    database, or an unreadable one: {})."""
    import sqlite3

    from xania_sim.record.store import DEFAULT_PATH

    if not signatures or not DEFAULT_PATH.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{DEFAULT_PATH.as_posix()}?mode=ro", uri=True)
        try:
            marks = ",".join("?" * len(signatures))
            rows = con.execute(f"SELECT signature, status FROM findings WHERE signature IN ({marks})", signatures)
            return {sig: (status, False) for sig, status in rows}
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def _ref(f, status: str, first: bool) -> dict:
    """A finding as the JSON lists it, with its summary (crash call chains, BUG lines) so the evidence survives even
    when the run isn't recorded and the test MUD's logs are gone."""
    return {"signature": f.signature, "kind": f.kind, "title": f.title, "severity": f.severity, "status": status,
            "first_time": first, "repro": f.repro, "summary": f.summary}


def game_version(result: TrialResult) -> TrialResult:
    """Fill in the Xania checkout's commit (left empty if the checkout can't be read)."""
    try:
        from xania_sim.core.repo import GameRepo

        v = GameRepo().version()
        result.commit, result.dirty = v.commit, v.dirty
    except Exception:
        pass
    return result
