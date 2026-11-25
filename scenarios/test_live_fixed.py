"""Xania bugs that were fixed upstream, kept as live regression checks: each test passes while the fix holds and
fails (recording the crash as a finding, if there is one) if the bug comes back. They began in test_live_bugs.py,
pinning each bug, and were flipped when its fix was merged."""

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

from conftest import RUNS  # every bot keeps a transcript, so a failure in a full run can be read afterwards


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


async def _regressed(mud, found, repro: str, what: str) -> str:
    """xania died: record it as a finding, restart the MUD for the next test, and return the crash report."""
    report = mud.crash_report()
    found.append(crash_finding(report, repro, what))
    await mud.restart()
    return report


async def test_get_all_in_an_empty_room_survives(mud, names, found):
    """act_obj.cpp do_get read ``arg1[3]`` of "all" when nothing was found (abraidotti/xania#1), aborting debug
    builds. Fixed upstream in mattgodbolt/xania#307."""
    bot = Bot(names(), "simpass1", host=mud.host, port=mud.port, transcript_dir=RUNS)
    await bot.enter(Profile())
    crashed = await _crashes_after(mud, bot, "get all")
    if crashed:
        report = await _regressed(mud, found, "python -m pytest scenarios/test_live_fixed.py -k get_all",
                                  "A bot typed 'get all' in an empty room (a regression check for xania#307).")
        pytest.fail(f"'get all' crashes xania again (mattgodbolt/xania#307 regressed):\n{report}")
    await bot.close()


async def test_practice_empty_quotes_at_a_trainer_survives(mud, names, found, world):
    """act_info.cpp do_practice -> lookup.cpp skill_lookup read ``name[0]`` of the empty view ``practice ''`` makes
    (abraidotti/xania#7). Fixed upstream in mattgodbolt/xania#310: an empty name matches nothing."""
    bot = Bot(names(), "simpass1", host=mud.host, port=mud.port, transcript_dir=RUNS)
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
    if crashed:
        report = await _regressed(mud, found, "python -m pytest scenarios/test_live_fixed.py -k practice_empty",
                                  "A bot typed practice '' at a trainer (a regression check for xania#310).")
        pytest.fail(f"practice '' crashes xania again (mattgodbolt/xania#310 regressed):\n{report}")
    await bot.close()
    if not at_trainer:
        pytest.skip("no trainer with a session to spare was reached; the check didn't run")


async def test_cast_empty_quotes_survives(mud, names, found):
    """magic.cpp do_cast -> lookup.cpp skill_lookup: ``cast ''`` anywhere, like practice ''. Fixed upstream in
    mattgodbolt/xania#310."""
    bot = Bot(names(), "simpass1", host=mud.host, port=mud.port, transcript_dir=RUNS)
    await bot.enter(Profile())
    crashed = await _crashes_after(mud, bot, "cast ''")
    if crashed:
        report = await _regressed(mud, found, "python -m pytest scenarios/test_live_fixed.py -k cast_empty",
                                  "A bot typed cast '' (a regression check for xania#310).")
        pytest.fail(f"cast '' crashes xania again (mattgodbolt/xania#310 regressed):\n{report}")
    await bot.close()


async def test_autoloot_of_a_corpse_with_something_in_it_survives(mud, names, found):
    """act_obj.cpp do_get (abraidotti/xania#1): autoloot runs ``get all corpse``, and for each object in the corpse
    the undroppable-item guard read ``arg1[3]`` of "all". Fixed upstream in mattgodbolt/xania#307. The director
    makes sure the mob carries something: a beggar given a small sword, killed by a level-30 bot with autoloot on."""
    bot = Bot(names(), "simpass1", host=mud.host, port=mud.port, transcript_dir=RUNS)
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
    # or the sword in the bot's inventory (looted, so the guard ran and survived).
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
    if crashed:
        report = await _regressed(mud, found, "python -m pytest scenarios/test_live_fixed.py -k autoloot_of_a_corpse",
                                  "A bot with autoloot on killed a mob carrying a sword (a regression check for "
                                  "xania#307).")
        pytest.fail(f"autoloot crashes xania again (mattgodbolt/xania#307 regressed):\n{report}")
    await bot.close()
    assert looted, "the beggar wasn't killed and looted in 45 s, so the check didn't run"


