"""Navigation on the real MUD: routes from Explorer's map, rooms recognised as the bot arrives, danger avoided."""

import pytest

from xania_explorer import api
from xania_sim.activities.move import Navigator, danger_rooms
from xania_sim.record.rooms import Fingerprints


@pytest.fixture(scope="module")
def fingerprints(world):
    return Fingerprints(world)


@pytest.fixture(scope="module")
def level1_danger(world):
    return danger_rooms(world, 1, "knight")


async def test_walks_from_mud_school_to_the_south_gate(new_bot, world, fingerprints, level1_danger):
    bot = await new_bot()
    nav = Navigator(bot, world, fingerprints, avoid=level1_danger)
    assert await nav.locate() == 3700  # new characters start at the Mud School entrance
    gate = next(v for v, r in world.rooms.items() if r.name == "Outside the South Gate of Midgaard")
    expected = len(nav.plan(gate))
    r = await nav.walk_to(gate)
    assert r.reached and r.steps == expected and not nav.blocked
    look = await bot.do("look")
    assert look.lines[0] == "Outside the South Gate of Midgaard"
    assert not (nav.visited & level1_danger)


@pytest.mark.slow
async def test_explores_mud_school_safely(new_bot, world, fingerprints, level1_danger):
    bot = await new_bot()
    nav = Navigator(bot, world, fingerprints, avoid=level1_danger)
    cov = await nav.explore("Mud School", max_steps=300)
    rooms = set(api.rooms_in(world, "Mud School"))
    assert cov.share >= 0.5
    assert not (cov.visited & level1_danger)
    assert set(cov.skipped) == rooms - cov.visited
    assert all(why in ("dangerous", "no safe route", "no route", "locked", "blocked", "step limit")
               or why.startswith(("The ", "You ")) for why in cov.skipped.values())
