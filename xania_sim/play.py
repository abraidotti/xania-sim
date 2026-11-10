"""``python -m xania_sim level``: one bot levelling on its own for a while, survival first, with everything it does
recorded in the event log (so ``status`` and the dispatch show it).

It connects to a MUD that's already running (``--port``; 9000 is the usual one), or starts an isolated test MUD
with ``--test-mud``. The bot's password is ``--password`` (default ``botpass1``); a new name creates the character.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from . import world as W
from .events import Recorder
from .fight import FightLog, MobIndex
from .level import Leveller
from .nav import Navigator
from .rooms import Fingerprints
from .server import TestMud
from .session import Bot, Profile
from .store import Lore


async def _with_mud(test_mud: bool, port: int, play):
    if not test_mud:
        return await play("127.0.0.1", port)
    async with TestMud(port=port) as mud:
        return await play(mud.host, mud.port)


def _setup(bot: Bot, w, cls: str, race: str, lore: Lore, run_id: str):
    """A bot's navigator, event recorder and leveller (also used by xania-companions)."""
    nav = Navigator(bot, w, Fingerprints(w))
    rec = Recorder(lore.db, bot, cls=cls, race=race, run_id=run_id, room=lambda: nav.here, area_of=W.area_namer(w))
    lev = Leveller(bot, w, cls, nav, FightLog(lore.db), MobIndex(w), recorder=rec)
    return nav, rec, lev


async def level(name: str, cls: str, race: str, goal: int, minutes: float, port: int, test_mud: bool,
                password: str) -> None:
    w, lore = W.load(), Lore()
    runs = Path("runs") / f"level-{time.strftime('%Y%m%d-%H%M%S')}"

    async def play(host, port):
        bot = Bot(name, password, host=host, port=port, transcript_dir=runs)  # autoloot off: it crashes debug builds (do_get)
        await bot.enter(Profile(race=race, cls=cls))
        nav, rec, lev = _setup(bot, w, cls, race, lore, "level")
        rec.level = await lev.level()
        rec.snapshot()
        try:
            rep = await lev.run(goal_level=goal, budget=minutes * 60)
        finally:
            rec.snapshot()
            with contextlib.suppress(Exception):
                await bot.quit()
        print(f"{bot.name}: level {rep.start_level} -> {rep.level} in {rep.seconds / 60:.0f} min; {rep.kills} kills, "
              f"{rep.flees} flees, {rep.deaths} deaths, {rep.xp} xp ({rep.reason}). Transcript: {bot.transcript}")

    try:
        await _with_mud(test_mud, port, play)
    finally:
        lore.close()


def add_commands(sub) -> None:
    p = sub.add_parser("level", help="a bot that levels on its own, survival first")
    p.add_argument("name", help="the bot's character name (created if new)")
    p.add_argument("--goal", type=int, default=5, help="stop at this level")
    p.add_argument("--cls", default="knight", help="mage, cleric, knight or barbarian (default knight)")
    p.add_argument("--race", default="human")
    p.add_argument("--minutes", type=float, default=60)
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--test-mud", action="store_true", help="start an isolated test MUD on --port instead")
    p.add_argument("--password", default="botpass1")


def run(args) -> None:
    asyncio.run(level(args.name.capitalize(), args.cls, args.race, args.goal, args.minutes, args.port, args.test_mud,
                      args.password))
