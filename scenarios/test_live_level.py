"""Survival and levelling on the real MUD: a new knight leaves Mud School and levels on fights the model (and the
shared fight log) call safe, without dying. Its events go into the shared event log, so the run shows up in
``python -m xania_sim status`` and on the dispatch."""

import pytest

from xania_sim.core import world as W
from xania_sim.record.events import Progress, Recorder
from xania_sim.activities.fight import FightLog, MobIndex
from xania_sim.players.careful import Leveller
from xania_sim.activities.move import Navigator
from xania_sim.record.rooms import Fingerprints
from xania_sim.core.session import Profile
from xania_sim.record.store import Lore


@pytest.mark.slow
async def test_new_knight_levels_without_dying(new_bot, world):
    lore = Lore()
    try:
        bot = await new_bot(Profile(cls="knight"))
        nav = Navigator(bot, world, Fingerprints(world))
        rec = Recorder(lore.db, bot, cls="knight", race="human", level=1, run_id="tests", room=lambda: nav.here,
                       area_of=W.area_namer(world))
        rec.snapshot()
        lev = Leveller(bot, world, "knight", nav, FightLog(lore.db), MobIndex(world), recorder=rec)
        rep = await lev.run(goal_level=2, budget=15 * 60)
        rec.snapshot()
        summary = "; ".join(f"{world.mobs[f.mob].short_descr} {f.outcome} hp {f.hp_before}->{f.hp_after}"
                            for f in rep.fights)
        assert rep.deaths == 0, summary
        assert rep.level >= 2, f"{rep.reason}: {summary}"
        [me] = [b for b in Progress(lore.db).bots() if b.name == bot.name]
        assert me.kills >= 1 and me.level >= 2
    finally:
        lore.close()
