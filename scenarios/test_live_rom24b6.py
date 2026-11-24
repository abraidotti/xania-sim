"""Live checks for bugs fixed in ROM 2.4b6 (https://github.com/abraidotti/2.4b6-fixed) that Xania may still have,
and Xania-only bugs found while checking them. Each test asserts the correct behaviour, so it fails while the bug is
there.

Run against a Debug (ASan/UBSan) build so memory errors abort:
    XANIA_SIM_XANIA='$HOME/xania-verify' XANIA_SIM_RECORD=0 python -m pytest scenarios/test_live_rom24b6.py -v
"""

from __future__ import annotations

import asyncio
import itertools
import re
import shlex

import pytest
import pytest_asyncio

from xania_sim import Profile
from xania_sim.core.director import Director
from xania_sim.core.session import Bot

STAGE = 6001  # A trail through the light forest: no room flags
FAR = 6003  # A trail through the dense forest
PAD = 6004  # where the executioner test parks its padding mobs
LUMBERJACK = 6000  # John the Lumberjack, level 5, not aggressive
EXITS = r"\[Exits"
_marks = itertools.count()


@pytest_asyncio.fixture
async def d(mud):
    director = await Director.create(mud)
    await director.do("set skill directrix all 100", expect=None)
    await director.goto(STAGE)
    await flush(director.bot)
    yield director
    try:
        await director.do("peace", expect=None)
        await director.goto(STAGE)
        await director.do("purge", expect=None)
    finally:
        await director.close()


async def flush(bot) -> str:
    """Everything that reached the bot so far, read through the reply to a unique say."""
    mark = f"zqmark{next(_marks)}"
    return (await bot.do(f"say {mark}", expect=mark)).text


async def staged(d: Director, *bots) -> None:
    for b in bots:
        await d.bring(b.name)
    for b in bots:
        await flush(b)
    await flush(d.bot)


def gap(text: str, msg: str) -> int:
    """How many line ends follow ``msg`` in ``text`` before the next output."""
    i = text.find(msg)
    assert i >= 0, f"{msg!r} not in {text!r}"
    return len(re.match(r"\n*", text[i + len(msg):]).group())


async def listen_for(bot, pattern: str, seconds: float) -> str:
    text = ""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end and not re.search(pattern, text):
        text += (await bot.listen(timeout=max(0.1, end - loop.time()))).text
    return text


async def raw(bot, command: str, quiet: float = 1.5) -> str:
    """Send a command and read whatever comes back, for when the bot's prompt isn't in use (switched)."""
    await bot.conn.send(command)
    return await bot.conn.drain_output(quiet=quiet)


async def one_hp_lumberjack(d: Director, which: str = "lumberjack") -> None:
    await d.set_char(which, "hp", 1)
    await d.restore(which)


async def tough_lumberjack(d: Director, which: str = "lumberjack") -> None:
    await d.set_char(which, "hp", 30000)
    await d.restore(which)


# --- crashes and out-of-bounds reads ----------------------------------------------------------------------------


async def test_exotic_weapon_hits_leave_the_skill_tables_alone(mud, d, new_bot):
    """#53 (77509e0) / fight.cpp:469: one_hit passes an exotic weapon's sn, -1, to check_improve."""
    b = await new_bot()
    await staged(d, b)  # advance, like trust, only finds someone in the imm's room
    await d.advance(b.name, 20)
    await d.give_obj(b.name, 666, "broom")  # a broom: an exotic weapon, level 5
    r = await b.do("wield broom", expect=r"You wield|wield it|can't|must be")
    assert r.has("You wield"), r.text
    await d.do(f"load mob {LUMBERJACK}", expect=r"Ok\.")
    await tough_lumberjack(d)
    before = len(mud.log("xania"))
    await b.do("kill lumberjack", expect=r"Lumberjack")
    rounds = await listen_for(b, r"(?!)", 20)  # twenty seconds of rounds
    log = mud.log("xania")[before:]
    assert mud.alive(), mud.crash_report()
    assert re.search(r"Your pound \w+ John", rounds), f"no hits landed: {rounds[-500:]}"
    errors = [ln for ln in log.splitlines() if "runtime error" in ln or "Assertion" in ln]
    assert not errors, "\n".join(errors[:12])


