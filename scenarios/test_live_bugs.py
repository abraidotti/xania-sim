"""Known Xania bugs, kept as live checks: each test passes while the bug is still there and fails (telling you to
retire or flip it) once a fix changes the behaviour. Fixed bugs move to test_live_fixed.py as regression checks.
The bots' autoloot default (off: they loot by hand) is checked here too."""

import asyncio
import re

from xania_sim.core.director import Director
from xania_sim.core.session import Bot, Profile


async def test_bots_turn_autoloot_off(new_bot):
    bot = await new_bot()
    r = await bot.do("autoloot", expect=r"Autolooting removed|Automatic corpse looting set")
    assert "Automatic corpse looting set" in r.text  # it was off: toggling turned it back on
    await bot.set_autoloot(False)


async def _fighting(d: Director, name: str) -> str:
    """Who ``name`` is fighting, from act_wiz.cpp do_mstat's "Fighting: <name>" line ("" for nobody)."""
    r = await d.do(f"stat mob {name}", expect=r"Fighting: .*")
    m = re.search(r"Fighting: (.*)", r)
    return "" if m is None or m.group(1).strip() == "(none)" else m.group(1).strip()


async def _attacked_within(d: Director, name: str, seconds: int) -> str:
    for _ in range(seconds):
        if who := await _fighting(d, name):
            return who
        await asyncio.sleep(1)
    return ""


async def test_dark_vision_does_nothing_an_aggressive_bat_ignores_a_lightless_player(mud, names):
    """AffectFlag::DarkVision is never read (abraidotti/xania#10, upstream mattgodbolt/xania#246). Moria's bats get it
    from their race and have no infravision, so in a dark room Char::can_see fails for them and aggr_update (which
    needs can_see) skips a player without a light. A controlled pair, same bot, same bats, same room:

    - room 4155 "The large tunnel" (flags Dark | Indoors), with a blue bat (4150, level 7, aggressive, sentinel)
      loaded on top of the bats its resets put there;
    - the bot, a level-10 human (no infravision; within aggr_update's `ch->level >= vch->level - 5` of the bats),
      first with no light: no fight for 15 s (aggr_update runs every pulse, 4 a second, and each bat rolls 50%, so a
      bat that could see the bot would attack within a second or two);
    - then holding its light again, which makes the room not dark (room->light > 0): the bats attack.

    Every new character starts holding a lit war banner (3365), so the bot takes it off first; the director has one
    too, so it stages the tunnel from Temple with `at` and `transfer <bot> 4155` and never stands in it. The lit half
    shows the stage can produce an attack, so the quiet unlit half means "couldn't see", not "never would".

    If dark vision ever works (#10's options B or C), the unlit half fails: flip or retire this test. Under option A
    (remove the flag) it keeps passing, and then pins that the removal changed nothing for players."""
    bot = Bot(names(), "simpass1", host=mud.host, port=mud.port)
    await bot.enter(Profile())
    d = await Director.create(mud)
    try:
        await d.advance(bot.name, 10)  # enough hp for a few bites; still within the level-7 bats' reach
        r = await bot.do("remove banner", expect=r"You stop using|You aren't|can't remove")
        assert "You stop using" in r.text, f"the bot couldn't put its light away:\n{r.text}"
        # Staged from afar: the director's own banner would light the tunnel while the bot arrived.
        await d.goto(3001)
        await d.do("at 4155 load mob 4150", expect=r"Ok\.|No mob")
        await d.do(f"transfer {bot.name} 4155", expect=re.compile(r"^Ok\.$|They aren't here|No such location", re.M))

        look = await bot.do("look", expect=r"pitch black|\[Exits:")
        assert "It is pitch black" in look.text, f"room 4155 wasn't dark for the bot, the check can't run:\n{look.text}"
        unlit = await _attacked_within(d, bot.name, 15)

        if not unlit:  # the control: same bot, same bats, with a light
            r = await bot.do("wear banner", expect=r"You light|You hold|You can't|You do not have")
            assert "You light" in r.text, f"the bot couldn't light its banner again:\n{r.text}"
            lit = await _attacked_within(d, bot.name, 15)
        else:
            lit = ""
    finally:
        try:
            await d.do("at 4155 peace", expect=r"Ok\.")
            await d.restore(bot.name)
        finally:
            await d.close()
    await bot.close()
    assert not unlit, (f"{unlit} attacked a player without a light in a dark room: dark vision seems to work now "
                       "(abraidotti/xania#10 option B or C), flip or retire this test")
    assert lit, "no bat attacked even with a lit lantern in 15 s, so the stage is wrong and the check proved nothing"
