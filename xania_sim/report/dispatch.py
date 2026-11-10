"""The dispatch: a static website of what runs found, to read and share.

``build(journal, out_dir)`` writes:
- ``index.html``: headlines, finding cards (new first, then by severity), and the run log;
- ``findings/<id>.html``: one page per finding (a stable link to share): what happened, how to reproduce it, a
  link to its issue, and every occurrence with its backtrace and the bots' transcript excerpts;
- ``dispatch.json``: the same data, for other tools.

Pages are plain HTML with inline CSS (light and dark), no scripts. Bots' progress pages come from the event log;
stories join later. ``publish`` pushes the site to the repo's ``dispatch`` branch and asks GitHub to deploy it to Pages.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from xania_sim.record.findings import Journal

TITLE = "Xania Sim dispatch"
WORKFLOW = "dispatch.yml"
CSS = """
:root { --bg: #fbfaf7; --fg: #1d1d1b; --muted: #6b6a66; --card: #ffffff; --line: #e4e1da; --accent: #8a3b12;
        --high: #b42318; --medium: #b54708; --low: #475467; --code: #f3f1ec; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #161513; --fg: #ecebe7; --muted: #a3a19b; --card: #1f1e1b; --line: #34322d; --accent: #f0a46b;
          --high: #f97066; --medium: #fdb022; --low: #98a2b3; --code: #26241f; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 860px; margin: 0 auto; padding: 32px 16px 64px; }
a { color: var(--accent); }
h1 { font-size: 1.9rem; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 1.15rem; margin: 36px 0 12px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.sub { color: var(--muted); margin: 0 0 8px; }
.headlines li { margin: 4px 0; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; margin: 12px 0; }
.card h3 { margin: 0 0 6px; font-size: 1.05rem; }
.meta { color: var(--muted); font-size: 0.88rem; }
.badge { display: inline-block; font-size: 0.75rem; font-weight: 600; padding: 1px 8px; border-radius: 999px;
         border: 1px solid currentColor; margin-right: 6px; text-transform: uppercase; letter-spacing: 0.04em; }
.high { color: var(--high); } .medium { color: var(--medium); } .low { color: var(--low); }
.status-new { color: var(--accent); }
pre { background: var(--code); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px;
      overflow-x: auto; font-size: 0.8rem; line-height: 1.45; }
code { background: var(--code); padding: 1px 5px; border-radius: 4px; font-size: 0.9em; }
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
.scroll { overflow-x: auto; }
details { margin: 8px 0; } summary { cursor: pointer; color: var(--muted); }
footer { margin-top: 48px; color: var(--muted); font-size: 0.85rem; }
.chart { width: 100%; height: auto; display: block; }
.chart .grid { stroke: var(--line); stroke-width: 1; }
.chart .axis { fill: var(--muted); font-size: 11px; }
.chart .line { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linejoin: round; }
.chart .dot { fill: var(--accent); stroke: var(--card); stroke-width: 2; }
.chart .hit { fill: transparent; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.journal p { margin: 4px 0; }
"""


def slug(signature: str) -> str:
    return hashlib.sha1(signature.encode()).hexdigest()[:10]


def _when(t: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""


_HOME = re.compile(r"/home/[^/\s]+/")


def _e(text) -> str:
    """HTML-escaped, with home directories shortened to ~/ (backtraces carry the builder's username)."""
    return html.escape(_HOME.sub("~/", str(text)))


def _page(title: str, body: str, root: str = "") -> str:
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{_e(title)}</title><style>{CSS}</style></head><body><main>{body}"
            f'<footer>Made by <a href="https://github.com/abraidotti/xania-sim">xania-sim</a>: bots playing '
            f'<a href="https://github.com/mattgodbolt/xania">Xania</a>. <a href="{root}index.html">Dispatch home</a>.'
            f"</footer></main></body></html>\n")


def _commit(c: str) -> str:
    return f"<code>{_e(c[:10])}</code>" if c else "unknown version"


def _card(f: dict, root: str = "") -> str:
    link = f' · <a href="{_e(f["link"])}">issue</a>' if f["link"] else ""
    seen = f'seen {f["count"]}×, first {_when(f["first_seen"])}, last {_when(f["last_seen"])} on {_commit(f["last_commit"])}'
    return (f'<div class="card"><h3><a href="{root}findings/{slug(f["signature"])}.html">{_e(f["title"])}</a></h3>'
            f'<div class="meta"><span class="badge {f["severity"]}">{f["severity"]}</span>'
            f'<span class="badge status-{f["status"]}">{f["status"]}</span>{seen}{link}</div>'
            + (f'<p>{_e(f["summary"].splitlines()[0])}</p>' if f["summary"] else "") + "</div>")


def _headlines(findings: list[dict], runs: list[dict]) -> list[str]:
    out = []
    fresh = [f for f in findings if f["status"] == "new"]
    for f in fresh[:5]:
        out.append(f'<a href="findings/{slug(f["signature"])}.html">{_e(f["title"])}</a>'
                   + (f' (seen {f["count"]}×)' if f["count"] > 1 else ""))
    if runs:
        last = runs[0]
        out.append(f'Latest run: {_e(last["title"])}, {_when(last["started"])}'
                   + (f' — {_e(_summary_text(last["summary"]))}' if last["summary"] else ""))
    if not fresh:
        out.append("No new findings.")
    return out


def _summary_text(s: dict) -> str:
    return ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in s.items() if not isinstance(v, (dict, list)))


def _bots_table(bots, root: str = "") -> str:
    if not bots:
        return "<p>No bots have played yet.</p>"
    rows = "".join(
        f"<tr><td><a href='{root}bots/{_e(b.name.lower())}.html'>{_e(b.name)}</a></td><td>{_e(b.race)} {_e(b.cls)}</td>"
        f"<td class='num'>{b.level}</td><td class='num'>{b.kills}</td><td class='num'>{b.deaths}</td>"
        f"<td class='num'>{b.flees}</td><td class='num'>{b.hours:.1f}</td><td>{_when(b.last_seen)}</td></tr>"
        for b in bots)
    return ("<div class='scroll'><table><tr><th>Bot</th><th>Who</th><th class='num'>Level</th><th class='num'>Kills</th>"
            "<th class='num'>Deaths</th><th class='num'>Flees</th><th class='num'>Hours</th><th>Last seen</th></tr>"
            f"{rows}</table></div>")


def level_chart(points: list[tuple[float, int]], width: int = 760, height: int = 220) -> str:
    """Level over time as a step line (levels are whole steps), inline SVG; each level has a hover title."""
    if not points:
        return "<p>No snapshots yet.</p>"
    left, right, top, bottom = 34, 12, 10, 26
    t0, t1 = points[0][0], max(points[-1][0], points[0][0] + 60)
    lmax = max(2, max(lv for _, lv in points) + 1)
    w, h = width - left - right, height - top - bottom

    def x(t):
        return left + (t - t0) / (t1 - t0) * w

    def y(lv):
        return top + h - lv / lmax * h

    step = max(1, lmax // 5)
    grid = "".join(f"<line class='grid' x1='{left}' x2='{width - right}' y1='{y(v):.1f}' y2='{y(v):.1f}'/>"
                   f"<text class='axis' x='{left - 6}' y='{y(v) + 4:.1f}' text-anchor='end'>{v}</text>"
                   for v in range(0, lmax + 1, step))
    span = t1 - t0
    unit, div = ("h", 3600) if span >= 3 * 3600 else ("min", 60)
    ticks = "".join(f"<text class='axis' x='{x(t0 + i * span / 4):.1f}' y='{height - 6}' text-anchor='middle'>"
                    f"{i * span / 4 / div:.0f} {unit}</text>" for i in range(5))
    d = f"M{x(points[0][0]):.1f},{y(points[0][1]):.1f}"
    for t, lv in points[1:]:
        d += f" H{x(t):.1f} V{y(lv):.1f}"
    d += f" H{x(t1):.1f}"
    dots = "".join(f"<g><circle class='hit' cx='{x(t):.1f}' cy='{y(lv):.1f}' r='10'/>"
                   f"<circle class='dot' cx='{x(t):.1f}' cy='{y(lv):.1f}' r='4'/>"
                   f"<title>level {lv} at {_when(t)}</title></g>" for t, lv in points)
    return (f"<svg class='chart' viewBox='0 0 {width} {height}' role='img' aria-label='Level over time'>"
            f"{grid}{ticks}<path class='line' d='{d}'/>{dots}</svg>")


_EVENT_TEXT = {
    "kill": lambda d: f"killed {d.get('victim', '?')}",
    "died": lambda d: "was killed" + (f" by {d['killer']}" if d.get("killer") else ""),
    "level": lambda d: f"reached level {d.get('level', '?')}",
    "fled": lambda d: "fled a fight",
    "loot": lambda d: f"took {d.get('item', '?')} from {d.get('of', '?')}",
    "tell": lambda d: f"heard from {d.get('from', '?')}: \u201c{d.get('text', '')}\u201d",
    "group": lambda d: f"grouped with {d.get('leader') if d.get('who') == 'you' else d.get('who')}",
}


def _bot_page(b, progress, comparisons) -> str:
    parts = [f"<p class='sub'><a href='../index.html'>{TITLE}</a></p><h1>{_e(b.name)}</h1>",
             f"<p class='sub'>{_e(b.race)} {_e(b.cls)}, level {b.level}. {b.kills} kills, {b.deaths} deaths, "
             f"{b.flees} flees in {b.hours:.1f} hours played. First seen {_when(b.created)}.</p>"]
    parts += ["<h2>Level over time</h2>", level_chart(progress.levels(b.name))]
    areas = progress.by_area(b.name)
    if areas:
        parts.append("<h2>Where</h2><div class='scroll'><table><tr><th>Area</th><th class='num'>Kills</th>"
                     "<th class='num'>Deaths</th><th class='num'>XP</th></tr>"
                     + "".join(f"<tr><td>{_e(a)}</td><td class='num'>{k}</td><td class='num'>{d}</td>"
                               f"<td class='num'>{xp}</td></tr>" for a, k, d, xp in areas) + "</table></div>")
    events = progress.events(b.name, tuple(_EVENT_TEXT), limit=40)
    if events:
        parts.append("<h2>Recently</h2><ul>" + "".join(
            f"<li><span class='meta'>{_when(e['at'])}{' · ' + _e(e['area']) if e['area'] else ''}</span> "
            f"{_e(_EVENT_TEXT[e['kind']](e['data']))}</li>" for e in events) + "</ul>")
    mine = [c for c in comparisons if c.cls == b.cls]
    if mine:
        parts.append(_comparison_table(mine))
    return _page(b.name, "".join(parts), "../")


def _pct(v) -> str:
    return "" if v is None else f"{v:.0%}"


def _comparison_table(rows) -> str:
    body = "".join(
        f"<tr><td>{_e(c.name)}</td><td>{_e(c.cls)} {c.level}</td><td class='num'>{c.fights}</td>"
        f"<td class='num'>{_pct(c.won)}</td><td class='num'>{_pct(c.lost_share)}</td>"
        f"<td class='num'>{_pct(c.predicted_lost_share)}{' (deadly)' if c.predicted_deadly else ''}</td>"
        f"<td class='num'>{c.xp:.0f}</td><td class='num'>{'' if c.predicted_xp is None else f'{c.predicted_xp:.0f}'}"
        "</td></tr>" for c in rows)
    return ("<h2>Fights: predicted vs seen</h2><p class='sub'>Explorer's prediction for each mob beside what bots "
            "saw. HP lost is a share of the bot's maximum.</p><div class='scroll'><table><tr><th>Mob</th><th>Bot</th>"
            "<th class='num'>Fights</th><th class='num'>Won</th><th class='num'>HP lost</th>"
            "<th class='num'>predicted</th><th class='num'>XP</th><th class='num'>predicted</th></tr>"
            f"{body}</table></div>")


def _index(findings: list[dict], runs: list[dict], generated: float, bots=(), comparisons=()) -> str:
    rows = "".join(
        f"<tr><td>{_when(r['started'])}</td><td>{_e(r['kind'])}</td><td>{_e(r['title'])}</td>"
        f"<td>{_commit(r['commit'])}{' (dirty)' if r['dirty'] else ''}</td><td>{_e(_summary_text(r['summary']))}</td>"
        f"<td>{''.join(f'<a href=\"findings/{slug(s)}.html\">●</a> ' for s in r['findings'])}</td></tr>"
        for r in runs)
    body = (f"<h1>{TITLE}</h1><p class='sub'>What bots found playing Xania. Updated {_when(generated)}.</p>"
            "<h2>Headlines</h2><ul class='headlines'>" + "".join(f"<li>{h}</li>" for h in _headlines(findings, runs))
            + "</ul><h2>Bots</h2>" + _bots_table(list(bots))
            + (_comparison_table(list(comparisons)[:20]) if comparisons else "")
            + "<h2>Findings</h2>" + ("".join(_card(f) for f in findings) or "<p>None yet.</p>")
            + "<h2>Runs</h2><div class='scroll'><table><tr><th>When</th><th>Kind</th><th>Run</th><th>Game version</th>"
            f"<th>Summary</th><th>Findings</th></tr>{rows}</table></div>")
    return _page(TITLE, body)


def _finding_page(f: dict, occurrences: list[dict]) -> str:
    parts = [f"<p class='sub'><a href='../index.html'>{TITLE}</a></p><h1>{_e(f['title'])}</h1>", _card(f, "../")]
    if f["summary"]:
        parts.append(f"<h2>What happened</h2><pre>{_e(f['summary'])}</pre>")
    if f["repro"]:
        parts.append(f"<h2>Reproduce</h2><pre>{_e(f['repro'])}</pre>")
    parts.append(f"<h2>Occurrences ({f['count']})</h2>")
    for o in occurrences:
        ev = o["evidence"]
        block = [f"<div class='card'><div class='meta'>{_when(o['at'])} · run {_e(o['run'])} · {_commit(o['commit'])}</div>"]
        if ev.get("context"):
            block.append(f"<p>{_e(ev['context'])}</p>")
        if ev.get("frames"):
            block.append("<p>" + " ← ".join(f"<code>{_e(fr['func'])}</code> {_e(fr['file'])}:{fr['line']}"
                                            for fr in ev["frames"][:6]) + "</p>")
        if ev.get("backtrace"):
            block.append(f"<details><summary>Backtrace and xania's last log lines</summary><pre>{_e(ev['backtrace'])}</pre></details>")
        for name, text in (ev.get("transcripts") or {}).items():
            block.append(f"<details><summary>{_e(name)}'s transcript (last lines)</summary><pre>{_e(text)}</pre></details>")
        for line in ev.get("lines", [])[:20]:
            block.append(f"<pre>{_e(line)}</pre>")
        parts.append("".join(block) + "</div>")
    return _page(f["title"], "".join(parts), "../")


def build(journal: Journal, out_dir: Path, progress=None, comparisons=()) -> Path:
    """``progress`` (events.Progress) adds the bots table and a page per bot; ``comparisons`` (fight.compare) adds
    predicted-vs-seen fight tables. Both also go into dispatch.json."""
    from dataclasses import asdict

    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "findings").mkdir(parents=True)
    findings, runs, now = journal.findings(), journal.runs(), time.time()
    bots = progress.bots() if progress is not None else []
    comparisons = list(comparisons)
    (out_dir / "index.html").write_text(_index(findings, runs, now, bots, comparisons), encoding="utf-8")
    if bots:
        (out_dir / "bots").mkdir()
    for b in bots:
        (out_dir / "bots" / f"{b.name.lower()}.html").write_text(_bot_page(b, progress, comparisons), encoding="utf-8")
    data = {"generated": now, "findings": [], "runs": runs, "bots": [asdict(b) for b in bots],
            "fights": [asdict(c) for c in comparisons]}
    for f in findings:
        occ = journal.occurrences(f["signature"])
        (out_dir / "findings" / f"{slug(f['signature'])}.html").write_text(_finding_page(f, occ), encoding="utf-8")
        data["findings"].append(f | {"id": slug(f["signature"]), "occurrences": occ})
    (out_dir / "dispatch.json").write_text(_HOME.sub("~/", json.dumps(data, indent=1)), encoding="utf-8")
    (out_dir / ".nojekyll").write_text("")
    return out_dir


def _git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def publish(journal: Journal, repo: Path, branch: str = "dispatch", deploy: bool = True, progress=None,
            comparisons=()) -> str:
    """Build the site, commit it as the only content of ``branch`` (history kept, one commit per publish), push,
    and run the Pages workflow. Returns the pushed commit."""
    repo = Path(repo)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "site"
        _git(repo, "fetch", "-q", "origin", branch, check=False)
        exists = bool(_git(repo, "ls-remote", "--heads", "origin", branch, check=False))
        _git(repo, "worktree", "add", "-q", "--detach", str(work))
        try:
            if exists:
                _git(work, "checkout", "-q", "-B", branch, f"origin/{branch}")
            else:
                _git(work, "checkout", "-q", "--orphan", branch)
            _git(work, "rm", "-rqf", "--ignore-unmatch", ".")
            site = build(journal, Path(tmp) / "build", progress, comparisons)
            for p in site.iterdir():
                dest = work / p.name
                shutil.copytree(p, dest) if p.is_dir() else shutil.copy2(p, dest)
            _git(work, "add", "-A")
            if _git(work, "status", "--porcelain"):
                _git(work, "commit", "-q", "-m", f"Dispatch {time.strftime('%Y-%m-%d %H:%M')}")
            commit = _git(work, "rev-parse", "HEAD")
            _git(work, "push", "-q", "origin", f"HEAD:{branch}")
        finally:
            _git(repo, "worktree", "remove", "--force", str(work), check=False)
    if deploy:
        subprocess.run(["gh", "workflow", "run", WORKFLOW, "--repo", _origin_slug(repo)], check=True,
                       capture_output=True)
    return commit


def _origin_slug(repo: Path) -> str:
    url = _git(repo, "remote", "get-url", "origin")
    tail = url.split("github.com", 1)[1].lstrip(":/")
    return tail[:-4] if tail.endswith(".git") else tail