async def test_char_list_survives_the_executioners_guards(mud, d, new_bot, names):
    """Xania only: spec_executioner's create_mobile pushes onto char_list inside mobile_update's range-for over it
    (update.cpp:244, special.cpp:654). Pad char_list to one short of its capacity (a power of two: it only grows by
    push_back and never shrinks), then let the executioner add his two guards."""
    # a THIEF: stealing from a player is refused ("player killing is not permitted"), so set the flag in the file
    name = names()
    first = await new_bot(name=name)
    await first.quit()
    pfile = f"{mud.data_dir}/player/{name.capitalize()}"
    q = shlex.quote(pfile)  # Act is saved as a number (save.cpp:102); PlrThief is Z, bit 25
    out = mud.run(f"v=$(awk '$1==\"Act\"{{print $2; exit}}' {q}); sed -i \"s/^Act .*/Act  $((v | 33554432))/\" {q} "
                  f"&& grep '^Act ' {q}", check=False)
    assert re.search(r"Act\s+\d+", out), out
    thief = Bot(name, "simpass1", host=mud.host, port=mud.port)
    await thief.enter()
    try:
        r = await thief.do("who", expect=thief.name)
        assert r.has("THIEF"), r.text
        await d.bring(thief.name)
        await d.advance(thief.name, 91)
        await d.set_char(thief.name, "hp", 30000)
        await d.restore(thief.name)
        await d.do(f"transfer {thief.name} {FAR}", expect=None)
        # Greedo has spec_executioner (MosEisley.are). Midgaard's Concordius (3011) is given it and then
        # spec_concordius, which wins.
        await d.do("load mob 5819", expect=r"Ok\.")

        async def chars() -> int:
            return int(re.search(r"Chars\s+(\d+)", await d.do("memory", expect=r"Chars")).group(1))

        # Pad to a few short of the boundary, then let the executioner act again and again: each time he adds two
        # guards from inside mobile_update, so the push that crosses the boundary is almost certainly his (only an
        # area reset could get there first, and the test then says so).
        n = await chars()
        cap = 1 << n.bit_length()  # the smallest power of two above n
        # the padding goes in a room of its own: a room listing a thousand hermits overflows a player's output
        while (c := await chars()) < cap - 6:
            for _ in range(cap - 6 - c):
                await d.do(f"at {PAD} load mob 301", expect=r"Ok\.")  # hermit Sorbus: sentinel, purgeable
        print(f"chars {n} -> {c}, capacity {cap}")
        await d.bring(thief.name)
        for turn in range(8):
            before = await chars()
            yell = await listen_for(thief, r"PROTECT THE INNOCENT", 20)
            await asyncio.sleep(1)
            assert mud.alive(), f"chars {before} before the executioner's turn {turn}:\n{mud.crash_report()}"
            if "PROTECT THE INNOCENT" not in yell:
                stat = await d.do("stat mob greedo", expect=r"Long  description")
                where = await thief.do("look", expect=EXITS)
                pytest.skip(f"the executioner never acted. The thief heard:\n{yell[-600:]}\nand sees:\n"
                            f"{where.text}\nGreedo:\n{stat}")
            after = await chars()
            print(f"turn {turn}: chars {before} -> {after}")
            await d.do("peace", expect=None)  # so he acts again: he doesn't while fighting
            if after > cap:
                if after - before != 2 or before > cap:
                    pytest.skip(f"something else crossed the boundary ({before} -> {after}, capacity {cap})")
                return  # his guards took char_list past its capacity inside mobile_update, and xania lived
        pytest.skip("char_list never crossed its capacity boundary")
    finally:
        if mud.alive():
            await d.do(f"at {PAD} purge", expect=None)
        await thief.quit()


# --- wrong behaviour ---------------------------------------------------------------------------------------------