# DamageMessages.cpp: the verbs only DamageType::Fire uses (none of them is a generic, DamageType::None, verb).
FIRE_VERBS = {"touches", "hurts", "stings", "scalds", "blisters", "burns", "torches", "sizzles", "frazzles", "bakes",
              "grills", "roasts", "sears", "chars", "BRANDS", "MELTS", "ENGULFS", "NUKES", "ATOMIZES"}


# SkillTables.cpp: the damage noun each spell's messages use ("Your harm spell hits...", "Your spell hits...").
NOUNS = {"harm": "harm spell", "cause critical": "spell"}


async def _cast(bot: Bot, spell: str, mob: str) -> str:
    """What one cast at ``mob`` did: "unaffected", or the verb of the hit (else the reply, for the failure)."""
    noun = NOUNS[spell]
    r = await bot.do(f"cast '{spell}' {mob}", timeout=20,
                     expect=rf"unaffected by your {noun}|Your {noun} (?:does )?\W*\w+\W* (?:things to )?the [\w ]*{mob}|"
                            r"lost your concentration|"
                            r"aren't here|enough mana|You can't")
    if re.search(rf"unaffected by your {noun}", r.text):
        return "unaffected"
    # big hits: "<<<  ERADICATES  >>>", and the biggest: "does UNSPEAKABLE things to"
    m = re.search(rf"Your {noun} (?:does )?\W*(\w+)\W* (?:things to )?the [\w ]*{mob}", r.text)
    return m.group(1) if m else f"?? {r.text.strip()}"


async def test_harm_deals_harm_damage_like_the_rest_of_its_group(mud, names):
    """magic.cpp spell_harm dealt DamageType::Fire from d165e77 (abraidotti/xania#11) while the rest of the Harmful
    group (cause light/serious/critical) dealt Harm, which DamageTolerance.cpp checks against the Magic tolerance.
    #11's option A, restoring DamageType::Harm, was merged upstream as mattgodbolt/xania#312. Immunity shows the type
    exactly ("$N is unaffected by your harm"), so a controlled pair:

    - the snotling (13015, Flying Citadel, level 20): immune to Magic (its area flags), no Fire tolerance;
    - the red bat (4151, Moria, level 7): immune to Fire, no Magic tolerance;

    each cast at by a level-30 cleric with harm and cause critical at 100% (so no cast fails), in Temple Square.
    cause critical is the control, and harm must now behave like it: it can't touch the snotling, does hurt the bat,
    and doesn't use a fire verb. Each cast gets a freshly loaded mob, since one hit can kill the bat."""
    bot = Bot(names(), "simpass1", host=mud.host, port=mud.port, transcript_dir=RUNS)
    await bot.enter(Profile(cls="cleric"))
    d = await Director.create(mud)
    seen: dict[tuple[str, str], str] = {}
    try:
        await d.goto(3005)  # Temple Square: lit, not a safe room
        await d.bring(bot.name)
        await d.advance(bot.name, 30)
        for spell in ("harm", "cause critical"):
            await d.set_skill(bot.name, spell, 100)
        for vnum, mob in ((13015, "snotling"), (4151, "bat")):
            for spell in ("harm", "cause critical"):  # a fresh mob for each, the only one in the room
                await d.restore(bot.name)
                r = await d.do(f"load mob {vnum}", expect=r"Ok\.|No mob")
                assert "Ok." in r, r
                seen[mob, spell] = await _cast(bot, spell, mob)
                await d.peace()
                await d.do(f"purge {mob}", expect=None)
    finally:
        try:
            await d.peace()
            await d.restore(bot.name)
        finally:
            await d.close()
    await bot.close()
    assert not any(v.startswith("??") for v in seen.values()), f"a cast neither hit nor was shrugged off, so the " \
                                                               f"check didn't run: {seen}"
    # the control: Harm damage checks Magic
    assert seen["snotling", "cause critical"] == "unaffected", seen
    assert seen["bat", "cause critical"] != "unaffected", seen
    # harm, the same
    assert seen["snotling", "harm"] == "unaffected", (
        f"harm hurts a Magic-immune mob again: it deals Fire damage (mattgodbolt/xania#312 regressed). {seen}")
    assert seen["bat", "harm"] != "unaffected", seen
    assert seen["bat", "harm"] not in FIRE_VERBS, seen
