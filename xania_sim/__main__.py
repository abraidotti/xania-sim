"""``python -m xania_sim smoke``: start a test MUD, create a bot, look around, quit."""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from xania_sim.core import world
from xania_sim.core.names import bot_names, mob_words
from xania_sim.core.server import TestMud
from xania_sim.core.session import Bot, Profile


async def smoke(port: int, existing: bool) -> None:
    runs = Path("runs") / time.strftime("%Y%m%d-%H%M%S")
    if existing:
        await _visit(port, runs)
        return
    async with TestMud(port=port) as mud:
        await _visit(mud.port, runs)


async def _visit(port: int, runs: Path) -> None:
    [name] = bot_names("smoke", 1, mob_words(world.load()))
    bot = Bot(name, "botpass1", port=port, transcript_dir=runs)
    r = await bot.enter(Profile(race="human", cls="knight"))
    print(f"{bot.name} is in ({'created' if bot.created else 'logged in'}): {r.prompt}")
    look = await bot.do("look")
    print("\n".join(look.lines[:6]))
    print((await bot.quit()).strip().splitlines()[:1])
    print(f"transcript: {bot.transcript}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="xania_sim")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("smoke", help="start a test MUD, create a bot, look, quit")
    s.add_argument("--port", type=int, default=9100)
    s.add_argument("--existing", action="store_true", help="use a MUD already running on --port")
    lo = sub.add_parser("lore", help="scan the game repo's skills and spells into the shared lore and show status")
    lo.add_argument("--db", default=None, help="lore database (default state/lore.db)")
    lo.add_argument("--level", type=int, default=1, help="show status for this level's band")
    lo.add_argument("names", nargs="*", help="abilities to show in detail")
    h = sub.add_parser("hunt", help="bots fight a mob on a test MUD under gdb until xania crashes; save the backtrace")
    h.add_argument("--mob", default="angry cave beast", help="mob to fight, matched in its short description")
    h.add_argument("--bots", type=int, default=4)
    h.add_argument("--minutes", type=float, default=10)
    h.add_argument("--port", type=int, default=9100)
    h.add_argument("--no-record", action="store_true", help="don't record the run in the lore database")
    cu = sub.add_parser("churn", help="bots create characters and quit in a loop on a test MUD until xania crashes")
    cu.add_argument("--bots", type=int, default=6)
    cu.add_argument("--minutes", type=float, default=15)
    cu.add_argument("--port", type=int, default=9100)
    cu.add_argument("--no-record", action="store_true", help="don't record a crash in the lore database")
    sub.add_parser("report", help="print the latest runs and findings")
    st = sub.add_parser("status", help="each bot's progress: level, xp, kills, deaths, time played")
    st.add_argument("bot", nargs="?", help="show this bot's recent events and areas too")
    fi = sub.add_parser("findings", help="mark a finding known/ignored/new, or link it to an issue")
    fi.add_argument("finding", help="its signature, or part of it (e.g. do_get)")
    fi.add_argument("--status", choices=["new", "known", "ignored"])
    fi.add_argument("--link", help="a URL, e.g. the GitHub issue")
    d = sub.add_parser("dispatch", help="build the dispatch website locally")
    d.add_argument("--out", default="build/dispatch")
    sub.add_parser("publish", help="build the dispatch, push it to the dispatch branch and deploy it to GitHub Pages")
    from xania_sim.players import run as play

    play.add_commands(sub)
    args = ap.parse_args()
    if args.cmd == "smoke":
        asyncio.run(smoke(args.port, args.existing))
    elif args.cmd == "lore":
        lore_cmd(args.db, args.level, args.names)
    elif args.cmd == "hunt":
        from xania_sim.trials.hunt import hunt

        r = asyncio.run(hunt(args.mob, args.bots, args.minutes, args.port, record=not args.no_record))
        print(f"{r.fights} fights in {r.seconds:.0f} s, {r.errors} bot errors")
        if r.crashed:
            print(f"XANIA CRASHED. Report with backtrace: {r.report_path}\n")
            print("\n".join(r.report.splitlines()[:40]))
        else:
            print("no crash")
        for e in r.last_errors:
            print("  bot error:", e)
    elif args.cmd in ("report", "findings", "dispatch", "publish"):
        journal_cmd(args)
    elif args.cmd == "churn":
        from xania_sim.trials.churn import churn

        r = asyncio.run(churn(args.bots, args.minutes, args.port, record=not args.no_record))
        print(f"{r.quits} quits in {r.seconds:.0f} s, {len(r.errors)} bot errors")
        if r.crashed:
            print(f"XANIA CRASHED. Report: {r.report_path}")
            print(chr(10).join(r.report.splitlines()[:30]))
        else:
            print("no crash")
    elif args.cmd == "level":
        play.run(args)
    elif args.cmd == "status":
        status_cmd(args.bot)


def _progress(lore):
    """The event log and the predicted-vs-seen fights (no comparisons if the world can't be loaded)."""
    from xania_sim.record.events import Progress
    from xania_sim.activities.fight import FightLog, compare

    try:
        comparisons = compare(world.load(), FightLog(lore.db))
    except Exception as e:  # no checkout or Explorer: still build the rest
        print(f"(no fight comparisons: {e})")
        comparisons = []
    return Progress(lore.db), comparisons


def journal_cmd(args) -> None:
    from xania_sim.report import dispatch as D
    from xania_sim.record.findings import Journal
    from xania_sim.record.store import Lore

    lore = Lore()
    journal = Journal(lore.db)
    try:
        if args.cmd == "report":
            for r in journal.runs(8):
                print(f"{D._when(r['started'])}  {r['kind']:<6} {r['title']:<36} {D._summary_text(r['summary'])}")
            print()
            for f in journal.findings():
                print(f"[{f['status']:<7}] {f['severity']:<6} {f['title']}\n          seen {f['count']}x, last "
                      f"{D._when(f['last_seen'])}{'  ' + f['link'] if f['link'] else ''}\n          {f['signature']}")
        elif args.cmd == "findings":
            sig = journal.find(args.finding)
            if args.status:
                journal.set_status(sig, args.status)
            if args.link:
                journal.set_link(sig, args.link)
            print(f"{sig}: updated")
        elif args.cmd == "dispatch":
            out = D.build(journal, Path(args.out), *_progress(lore))
            print(f"built {out / 'index.html'}")
        elif args.cmd == "publish":
            progress, comparisons = _progress(lore)
            commit = D.publish(journal, Path(__file__).resolve().parents[1], progress=progress, comparisons=comparisons)
            print(f"pushed the dispatch ({commit[:10]}) and started the Pages deployment")
    finally:
        lore.close()


def status_cmd(bot: str | None) -> None:
    from xania_sim.report import dispatch as D
    from xania_sim.record.events import Progress
    from xania_sim.record.store import Lore

    lore = Lore()
    p = Progress(lore.db)
    print(f"{'bot':<12} {'class':<8} {'lvl':>3} {'xp':>7} {'gold':>6} {'kills':>5} {'deaths':>6} {'flees':>5} "
          f"{'hours':>5}  last seen")
    for s in p.bots():
        print(f"{s.name:<12} {s.cls:<8} {s.level:>3} {s.xp:>7} {s.gold:>6} {s.kills:>5} {s.deaths:>6} {s.flees:>5} "
              f"{s.hours:>5.1f}  {D._when(s.last_seen)}")
    if bot:
        name = bot.capitalize()
        print(f"\n{name}'s areas (kills, deaths, xp):")
        for area, k, d, xp in p.by_area(name):
            print(f"  {area:<28} {k:>4} {d:>3} {xp:>7}")
        print(f"\n{name}'s recent events:")
        for e in p.events(name, ("kill", "died", "level", "fled", "loot", "tell"), limit=20):
            print(f"  {D._when(e['at'])}  {e['kind']:<6} {e['area'] or '':<20} {e['data']}")
    lore.close()


def lore_cmd(db: str | None, level: int, names: list[str]) -> None:
    from collections import Counter

    from xania_sim.core.repo import GameRepo
    from xania_sim.record.scan import scan_source
    from xania_sim.record.store import DEFAULT_PATH, Lore, band_label, band_of

    repo = GameRepo()
    v = repo.version()
    print(f"game repo {repo.linux_path} at {v.label}")
    b = repo.build_check()
    if b.stale:
        print(f"WARNING: {b.reason}. Bots would learn from source the running game doesn't have.")
    lore = Lore(db or DEFAULT_PATH)
    diff = lore.apply_scan(v.commit, v.dirty, scan_source(repo.src_dir))
    for kind in ("new", "changed", "removed"):
        items = getattr(diff, kind)
        if items:
            print(f"{kind}: {len(items)}" + (f" ({', '.join(items[:12])}{', ...' if len(items) > 12 else ''})"))
    statuses = Counter(lore.summary(n, level).status for n in lore.abilities())
    print(f"{len(lore.abilities())} abilities; at levels {band_label(band_of(level))}: "
          + ", ".join(f"{k} {statuses[k]}" for k in ("known", "testing", "stale", "unknown") if statuses[k]))
    for n in names:
        f, s = lore.facts(n), lore.summary(n, level)
        if f is None:
            print(f"{n}: not in the source")
            continue
        learn = ", ".join(f"{c} {lv}" for c, lv in f.levels.items() if lv is not None) or "no class"
        print(f"{n}: {'spell' if f.is_spell else 'skill'}, target {f.target or '-'}, position {f.position or '-'}, "
              f"mana {f.min_mana}, lag {f.beats}; learned by {learn}; fingerprint {f.fingerprint}")
        print(f"  {band_label(s.band)}: {s.status}, {s.observations} observations"
              + (f", success {s.success_rate:.0%}" if s.success_rate is not None else "")
              + (f", mean damage {s.mean_damage:.1f}" if s.mean_damage is not None else "")
              + (f", claimed by {s.claimed_by}" if s.claimed_by else ""))
    lore.close()


if __name__ == "__main__":
    main()
