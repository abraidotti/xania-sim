"""``python -m xania_sim``: trials (smoke, hunt, churn, stage, level, skill, area) and reading what they found.

Every trial takes ``--json`` (print the TrialResult as JSON on stdout; the human text goes to stderr) and
``--no-record`` (don't store the run in the lore database), and exits 0 (clean), 1 (new findings) or 2 (couldn't
run): see record/result.py. ``--xania PATH`` (before the command) points everything at another Xania checkout.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

from xania_sim.core import world


def main() -> None:
    ap = argparse.ArgumentParser(prog="xania_sim")
    ap.add_argument("--xania", help="the Xania checkout to test, as a Linux path (default $XANIA_SIM_XANIA or ~/xania)")
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
    cu = sub.add_parser("churn", help="bots create characters and quit in a loop on a test MUD until xania crashes")
    cu.add_argument("--bots", type=int, default=6)
    cu.add_argument("--minutes", type=float, default=15)
    cu.add_argument("--port", type=int, default=9100)
    sk = sub.add_parser("skill", help="test a skill or spell: practise it, use it in fights, check its messages")
    sk.add_argument("skill", help="its name, e.g. bash or 'magic missile'")
    sk.add_argument("--classes", nargs="*", help="only these classes (default: every class that learns it)")
    sk.add_argument("--levels", nargs="*", type=int, help="also test at these levels (default: the level it's learned)")
    sk.add_argument("--fights", type=int, default=4, help="fights per class and level (default 4)")
    sk.add_argument("--port", type=int, default=9100)
    ar = sub.add_parser("area", help="test an area: careful players of each class at levels around its range play it")
    ar.add_argument("area", help="its file or name, e.g. Shire.are or 'The Shire'")
    ar.add_argument("--classes", nargs="*", help="only these classes (default: all)")
    ar.add_argument("--levels", nargs="*", type=int, help="these levels instead of low-5, low, median, high, high+5")
    ar.add_argument("--minutes", type=float, default=4, help="play time per level, all classes at once (default 4)")
    ar.add_argument("--kit", default="shop", choices=["shop", "best", "best-ac", "best-hitdam", "floor", "bare"], help="gear (default shop)")
    ar.add_argument("--cut-down", action="store_true",
                    help="load only this area, Limbo, Midgaard, Mud School, helps and socials")
    ar.add_argument("--quick", action="store_true",
                    help="preflight: one class at the first level for 1.5 minutes, to check it all works first")
    ar.add_argument("--fail-fast", action="store_true", help="stop after a level where no bot won a fight")
    ar.add_argument("--port", type=int, default=9160)
    lb = sub.add_parser("lobby", help="a careless player: press Enter at each login prompt and see if xania survives")
    lb.add_argument("--prompts", nargs="*", choices=["confirm_new", "sex", "alignment", "customize", "break_connect"],
                    help="only these prompts (default: all five)")
    lb.add_argument("--port", type=int, default=9170)
    wp = sub.add_parser("weapon", help="a weapon flag in a real fight: every hit and hp change accounted for")
    wp.add_argument("--cases", nargs="*", help="VNUM@LEVEL, e.g. 4100@20 (default: three vampiric weapons)")
    wp.add_argument("--rounds", type=int, default=40, help="combat rounds per case (default 40)")
    wp.add_argument("--cls", default="knight", choices=["mage", "cleric", "knight", "barbarian"])
    wp.add_argument("--port", type=int, default=9100)
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
    from xania_sim.setup import stage
    from xania_sim.trials.commands import TRIALS

    play.add_commands(sub)
    stage.add_command(sub)
    for name in TRIALS:
        t = sub.choices[name]
        t.add_argument("--json", action="store_true", help="print the result as JSON (the text goes to stderr)")
        t.add_argument("--no-record", action="store_true", help="don't store the run in the lore database")
    args = ap.parse_args()
    if args.xania:
        from xania_sim.core.server import set_checkout

        set_checkout(args.xania)
    if args.cmd in TRIALS:
        sys.exit(trial_cmd(TRIALS[args.cmd], args))
    if args.cmd == "lore":
        lore_cmd(args.db, args.level, args.names)
    elif args.cmd in ("report", "findings", "dispatch", "publish"):
        journal_cmd(args)
    elif args.cmd == "status":
        status_cmd(args.bot)


def trial_cmd(trial, args) -> int:
    """Run a trial; print its result (text, or JSON with everything else on stderr); return its exit code."""
    from xania_sim.record import watchdog

    out = sys.stderr if args.json else sys.stdout
    watchdog.arm()  # a trial whose event loop stops running gets a stack dump and an end (record/watchdog.py)
    try:
        with contextlib.redirect_stdout(out):
            result = trial(args)
            print(result.text())
    finally:
        watchdog.disarm()
    if args.json:
        print(result.to_json())
    return result.exit_code()


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
