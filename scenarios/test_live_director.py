"""The director: an immortal on the test MUD that levels bots, sets skills and hands out gear."""

import re

import pytest

from xania_sim.core.director import Director
from xania_sim.core.server import TestMud
from xania_sim.core.session import Bot, Profile

pytestmark = pytest.mark.live


async def test_director_sets_the_stage():
    async with TestMud(port=9100) as mud:
        d = await Director.create(mud)
        try:
            bot = Bot("Stagehand", "stagepass1", host=mud.host, port=mud.port)
            await bot.enter(Profile(cls="knight"))
            await d.bring("Stagehand")
            await d.advance("Stagehand", 40)
            r = await bot.do("score", expect=r"Level: \S*40\b")
            assert r.complete, r.text
            await d.set_skill("Stagehand", "parry", 75)
            r = await bot.do("practice", expect=r"practice sessions? left")
            assert re.search(r"parry\s+75%", r.text), r.text
            r = await d.give_obj("Stagehand", 3021)  # Midgaard's short sword (any object will do)
            assert "You give" in r, r
            inv = await bot.do("inventory", expect=r"You are carrying")
            assert "sword" in inv.text.lower(), inv.text
            await bot.quit()
        finally:
            await d.close()
        # the director survives a restart (its player file stays immortal)
        d = await Director.create(mud)
        await d.close()
