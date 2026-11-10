"""The harness itself, end to end on the isolated test MUD."""

import pytest

from xania_sim.core.session import Bot, Profile


async def test_new_character_starts_in_mud_school(new_bot):
    bot = await new_bot(Profile(race="human", cls="knight"))
    assert bot.created and bot.prompt.hp == bot.prompt.max_hp > 0
    look = await bot.do("look")
    assert look.lines[0] == "Entrance to Mud School"
    assert look.has(r"^\[Exits: .*north")


async def test_walking_follows_the_area_files(new_bot, world):
    """A step through an exit lands in the room Explorer's world says it leads to."""
    from xania_explorer import api

    start = next(v for v, r in world.rooms.items() if r.name == "Entrance to Mud School")
    [step] = [s for s in api.steps_from(world, start) if s.command == "north"]
    bot = await new_bot()
    r = await bot.do(step.command)
    assert r.lines[0] == world.rooms[step.to_vnum].name
    assert bot.prompt.moves < bot.prompt.max_moves


async def test_character_is_saved_and_logs_back_in(mud, names):
    name = names()
    bot = Bot(name, "simpass1", host=mud.host, port=mud.port)
    await bot.enter(Profile(race="dwarf", cls="cleric"))
    await bot.quit()
    assert name in mud.run(f"ls {mud.data_dir}/player")
    again = Bot(name, "simpass1", host=mud.host, port=mud.port)
    await again.enter()
    assert not again.created
    assert (await again.do("score")).has(r"dwarf")
    await again.quit()


async def test_several_bots_at_once(new_bot):
    import asyncio

    bots = await asyncio.gather(*(new_bot(Profile(cls=c)) for c in ("mage", "cleric", "knight", "barbarian")))
    who = await bots[0].do("who")
    for b in bots:
        assert b.name in who.text


async def test_wrong_password_and_mob_names_are_refused(mud, names):
    from xania_sim.core.session import LoginError

    name = names()
    b = Bot(name, "simpass1", host=mud.host, port=mud.port)
    await b.enter(Profile())
    await b.quit()
    with pytest.raises(LoginError, match="Wrong password"):
        await Bot(name, "wrongpass", host=mud.host, port=mud.port).enter()
    with pytest.raises(LoginError, match="Illegal name"):
        await Bot("Smokey", "simpass1", host=mud.host, port=mud.port).enter(Profile())
