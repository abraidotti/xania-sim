"""``python -m xania_sim churn``: bots create characters and quit, as fast as the lobby allows, on a test MUD,
until xania crashes or time runs out. Reproduces crashes that need a quit to coincide with something periodic.

Found with it: a quit that lands on the same pulse as a tick reads freed memory. do_quit extracts the character
(handler.cpp extract_char: it stays in char_list until collect_all_garbage, and keeps its ``desc``) and closes the
descriptor; the game loop frees closed descriptors (MudImpl.cpp reap_closed) and then, on a tick, char_update
calls Char::is_immortal -> get_trust -> desc->is_switched() on it (update.cpp), before the garbage is collected.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from pathlib import Path

from xania_sim.core import world as W
from xania_sim.core.names import bot_names, mob_words
from xania_sim.core.server import TestMud
from xania_sim.core.session import Bot, LoginError, Profile
from xania_sim.core.telnet import Closed, Timeout


@dataclass
class ChurnReport:
    quits: int = 0
    seconds: float = 0.0
    crashed: bool = False
    report: str = ""
    report_path: Path | None = None
    errors: list[str] = field(default_factory=list)
    findings: list = field(default_factory=list)  # findings.Finding


async def churn(bots: int = 6, minutes: float = 15, port: int = 9100) -> ChurnReport:
    runs = Path("runs") / f"churn-{time.strftime('%Y%m%d-%H%M%S')}"
    names = iter(bot_names("churn", 100_000, mob_words(W.load())))
    rep = ChurnReport()
    t0 = time.monotonic()
    end = t0 + minutes * 60
    async with TestMud(port=port) as mud:
        stop = asyncio.Event()

        async def loop(i: int) -> None:
            while not stop.is_set() and time.monotonic() < end:
                bot = Bot(next(names), "churnpass1", host=mud.host, port=mud.port)
                try:
                    await bot.enter(Profile(cls=("mage", "cleric", "knight", "barbarian")[i % 4]))
                    await bot.quit()
                    rep.quits += 1
                except (LoginError, Closed, Timeout, OSError) as e:
                    with contextlib.suppress(Exception):
                        await bot.close()
                    if not mud.alive():
                        stop.set()
                        return
                    rep.errors.append(f"{type(e).__name__}: {str(e)[:120]}")

        async def watch() -> None:
            while not stop.is_set() and time.monotonic() < end:
                await asyncio.sleep(2)
                if not mud.alive():
                    stop.set()

        await asyncio.gather(watch(), *(loop(i) for i in range(bots)))
        rep.seconds = time.monotonic() - t0
        if not mud.alive():
            rep.crashed = True
            rep.report = mud.crash_report()
            runs.mkdir(parents=True, exist_ok=True)
            rep.report_path = runs / "xania-crash.txt"
            rep.report_path.write_text(rep.report, encoding="utf-8")
        log = mud.log("xania")
    from xania_sim.record.findings import bug_line_findings, crash_finding

    repro = f"python -m xania_sim churn --bots {bots} --minutes {minutes:g}"
    if rep.crashed:
        rep.findings.append(crash_finding(
            rep.report, repro, f"{bots} bots creating characters and quitting in a loop; crashed after {rep.quits} "
            f"quits in {rep.seconds:.0f} s."))
    rep.findings += bug_line_findings(log, repro)
    return rep
