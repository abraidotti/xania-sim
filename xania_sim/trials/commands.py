"""The CLI's trials, each returning a ``record.result.TrialResult`` (printed as text or ``--json``; its exit code is
the process's). Recording into the lore database happens here, once, for every trial."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from xania_sim.record.result import Outcome, TrialResult, add_findings, game_version


def run(name: str, title: str, body, record: bool = True) -> TrialResult:
    """Run ``body(result) -> findings`` and fill in timing, the game version and the findings; any error in the sim
    or the setup makes the result "couldn't run" (exit code 2) rather than a crash of the command."""
    result = game_version(TrialResult(name, title))
    t0 = time.monotonic()
    try:
        found = body(result) or []
    except KeyboardInterrupt:
        raise
    except Exception as e:  # the MUD wouldn't start, no build, a bot error...
        result.ran, result.error = False, f"{type(e).__name__}: {e}"
        found = []
    result.seconds = round(time.monotonic() - t0, 1)
    if result.ran:
        add_findings(result, found, record)
    return result


def smoke(args) -> TrialResult:
    from xania_sim.core import world
    from xania_sim.core.names import bot_names, mob_words
    from xania_sim.core.server import TestMud
    from xania_sim.core.session import Bot, Profile
    from xania_sim.record.findings import bug_line_findings

    async def visit(port: int, result: TrialResult) -> None:
        [name] = bot_names("smoke", 1, mob_words(world.load()))
        bot = Bot(name, "botpass1", port=port, transcript_dir=Path("runs") / time.strftime("%Y%m%d-%H%M%S"))
        r = await bot.enter(Profile(race="human", cls="knight"))
        look = await bot.do("look")
        await bot.quit()
        result.outcomes.append(Outcome("account", True, f"{bot.name} {'created' if bot.created else 'logged in'}",
                                       [str(r.prompt)]))
        result.outcomes.append(Outcome("move", look.complete, "looked around", look.lines[:6]))
        result.artifacts["transcript"] = str(bot.transcript)

    async def go(result):
        if args.existing:
            await visit(args.port, result)
            return []
        async with TestMud(port=args.port) as mud:
            await visit(mud.port, result)
            return bug_line_findings(mud.log("xania"), "python -m xania_sim smoke")

    return run("smoke", "Smoke: create a bot, look, quit", lambda r: asyncio.run(go(r)), not args.no_record)


def hunt(args) -> TrialResult:
    from xania_sim.trials.hunt import hunt as _hunt

    def body(result):
        r = asyncio.run(_hunt(args.mob, args.bots, args.minutes, args.port))
        result.measurements.update(fights=r.fights, bot_errors=r.errors, crashed=r.crashed)
        result.outcomes += [Outcome("fight", False, e) for e in r.last_errors]
        if r.report_path:
            result.artifacts["crash_report"] = str(r.report_path)
        return r.findings

    return run("hunt", f"Hunt: {args.mob}, {args.bots} bots", body, not args.no_record)


def churn(args) -> TrialResult:
    from xania_sim.trials.churn import churn as _churn

    def body(result):
        r = asyncio.run(_churn(args.bots, args.minutes, args.port))
        result.measurements.update(quits=r.quits, bot_errors=len(r.errors), crashed=r.crashed)
        if r.report_path:
            result.artifacts["crash_report"] = str(r.report_path)
        return r.findings

    return run("churn", f"Churn: {args.bots} bots creating and quitting", body, not args.no_record)


def stage(args) -> TrialResult:
    from xania_sim.setup import stage as S

    def body(result):
        rep = asyncio.run(S.stage_cmd(args))
        t = rep.kit.totals()
        result.measurements.update(level_seen=rep.level_seen, worn=len(rep.worn) - len(rep.failed),
                                   failed=len(rep.failed), missing=len(rep.missing),
                                   skill_mismatches=len(rep.skill_mismatches), ac=t.ac, hitroll=t.hitroll,
                                   damroll=t.damroll, weapon_average=t.weapon_average)
        result.outcomes += [Outcome("gear", w.ok, f"{w.piece.command} {w.piece.name}", [w.reply]) for w in rep.worn]
        if not rep.ok:  # the stage wasn't set: nothing after it would be a fair test
            result.ran, result.error = False, "staging incomplete: " + "; ".join(
                [f"{w.piece.name}: {w.reply}" for w in rep.failed][:3] + [f"skills off: {len(rep.skill_mismatches)}"]
                * bool(rep.skill_mismatches))
        return []

    return run("stage", f"Stage: {args.name}, level {args.level} {args.cls}, kit {args.kit}, loadout {args.loadout}",
               body, not args.no_record)


def level(args) -> TrialResult:
    from xania_sim.players import run as play

    def body(result):
        rep = play.run(args)
        result.measurements.update(start_level=rep.start_level, level=rep.level, kills=rep.kills, deaths=rep.deaths,
                                   flees=rep.flees, xp=rep.xp, stopped=rep.reason)
        if getattr(rep, "transcript", None):
            result.artifacts["transcript"] = rep.transcript
        return []

    return run("level", f"Level: {args.name}, {args.cls} to level {args.goal}", body, not args.no_record)


def skill(args) -> TrialResult:
    from xania_sim.trials.skill import run_skill

    def body(result):
        return asyncio.run(run_skill(result, args.skill, args.port, args.classes, args.levels, args.fights))

    return run("skill", f"Skill: {args.skill}", body, not args.no_record)


def area(args) -> TrialResult:
    from xania_sim.trials.area import run_area

    def body(result):
        return asyncio.run(run_area(result, args.area, args.port, args.classes, args.levels, args.minutes, args.kit,
                                    args.cut_down, args.quick, args.fail_fast))

    return run("area", f"Area: {args.area}" + (" (cut-down world)" if args.cut_down else ""), body,
               not args.no_record)


TRIALS = {"smoke": smoke, "hunt": hunt, "churn": churn, "stage": stage, "level": level, "skill": skill, "area": area}
