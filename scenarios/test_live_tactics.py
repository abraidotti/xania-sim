"""Combat tactics on the real MUD (its own test MUD on port 9150): the director stages a level 40 mage and a level 40
knight (every class skill and spell up to 40 at 75%, a weapon wielded), loads a wandering wolf (level 40, 533 HP on
average; it trips, dodges and disarms) into a quiet room of the park and brings the bot to it, restoring it before
every bout. ``test_tactics_staged`` fights a couple with tactics on; ``test_tactics_comparison`` (slow) fights the
same mob N times with tactics off and then on, and writes the numbers to runs/tactics-*/comparison.json.

    XANIA_SIM_TACTICS_N=8 python -m pytest scenarios/test_live_tactics.py -m live -s
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from xania_explorer import api

from xania_sim import world as W
from xania_sim.director import Director
from xania_sim.events import Recorder
from xania_sim.fight import fight
from xania_sim.names import bot_names, mob_words
from xania_sim.nav import ROOM_REPLY, Navigator
from xania_sim.server import TestMud
from xania_sim.session import Bot, Profile
from xania_sim.store import Lore
from xania_sim.tactics import Tactics

pytestmark = pytest.mark.live

PORT = 9150
LEVEL = 40
ARENA = 3108  # Midgaard park, "Small path in the park": no resets, no room flags
WOLF = 10093  # "a wandering wolf"
WEAPONS = {"mage": (3020, "dagger"), "knight": (3022, "sword")}  # a thin dagger, a long sword
RUNS = Path(__file__).resolve().parents[1] / "runs" / time.strftime("tactics-%Y%m%d-%H%M%S")


@pytest_asyncio.fixture(scope="module")
async def stage():
    """A test MUD of its own, the director, and a supply of fresh names."""
    world = W.load()
    mud = TestMud(port=PORT)
    try:
        await mud.start()
    except RuntimeError as e:
        if "NO_BUILD" in str(e):
            pytest.skip(f"Xania isn't built for the test MUD: {e}")
        raise
    d = await Director.create(mud)
    names = iter(bot_names("tacbot", 200, mob_words(world)))
    lore = Lore()
    try:
        yield mud, d, world, names, lore
    finally:
        lore.close()
        await d.close()
        await mud.stop()


async def staged(mud, d, name: str, cls: str) -> Bot:
    """A level 40 ``cls`` with its class's skills and spells up to level 40 at 75%, wielding its weapon. (Per skill
    rather than ``set skill <name> all``: that also grants what the class can never learn, see the report.)"""
    RUNS.mkdir(parents=True, exist_ok=True)
    bot = Bot(name, "simpass1", host=mud.host, port=mud.port, transcript_dir=RUNS)
    await bot.enter(Profile(cls=cls))
    await d.bring(bot.name)
    for a in api.abilities(cls):
        if a.level <= LEVEL:
            await d.set_skill(bot.name, a.name, 75)
    await arm(d, bot, cls)
    return bot


async def arm(d, bot: Bot, cls: str) -> None:
    """(Re)level to 40 and wield a weapon: after a death the bot is a level down and its weapon is in the corpse."""
    await d.goto(3001)
    await d.bring(bot.name)
    await d.advance(bot.name, LEVEL)
    vnum, kw = WEAPONS[cls]
    await d.give_obj(bot.name, vnum, kw)
    await bot.do(f"wield {kw}", expect=r"You wield|You are already|You can't|You don't have")


async def into_arena(d, bot: Bot) -> None:
    """A fresh wolf in the arena, the bot beside it at full HP and mana, the director gone."""
    await d.goto(ARENA)
    await d.do("purge", expect=r"Ok\.|\n")
    await d.do(f"load mob {WOLF}", expect=r"Ok\.|No mob")
    await d.bring(bot.name)
    await d.restore(bot.name)
    await d.goto(3001)
    await bot.do("stand", expect=r"You stand|You are already|You're|Nah")
    await bot.do("look", expect=ROOM_REPLY)  # read the restore's prompt, and anything else waiting


@dataclass
class Bout:
    cls: str
    tactics: bool
    outcome: str
    seconds: float
    rounds: int
    hp_lost: float  # share of max HP
    mana: int  # spent, buffs included
    actions: int


async def bout(world, d, bot: Bot, cls: str, tactics: Tactics | None) -> Bout:
    await into_arena(d, bot)
    t0, mana0 = time.monotonic(), bot.prompt.mana
    used = len(tactics.uses) if tactics is not None else 0
    if tactics is not None:
        await tactics.prepare(bot)
    nav = Navigator(bot, world)
    nav.here = ARENA
    r = await fight(bot, WOLF, "wolf", escape=nav.escape, tactics=tactics)
    seconds = time.monotonic() - t0
    lost = (r.hp_before - max(0, r.hp_after)) / r.max_hp if r.outcome != "died" else 1.0
    b = Bout(cls, tactics is not None, r.outcome, round(seconds, 1), r.rounds, round(lost, 3),
             max(0, mana0 - r.mana_after), (len(tactics.uses) - used) if tactics is not None else 0)
    if r.outcome == "died":
        await arm(d, bot, cls)
    return b


def summarise(bouts: list[Bout]) -> dict:
    out = {}
    for b in bouts:
        out.setdefault((b.cls, "on" if b.tactics else "off"), []).append(b)
    table = {}
    for (cls, arm_), bs in sorted(out.items()):
        n = len(bs)
        table[f"{cls} {arm_}"] = {
            "fights": n, "kills": sum(b.outcome == "won" for b in bs), "flees": sum(b.outcome == "fled" for b in bs),
            "deaths": sum(b.outcome == "died" for b in bs),
            "seconds": round(sum(b.seconds for b in bs) / n, 1), "rounds": round(sum(b.rounds for b in bs) / n, 1),
            "hp_lost_share": round(sum(b.hp_lost for b in bs) / n, 3), "mana": round(sum(b.mana for b in bs) / n, 1),
            "actions": round(sum(b.actions for b in bs) / n, 1)}
    return table


def recorder_for(bot: Bot, cls: str, world, lore) -> Recorder:
    rec = Recorder(lore.db, bot, cls=cls, race="human", level=LEVEL, run_id="tactics", room=lambda: ARENA,
                   area_of=W.area_namer(world))
    return rec


async def test_tactics_staged(stage):
    """Both bots buff, open and use their skills and spells, and every use is recorded."""
    mud, d, world, names, lore = stage
    for cls in ("mage", "knight"):
        bot = await staged(mud, d, next(names), cls)
        try:
            rec = recorder_for(bot, cls, world, lore)
            t = await Tactics.read(bot, cls, LEVEL, world, recorder=rec)
            bouts = [await bout(world, d, bot, cls, t) for _ in range(2)]
            names_used = {u.action.name for u in t.uses}
            print(cls, [asdict(b) for b in bouts], sorted(names_used))
            assert all(b.outcome != "died" for b in bouts), bouts
            assert any(u.ok for u in t.uses), [(u.action.command, u.text[:120]) for u in t.uses]
            if cls == "mage":
                assert names_used & {"fireball", "acid blast"} and "sanctuary" in names_used
            else:
                assert names_used & {"kick", "headbutt", "trip", "bash"}
            n = lore.db.execute("SELECT COUNT(*) FROM events WHERE bot = ? AND kind = 'tactic'",
                                (bot.name,)).fetchone()[0]
            assert n >= 1
            rec.detach()
        finally:
            await bot.quit()


@pytest.mark.slow
async def test_tactics_comparison(stage):
    """The same bot against the same mob, N times with tactics off and then N times on."""
    mud, d, world, names, lore = stage
    n = int(os.environ.get("XANIA_SIM_TACTICS_N", "6"))
    bouts: list[Bout] = []
    for cls in ("mage", "knight"):
        bot = await staged(mud, d, next(names), cls)
        try:
            for _ in range(n):
                bouts.append(await bout(world, d, bot, cls, None))
            rec = recorder_for(bot, cls, world, lore)
            t = await Tactics.read(bot, cls, LEVEL, world, recorder=rec)
            for _ in range(n):
                bouts.append(await bout(world, d, bot, cls, t))
            rec.detach()
        finally:
            await bot.quit()
    table = summarise(bouts)
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / "comparison.json").write_text(json.dumps({"table": table, "bouts": [asdict(b) for b in bouts]}, indent=1))
    for k, v in table.items():
        print(k, v)
    for cls in ("mage", "knight"):
        on, off = table[f"{cls} on"], table[f"{cls} off"]
        assert on["deaths"] <= off["deaths"], table  # survival first: tactics never cost lives