async def test_group_gain_zaps_each_member_for_their_own_gear(d, new_bot):
    """#56 (f892bd4) / fight.cpp:1369: group_gain checks the killer's gear and alignment for every member."""
    killer, member = await new_bot(), await new_bot()
    await staged(d, killer, member)
    await d.advance(killer.name, 91)
    await d.advance(member.name, 10)
    await d.give_obj(member.name, 655, "dragonhelm")  # a golden dragonhelm: anti-evil
    r = await member.do("wear dragonhelm", expect=r"on your head|can't|zapped")
    assert r.has("on your head"), r.text
    await d.set_char(member.name, "align", -1000)
    await member.do(f"follow {killer.name}", expect=r"You now follow")
    await killer.do(f"group {member.name}", expect=r"joins your group|join")
    await d.do(f"load mob {LUMBERJACK}", expect=r"Ok\.")
    await one_hp_lumberjack(d)
    await killer.do("kill lumberjack", expect=r"Lumberjack")
    died = await listen_for(killer, r"is DEAD", 15)
    assert "is DEAD" in died, died[-500:]
    r = await member.do("equipment", expect=r"You are using|nothing")
    assert "dragonhelm" not in r.text, r.text


async def test_clone_has_the_parents_affects_once(d):
    """#66 (f1b506c) / db.cpp:1182: clone_mobile copies the stats, affects included, then adds the affects again."""
    await d.do(f"load mob {LUMBERJACK}", expect=r"Ok\.")
    await d.do("cast bless lumberjack", expect=None)
    await d.do("clone mob lumberjack", expect=r"You clone")
    stats = [re.search(r"Hit: (-?\d+)\s+Dam: (-?\d+)\s+Saves: (-?\d+)", await d.do(f"stat mob {i}.lumberjack",
                                                                               expect=r"Saves:")).groups()
             for i in (1, 2)]
    assert stats[0] == stats[1], f"clone and parent (Hit, Dam, Saves): {stats}"


async def test_clone_equips_its_copies_of_the_parents_gear(d):
    """#66 (1f0eeb7) / act_wiz.cpp:1291: do_clone sets worn_loc without equip_char."""

    async def armour(which: str) -> str:
        return re.search(r"Armor: (.*)", await d.do(f"stat mob {which}", expect=r"Armor:")).group(1)

    await d.do(f"load mob {LUMBERJACK}", expect=r"Ok\.")
    bare = await armour("lumberjack")
    await d.do("load obj 655", expect=r"Ok\.")
    await d.do("give dragonhelm lumberjack", expect=r"You give")
    await d.force("lumberjack", "wear dragonhelm")
    helmed = await armour("lumberjack")
    assert helmed != bare, f"the helm didn't change the parent's armour: {bare}"
    # now above the cloner's trust, so do_clone leaves it behind (char_can_clone_obj, act_wiz.cpp:81)
    r = await d.do("set obj dragonhelm level 101", expect=None)
    assert "Level: 101" in await d.do("stat obj dragonhelm", expect=r"Level:"), r
    await d.do("clone mob lumberjack", expect=r"You clone")
    both = sorted([await armour("1.lumberjack"), await armour("2.lumberjack")])
    assert both == sorted([bare, helmed]), \
        f"bare {bare!r}, helmed {helmed!r}; the clone, without the helm, should be bare: {both}"


async def test_rescue_refused_while_fighting_someone_else(d, new_bot):
    """#55 (3b6393a) / fight.cpp:2192: rescue never checks whether the rescuer is already fighting."""
    rescuer, friend = await new_bot(), await new_bot()
    await staged(d, rescuer, friend)
    for b in (rescuer, friend):
        await d.advance(b.name, 30)
    await d.set_skill(rescuer.name, "rescue", 100)
    await friend.do(f"follow {rescuer.name}", expect=r"You now follow")
    await rescuer.do(f"group {friend.name}", expect=r"joins your group|join")
    await rescuer.do("autoassist", expect=r"[Aa]utoassist")  # off, so each fights only its own lumberjack
    await friend.do("autoassist", expect=r"[Aa]utoassist")
    for _ in range(2):
        await d.do(f"load mob {LUMBERJACK}", expect=r"Ok\.")
    for i in (1, 2):
        await tough_lumberjack(d, f"{i}.lumberjack")
    # the players start the fights (forcing the mobs to kill didn't): each takes one lumberjack
    await rescuer.do("kill 1.lumberjack", expect=r"Lumberjack")
    await friend.do("kill 2.lumberjack", expect=r"Lumberjack")
    st = await d.do(f"stat mob {rescuer.name}", expect=r"Fighting:")
    sf = await d.do(f"stat mob {friend.name}", expect=r"Fighting:")
    assert "Fighting: john" in st and "Fighting: john" in sf, (st, sf)
    r = await rescuer.do(f"rescue {friend.name}", expect=r"You rescue|busy|Too late|isn't fighting|doesn't need|fail", timeout=30)
    # 2.4b6-fixed refuses with "You're busy fighting!"; anything else (a rescue, or a setup refusal) fails
    assert r.has("busy"), r.text


