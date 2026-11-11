"""Known Xania bugs, kept as live checks: each test passes while the bug is still there and fails (telling you to
retire it) once a fix makes the crash go away. Bot defaults that work around them (autoloot off) are checked too."""

import asyncio

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
