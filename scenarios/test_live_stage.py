"""Staging: bots set up at level 40 with a gear kit and a skill loadout, checked against equipment and practice."""

import pytest

from xania_sim.setup import kits as K
from xania_sim.setup import loadouts as L
from xania_sim.core import world as W
from xania_sim.core.director import Director
from xania_sim.core.server import TestMud
from xania_sim.core.session import Bot
from xania_sim.setup.stage import stage

pytestmark = pytest.mark.live

PORT = 9140


@pytest.mark.parametrize("name, cls, kit, loadout", [("Stageknight", "knight", "best", "class"),
                                                      ("Stagemage", "mage", "shop", "class")])
async def test_stage_level_40(name, cls, kit, loadout):
    world = W.load()
    async with TestMud(port=PORT) as mud:
        d = await Director.create(mud)
        try:
            bot = Bot(name, "stagepass1", host=mud.host, port=mud.port)
            k = K.build_kit(world, kit, 40, cls)
            lo = L.build_loadout(loadout, cls, 40, src_dir=world.src_dir)
            rep = await stage(mud, d, bot, 40, k, lo, world=world)
            assert rep.level_seen == 40, rep.text()
            assert not rep.failed, rep.text()
            assert not rep.missing, rep.text()
            assert not rep.skill_mismatches, rep.text()
            assert len(rep.equipment) == len(k.worn), rep.text()
            assert set(rep.practice) == set(lo.expected_practice()), rep.text()
            await bot.quit()
        finally:
            await d.close()