async def test_qui_warns_about_quit(new_bot):
    """#34 (3fecea3) / InterpreterImpl.cpp:209,275: quiet is registered before qui, so it wins the prefix."""
    b = await new_bot()
    r = await b.do("qui", expect=r"spell it out|quiet|hear")
    assert r.has("spell it out"), r.text


async def test_autosplit_leaves_a_pet_out(d, new_bot):
    """#46 (4a2eaa3) / Char.cpp:472: num_group_members_in_room counts charmed followers; split_coins doesn't."""
    b = await new_bot()
    await d.set_char(b.name, "gold", 100000)
    await d.goto(3031)  # the pet shop
    await staged(d, b)
    r = await b.do("buy kitten", expect=r"Enjoy your pet|afford|can't|isn't")
    assert r.has("Enjoy your pet"), r.text
    r = await b.do("autosplit", expect=r"[Aa]utosplit")
    if r.has("removed"):
        await b.do("autosplit", expect=r"[Aa]utosplit")
    await d.give_obj(b.name, 666, "broom")
    await b.do("drop broom", expect=r"You drop")
    r = await b.do("sacrifice broom", expect=r"for your sacrifice")
    assert "share the gold" not in r.text, r.text


async def test_string_with_a_bad_spec_keeps_the_mobs_own(d):
    """#65 (4a4dda1) / act_wiz.cpp:2247: the spec_lookup result is assigned before it's checked."""
    await d.do("load mob 3011", expect=r"Ok\.")  # Concordius: spec_executioner
    try:
        before = await d.do("stat mob concordius", expect=r"Long  description")
        assert "special procedure" in before, before
        r = await d.do("string char concordius spec nonsense", expect=r"No such spec fun|Ok|Syntax")
        assert "No such spec fun" in r, r
        after = await d.do("stat mob concordius", expect=r"Long  description")
        assert "special procedure" in after, after
    finally:
        await d.do("purge concordius", expect=None)


async def test_force_shows_the_command_as_typed(d, new_bot):
    """#65 (075bf3f) / act_wiz.cpp:2489: the forced command is formatted into act's format string."""
    b = await new_bot()
    await staged(d, b)
    await d.force(b.name, "say $n was here")
    t = await flush(b)
    assert "forces you to 'say $n was here'" in t, t


async def test_description_tilde_is_a_line_not_a_deletion(new_bot):
    """#36 (97f1d41) / act_info.cpp:1491: tildes are smashed before the "-" test, so "~" removes a line."""
    b = await new_bot()
    await b.do("description +A plain first line.", expect=r"Your description is")
    r = await b.do("description ~", expect=r"Your description is|no description")
    assert "A plain first line." in r.text, r.text


async def test_wimpy_refuses_a_word(new_bot):
    """Design question, #32 (73507f0) / act_info.cpp:1591: a word that isn't a number sets the default wimpy."""
    b = await new_bot()
    r = await b.do("wimpy abc", expect=r"[Ww]impy|courage|cowardice")
    assert not r.has("Wimpy set to"), r.text


# --- messages ----------------------------------------------------------------------------------------------------


