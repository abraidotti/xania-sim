"""Known Xania bugs, kept as live checks: each test passes while the bug is still there and fails (telling you to
retire it) once a fix makes the crash go away. Bot defaults that work around them (autoloot off) are checked too."""

import asyncio
import re

import pytest
from xania_explorer import api

from xania_sim.activities.move import Navigator
from xania_sim.activities.practice import practice_rooms
from xania_sim.record.findings import crash_finding
from xania_sim.record.rooms import Fingerprints
from xania_sim.core.director import Director
from xania_sim.core.session import Bot, Profile


async def _crashes_after(mud, bot, command) -> bool:
    try:
        await bot.do(command, timeout=5)
    except Exception:  # noqa: BLE001 - the connection may hang or close as xania dies
        pass
    for _ in range(40):
        if not mud.alive():
            return True
        await asyncio.sleep(0.25)
    return False


async def test_bots_turn_autoloot_off(new_bot):
    bot = await new_bot()
    r = await bot.do("autoloot", expect=r"Autolooting removed|Automatic corpse looting set")
    assert "Automatic corpse looting set" in r.text  # it was off: toggling turned it back on
    await bot.set_autoloot(False)


async def test_get_all_in_an_empty_room_crashes_debug_builds(mud, names, found):
    """act_obj.cpp do_get: 'get all' reads arg1[3] where arg1 is "all" (3 chars) when nothing is found (line 648).
    Each run that still sees it adds an occurrence to the finding (so the dispatch shows it's still there)."""
    bot = Bot(names(), "simpass1", host=mud.host, port=mud.port)
    await bot.enter(Profile())
    crashed = await _crashes_after(mud, bot, "get all")
    report = mud.crash_report() if crashed else ""
    await bot.close()
    if crashed:
        found.append(crash_finding(report, 'python -m pytest scenarios/test_live_bugs.py -k get_all',
                                   "A bot typed 'get all' in an empty room (a known-bug check)."))
        await mud.restart()
    assert crashed, "'get all' no longer crashes xania: the bug looks fixed, retire this test"
    assert "do_get" in report and "act_obj.cpp" in report


async def test_practice_empty_quotes_at_a_trainer_crashes_debug_builds(mud, names, found, world):
    """act_info.cpp do_practice -> lookup.cpp skill_lookup: `practice ''` at a trainer. ArgParser turns '' into an
    empty view (it strips the quotes), do_practice only checked that the line had arguments, and skill_lookup reads
    name[0] of it. A lead from Xania Builder's string_view detector (abraidotti/xania#1's analysis)."""
    bot = Bot(names(), "simpass1", host=mud.host, port=mud.port)
    await bot.enter(Profile())
    nav = Navigator(bot, world, Fingerprints(world))
    await nav.locate()
    dist = api.distances(world, nav.here) if nav.here is not None else {}
    rooms = sorted((v for v in practice_rooms(world) if v in dist), key=lambda v: dist[v])
    at_trainer = crashed = False
    for room in rooms[:4]:
        if not (await nav.walk_to(room)).reached:
            continue
        # A real practice first: proves a trainer is here (and that the bot has sessions) before the odd one.
        r = await bot.do("practice sword", expect=r"You practice|already learned|can't practice|can't do that here|"
                                                  r"no practice sessions")
        if "can't do that here" in r.text:
            continue
        at_trainer = "no practice sessions" not in r.text
        if at_trainer:
            crashed = await _crashes_after(mud, bot, "practice ''")
        break
    report = mud.crash_report() if crashed else ""
    if not crashed:
        await bot.close()
    if not at_trainer:
        pytest.skip("no trainer with a session to spare was reached; the check didn't run")
    if crashed:
        found.append(crash_finding(report, "python -m pytest scenarios/test_live_bugs.py -k practice_empty",
                                   "A bot typed practice '' at a trainer (a known-bug check)."))
        await mud.restart()
    assert crashed, "practice '' no longer crashes xania: the bug looks fixed (or isn't there), retire this test"
    assert "skill_lookup" in report


async def test_autoloot_of_a_corpse_with_something_in_it_crashes_debug_builds(mud, names, found):
    """act_obj.cpp do_get line 707 (abraidotti/xania#1): autoloot runs `get all corpse`, and for each object in the
    corpse the undroppable-item guard reads arg1[3] of "all". A hunt only reaches it when a mob happens to carry
    something; here the director makes sure one does: a beggar given a small sword, killed by a level-30 bot."""
    bot = Bot(names(), "simpass1", host=mud.host, port=mud.port)
    await bot.enter(Profile())
    await bot.set_autoloot(True)
    d = await Director.create(mud)
    try:
        await d.goto(3005)  # Temple Square: not a safe room
        await d.bring(bot.name)
        await d.do("load mob 3065", expect=r"Ok\.|No mob")  # a beggar (level 1, sentinel)
        r = await d.give_obj("beggar", 3021, "sword")  # a small sword (Midgaard)
        assert "You give" in r, r
        await d.advance(bot.name, 30)  # so the level-1 beggar dies in the first round or two
    finally:
        await d.close()
    # The fight takes a few rounds; autoloot runs when the beggar dies. Wait for whichever comes first: xania dying,
    # or the sword in the bot's inventory (looted, so the faulty line ran and survived).
    looted = crashed = False
    try:
        await bot.do("kill beggar", timeout=5)
    except Exception:  # noqa: BLE001
        pass
    for _ in range(15):
        await asyncio.sleep(3)
        if not mud.alive():
            crashed = True
            break
        try:
            inv = await bot.do("inventory", expect=r"You are carrying", timeout=5)
        except Exception:  # noqa: BLE001 - the connection may close as xania dies
            continue
        if "a small sword" in inv.text.split("You are carrying", 1)[-1]:  # not the bot's title ("the Swordpupil")
            looted = True
            break
    report = mud.crash_report() if crashed else ""
    if not crashed:
        await bot.close()
        assert looted, "the beggar wasn't killed and looted in 45 s, so the check didn't run"
    if crashed:
        found.append(crash_finding(report, "python -m pytest scenarios/test_live_bugs.py -k autoloot_of_a_corpse",
                                   "A bot with autoloot on killed a mob carrying a sword (a known-bug check)."))
        await mud.restart()
    assert crashed, "autoloot no longer crashes xania: the bug looks fixed, retire this test"
    assert "do_get" in report and "loot_and_sacrifice_corpse" in report


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


async def test_cast_empty_quotes_crashes_debug_builds(mud, names, found):
    """magic.cpp do_cast -> lookup.cpp skill_lookup: `cast ''` anywhere. Like practice '' (abraidotti/xania#7), but
    any player can type it in any room: do_cast only checks that the line had arguments."""
    bot = Bot(names(), "simpass1", host=mud.host, port=mud.port)
    await bot.enter(Profile())
    crashed = await _crashes_after(mud, bot, "cast ''")
    report = mud.crash_report() if crashed else ""
    if not crashed:
        await bot.close()
    if crashed:
        found.append(crash_finding(report, "python -m pytest scenarios/test_live_bugs.py -k cast_empty",
                                   "A bot typed cast '' (a known-bug check)."))
        await mud.restart()
    assert crashed, "cast '' no longer crashes xania: the bug looks fixed, retire this test"
    assert "skill_lookup" in report and "do_cast" in report
