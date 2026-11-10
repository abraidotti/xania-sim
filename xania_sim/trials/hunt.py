"""Crash hunting: bots repeat one risky activity on a test MUD (running under gdb) until xania crashes or time runs
out, then save the crash report with the backtrace.

The built-in activity is fighting a mob: each bot is a fresh level-1 character with the game's default settings
(unlike other bots, hunters keep autoloot on), that walks to where the mob spawns, attacks it, and stays in the fight
(dying, fleeing or winning, and autolooting) until it's over. That's how the
``get all corpse`` crash (act_obj.cpp, ``arg1[3]`` past the end of "all") was reproduced.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from xania_sim.core import world as W
from xania_sim.record.findings import bug_line_findings, crash_finding, transcript_tails
from xania_sim.core.names import bot_names, mob_words
from xania_sim.activities.move import Navigator
from xania_sim.record.rooms import Fingerprints
from xania_sim.core.server import TestMud
from xania_sim.core.session import Bot, Profile

CLASSES = ["mage", "cleric", "knight", "barbarian"]
FIGHT_OVER = ("is DEAD", "You have been KILLED", "You flee", "You aren't fighting")


@dataclass
class HuntResult:
    crashed: bool
    seconds: float
    fights: int
    errors: int
    report: str = ""
    report_path: Path | None = None
    last_errors: list[str] = field(default_factory=list)
    findings: list = field(default_factory=list)  # findings.Finding: the crash, and xania's BUG: log lines


def spawn_rooms(world, keywords: str) -> tuple[list[int], str]:
    """Rooms where a mob matching ``keywords`` (in its short description) spawns, and the word to attack it by."""
    from xania_explorer.assess import mobs as M

    found = [i for i in M.collect(world) if keywords.lower() in i.mob.short_descr.lower()]
    if not found:
        raise KeyError(f"no mob matching {keywords!r}")
    rooms = sorted({s.room_vnum for i in found for s in i.spawns})
    return rooms, found[0].mob.keywords.split()[0]


async def _fighter(mud, w, fp, names, rooms, target, stop, res: HuntResult, runs):
    while not stop.is_set():
        # autoloot=None: keep the game's defaults (autoloot on), like a new player; hunts look for crashes
        bot = Bot(next(names), "simpass1", host=mud.host, port=mud.port, transcript_dir=runs, autoloot=None)
        try:
            await bot.enter(Profile(cls=random.choice(CLASSES)))
            nav = Navigator(bot, w, fp)
            await nav.locate()
            await nav.walk_to(random.choice(rooms))
            await bot.do(f"kill {target}")
            res.fights += 1
            for _ in range(90):
                if stop.is_set():
                    break
                r = await bot.do("time", expect="o'clock", timeout=5)
                if any(s in r.text for s in FIGHT_OVER):
                    break
                await asyncio.sleep(1)
            await bot.quit()
        except Exception as e:  # noqa: BLE001 - a hunt keeps going until the MUD dies or time is up
            res.errors += 1
            res.last_errors = (res.last_errors + [f"{type(e).__name__}: {str(e)[:160]}"])[-5:]
            await bot.close()
            if not mud.alive():
                stop.set()


async def hunt(mob: str, bots: int = 4, minutes: float = 10, port: int = 9100) -> HuntResult:
    """Run the hunt. Its findings come back in the result (``record.result.add_findings`` stores them)."""
    findings: list = []
    w = W.load()
    fp = Fingerprints(w)
    rooms, target = spawn_rooms(w, mob)
    names = iter(bot_names("hunter", 5000, mob_words(w)))
    runs = Path("runs") / ("hunt-" + time.strftime("%Y%m%d-%H%M%S"))
    res = HuntResult(False, 0, 0, 0)
    t0 = time.monotonic()
    async with TestMud(port=port) as mud:
        stop = asyncio.Event()

        async def watch():
            while not stop.is_set() and time.monotonic() - t0 < minutes * 60:
                await asyncio.sleep(2)
                if not mud.alive():
                    stop.set()
            stop.set()

        await asyncio.gather(watch(), *(_fighter(mud, w, fp, names, rooms, target, stop, res, runs)
                                        for _ in range(bots)))
        res.seconds = time.monotonic() - t0
        repro = f'python -m xania_sim hunt --mob "{mob}" --bots {bots}'
        if mud.crashed():
            res.crashed = True
            res.report = mud.crash_report()
            runs.mkdir(parents=True, exist_ok=True)
            res.report_path = runs / "xania-crash.txt"
            res.report_path.write_text(res.report, encoding="utf-8")
            findings.append(crash_finding(res.report, repro, f"Level-1 bots fighting {mob} (room {rooms}).",
                                          transcript_tails(runs)))
        findings.extend(bug_line_findings(mud.log("xania"), repro))
    res.findings = findings
    return res