async def test_infravision_shows_the_room_the_targets_eyes(d, new_bot):
    """#79 (9c3b335) / magic.cpp:2751: act's $n is the caster, not the target."""
    target, onlooker = await new_bot(), await new_bot()
    await staged(d, target, onlooker)
    await d.do(f"cast infravision {target.name}", expect=None)
    t = await flush(onlooker)
    assert "eyes glow red." in t, t
    assert f"{target.name}'s eyes glow red." in t, t


async def test_infravision_has_no_blank_line(d, new_bot):
    """#78 (2fc8756) / magic.cpp:2751: the act message ends in \\n\\r, and act adds its own."""
    target, onlooker = await new_bot(), await new_bot()
    await staged(d, target, onlooker)
    await d.do(f"cast infravision {target.name}", expect=None)
    t = await flush(onlooker)
    # the control has the same shape: a spell on the target whose room message is act(..., victim) without \n\r
    await d.do(f"cast 'giant strength' {target.name}", expect=r"heightened power|failed|stronger")
    control = await flush(onlooker)
    assert gap(t, "eyes glow red.") == gap(control, "heightened power."), (t, control)


async def test_follow_refusal_has_no_blank_line(d, new_bot):
    """#87 (f7848b6) / act_comm.cpp:419: \\n\\r inside act."""
    follower, loner = await new_bot(), await new_bot()
    await staged(d, follower, loner)
    await loner.do("nofollow", expect=r"follower")
    await flush(follower)
    r = await follower.do(f"follow {loner.name}", expect=r"followers")
    control = await follower.do("follow nobodyhere", expect=r"aren't here")
    assert gap(r.text, "followers.") == gap(control.text, "aren't here."), (r.text, control.text)


async def test_sitting_has_no_blank_line(d, new_bot):
    """Xania's own instances of #87's pattern: act_move.cpp:808 and :819."""
    sitter, onlooker = await new_bot(), await new_bot()
    await staged(d, sitter, onlooker)
    await sitter.do("sleep", expect=r"go to sleep|sleep")
    control = await flush(onlooker)
    await sitter.do("sit", expect=r"wake|sit")
    woke = await flush(onlooker)
    await sitter.do("stand", expect=r"stand")
    await flush(onlooker)
    await sitter.do("sit", expect=r"sit")
    sat = await flush(onlooker)
    expected = gap(control, "goes to sleep.")
    assert (gap(woke, "wakes and sits up."), gap(sat, "sits down on the ground.")) == (expected, expected), \
        (control, woke, sat)


async def test_brandish_and_zap_name_the_item_once(d):
    """#95 (fb5f459) / act_obj.cpp:1648,1732: "Your $p" with $p's article: "Your a small wand"."""
    await d.do(f"load mob {LUMBERJACK}", expect=r"Ok\.")
    await d.do("load obj 106", expect=r"Ok\.")  # a small wand, one charge
    await d.do("hold wand", expect=r"hold|You")
    text = await d.do("zap lumberjack", expect=r"explodes into fragments|fail")
    await d.do("peace", expect=None)
    await d.do("load obj 1371", expect=r"Ok\.")  # a black staff, two charges
    await d.do("hold staff", expect=r"hold|You")
    for _ in range(4):
        text += await d.do("brandish", expect=r"blazes bright|brandish|fail")
        if "blazes bright" in text:
            break
    assert re.search(r"explodes into fragments", text) and "blazes bright" in text, text
    assert not re.search(r"Your (a|an) ", text), text


async def test_cityguard_closes_its_quote(d, new_bot):
    """#81 (5bb06c1) / special.cpp:816: "PROTECT THE INNOCENT!!  BANZAI!!" never closes its quote."""
    b = await new_bot()
    await staged(d, b)
    await d.advance(b.name, 30)
    await d.set_char(b.name, "align", -1000)
    await d.do(f"load mob {LUMBERJACK}", expect=r"Ok\.")
    await tough_lumberjack(d)
    await d.do("load mob 3060", expect=r"Ok\.")  # the cityguard: spec_guard
    await b.do("kill lumberjack", expect=r"Lumberjack")
    text = await listen_for(b, r"BANZAI.*\n", 30)
    if "BANZAI" not in text:
        pytest.skip("the cityguard never acted")
    assert re.search(r"BANZAI!!'", text), text


async def test_shopkeeper_closes_its_quote(d, new_bot):
    """Like #95's shop tells (aa5cab6) / act_obj.cpp:2053: "I'm afraid I don't have enough gold to buy $p."."""
    b = await new_bot()
    await staged(d, b)
    await d.do("load mob 3004", expect=r"Ok\.")  # Razorblade the armourer
    try:
        await d.set_char("razorblade", "gold", 0)
        await d.give_obj(b.name, 3067, "cap")  # a hard leather cap, cost 100
        r = await b.do("sell cap", expect=r"tells you")
        m = re.search(r"tells you '(I'm afraid[^\n]*)", r.text)
        assert m, r.text
        assert m.group(1).endswith("'"), m.group(1)
    finally:
        await d.do("purge razorblade", expect=None)


async def test_stat_shows_a_players_practices_to_a_switched_imm(d, new_bot):
    """#97 (4a4f8d1) / act_wiz.cpp:924: Practices is 0 when the looker is an NPC, not the one looked at."""
    b = await new_bot()
    await staged(d, b)
    own = re.search(r"Practices: (\d+)", await d.do(f"stat mob {b.name}", expect=r"Practices:")).group(1)
    if own == "0":
        pytest.skip("a new character with no practices can't show the difference")
    await d.do(f"load mob {LUMBERJACK}", expect=r"Ok\.")
    switched = await raw(d.bot, "switch lumberjack")
    assert re.search(r"Ok|You", switched), switched
    try:
        text = await raw(d.bot, f"stat mob {b.name}")
    finally:
        await raw(d.bot, "return")
        await flush(d.bot)
    assert f"Practices: {own}" in text, f"the player has {own}; switched, the imm sees:\n{text}"


async def test_trust_says_ok(d, new_bot):
    """#99 (735e285) / act_wiz.cpp:1499: trust says nothing when it works."""
    b = await new_bot()
    await staged(d, b)  # trust only finds someone in the imm's room (act_wiz.cpp:1478)
    r = await d.do(f"trust {b.name} 5", expect=None)
    st = await d.do(f"stat mob {b.name}", expect=r"Practices:")
    assert "not here" not in r, r
    assert "Ok." in r, f"trust's reply: {r!r}; trust is now set? {'trust' in st}"


# --- found along the way: Xania only -----------------------------------------------------------------------------


async def test_followers_follow_through_a_portal(d, new_bot):
    """act_move.cpp:314: after char_to_room, ch->in_room == to_room always, so do_enter returns before followers."""
    b = await new_bot()
    await staged(d, b)
    r = await b.do(f"follow {d.bot.name}", expect=r"You now follow|follow")
    assert r.has("You now follow"), r.text
    for _ in range(8):  # a mob may save against it
        r = await d.do("cast portal lumberjack", expect=r"create a portal|failed|cannot|aren't here")
        if "create a portal" in r:
            break
    else:
        pytest.skip(f"couldn't make a portal: {r}")
    await d.do("enter portal", expect=EXITS)
    r = await flush(b)
    look = await b.do("look", expect=EXITS)
    assert "light forest" not in look.text, f"the leader went through; the follower saw:\n{r}\nand is still in:\n" \
                                            f"{look.text}"


# last: it kills the MUD while the bug is there (crash_watch restarts it)
async def test_permban_survives_an_unwritable_ban_file(mud, d):
    """#18 (70f5541) / Ban.cpp:158: Bans::save carries on through a null FILE*."""
    ban_file = f"{mud.data_dir}/system/ban.lst"
    mud.run(f"rm -f {ban_file} && mkdir -p {ban_file}")  # a directory: fopen("w") fails
    try:
        await d.do("permban example.com all", expect=r"Bans updated|Syntax|banned", timeout=5)
        await asyncio.sleep(1)
        assert mud.alive(), mud.crash_report()
    finally:
        mud.run(f"rmdir {ban_file} 2>/dev/null || true", check=False)
        if mud.alive():
            await d.do("allow example.com", expect=None)
