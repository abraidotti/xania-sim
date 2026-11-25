"""Mob program triggers, as they behave today: a characterization net for abraidotti/xania#16, which moves the
MProg::*_trigger calls onto an event bus. Each test must pass both before and after that change, unchanged.

The programs are the area files' own (area/*.prg). Where a program fires by chance, the test repeats the action
until it does, with a budget that makes a spurious failure very unlikely (noted at each one).

ARENA is Midgaard's park path: no resets and no room flags, so only the mobs a test loads are there."""

import asyncio
import itertools
import re

import pytest_asyncio

from xania_sim.core.director import Director
from xania_sim.core.session import Profile

ARENA = 3108  # "Small path in the park"
LIMBO = 2  # Rooms::Limbo: where the director waits, out of the arena
PRACTICER = 3020  # practicer.prg: greet_prog 100, "You can practice your skills and spells with me, $N."
BEGGAR = 3065  # beggar.prg: bribe_prog 1/100/1000/10000, fight_prog 20, death_prog 75
JANITOR = 3061  # janitor.prg: act_prog "drops"
CRIER = 3144  # crier.prg: rand_prog 40 "cry", speech_prog "p What"
CITYGUARD = 3060  # mid_cit.prg: entry_prog 30, act_prog "p bursts into", speech_prog "hello hi"
OPPOSITE = {"north": "south", "south": "north", "east": "west", "west": "east", "up": "down", "down": "up"}
_marks = itertools.count()


@pytest_asyncio.fixture
async def d(mud):
    director = await Director.create(mud)
    yield director
    try:
        await director.do("peace", expect=None)
        await director.goto(ARENA)
        await director.do("purge", expect=None)
    finally:
        await director.close()


async def flush(bot) -> str:
    """Everything that reached the bot so far, read through the reply to a unique say."""
    mark = f"zqmark{next(_marks)}"
    return (await bot.do(f"say {mark}", expect=mark)).text


async def listen_for(bot, pattern: str, seconds: float) -> str:
    text = ""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end and not re.search(pattern, text):
        text += (await bot.listen(timeout=max(0.1, end - loop.time()))).text
    return text


async def arena_exit(d: Director) -> tuple[str, str]:
    """A way out of the arena and the way back in."""
    exits = re.search(r"\[Exits: ([^\]]*)\]", await d.goto(ARENA)).group(1).split()
    way = next(e for e in exits if e in OPPOSITE)
    return way, OPPOSITE[way]


async def load_in_arena(d: Director, vnum: int) -> None:
    await d.goto(ARENA)
    r = await d.do(f"load mob {vnum}", expect=r"Ok\.|No mob")
    assert "Ok." in r, r


async def test_greet_fires_after_the_room_and_followers_greet_first(d, new_bot):
    """move_char fires entry and greet last: after "has arrived", look_auto and every follower's own move."""
    leader, follower = await new_bot(Profile()), await new_bot(Profile())
    out, back_in = await arena_exit(d)
    await load_in_arena(d, PRACTICER)
    await d.goto(LIMBO)  # out of the way, so the director's own arrival isn't in the transcript
    await d.do(f"transfer {leader.name} {ARENA}", expect=r"Ok\.")
    await d.do(f"transfer {follower.name} {ARENA}", expect=r"Ok\.")
    for b in (leader, follower):
        await flush(b)

    # Arriving by transfer doesn't greet: only move_char and do_enter fire it.
    transferred = await listen_for(leader, "practice your skills", 2)
    assert "practice your skills" not in transferred, transferred

    await follower.do(f"follow {leader.name}", expect="You now follow")
    await leader.do(out, expect=r"\[Exits")
    await flush(leader)
    await flush(follower)
    r = await leader.do(back_in, expect=rf"with me, {follower.name}\.(.|\n)*with me, {leader.name}\.")
    text = r.text
    greet = "You can practice your skills and spells with me"
    room, arrived = text.find("[Exits:"), text.find(f"{follower.name} has arrived")
    follower_greet, leader_greet = text.find(f"{greet}, {follower.name}."), text.find(f"{greet}, {leader.name}.")
    assert 0 <= room < arrived < follower_greet < leader_greet, text


async def test_speech_bribe_and_give(d, new_bot):
    bot = await new_bot(Profile())
    await load_in_arena(d, CRIER)
    await load_in_arena(d, BEGGAR)
    await d.set_char(bot.name, "gold", 2000)
    await d.goto(LIMBO)
    await d.do(f"transfer {bot.name} {ARENA}", expect=r"Ok\.")
    await flush(bot)

    # speech_prog "p What" (a phrase anywhere in what's said)
    r = await bot.do("say What a day", expect=r"My Girlfriend left me")
    assert r.complete, r.text

    # bribe_prog: the highest threshold the amount reaches runs, once
    r = await bot.do("give 100 gold beggar", expect=r"Wow!  Thank you! Thank you!")
    assert r.complete, r.text
    assert "Oh my GOD" not in r.text, r.text
    r = await bot.do("give 1 gold beggar", expect=r"thanks you")
    assert r.complete and "Wow!" not in r.text, r.text


async def test_act_prog_runs_after_the_command(d, new_bot):
    """act_trigger only queues the message (Char::mpact). aggr_update runs the queue in the same pulse, after the
    command that caused it has finished, in occupied areas only."""
    bot = await new_bot(Profile())
    await load_in_arena(d, JANITOR)
    await d.force("janitor", "rest")  # a resting mob doesn't wander off
    await d.do(f"transfer {bot.name}", expect=r"Ok\.")
    r = await d.give_obj(bot.name, 3020, "dagger")  # a thin dagger
    assert "You give" in r, r
    await d.goto(LIMBO)
    await flush(bot)
    reaction = r"NOOGIES you|You are slapped by"
    r = await bot.do("drop dagger", expect=reaction, timeout=5)
    assert re.search(rf"You drop a thin dagger\.(.|\n)*({reaction})", r.text), r.text


async def test_rand_and_a_chain_of_triggers(d, new_bot):
    """crier rand_prog 40 "cry" -> cityguard act_prog "p bursts into": "What is wrong?" -> crier speech_prog
    "p What": "My Girlfriend left me". One random tick every 4 s: 20 ticks miss with chance 0.6^20 < 0.01%."""
    bot = await new_bot(Profile())
    await load_in_arena(d, CRIER)
    await load_in_arena(d, CITYGUARD)
    await d.goto(LIMBO)
    await d.do(f"transfer {bot.name} {ARENA}", expect=r"Ok\.")
    await flush(bot)
    text = await listen_for(bot, r"What is wrong\?(.|\n)*My Girlfriend left me", 80)
    assert "bursts into tears" in text, text
    assert re.search(r"What is wrong\?(.|\n)*My Girlfriend left me", text), text


async def test_entry_fires_when_the_mob_walks_in(d, new_bot):
    """mid_cit.prg entry_prog 30: 25 walks in all miss with chance 0.7^25 < 0.01%."""
    bot = await new_bot(Profile())
    out, back_in = await arena_exit(d)
    await load_in_arena(d, CITYGUARD)
    await d.goto(LIMBO)
    await d.do(f"transfer {bot.name} {ARENA}", expect=r"Ok\.")
    await flush(bot)
    text = ""
    for _ in range(25):
        # The director walks the guard out and back, staying with it: force finds a mob in the director's room
        # first, and other cityguards walk Midgaard.
        await d.goto(ARENA)
        await d.force("cityguard", out)
        await d.do(out, expect=r"\[Exits")
        await d.force("cityguard", back_in)
        await d.do(back_in, expect=r"\[Exits")
        text += await flush(bot)
        if "keeps an eye out for wanted criminals" in text:
            break
    assert "keeps an eye out for wanted criminals" in text, text


async def test_fight_and_death(d, new_bot):
    """beggar.prg fight_prog 20 runs once a combat round (3 s): 40 rounds all miss with chance 0.8^40 < 0.02%.
    death_prog 75 runs as the beggar dies, while it's still in the room: 8 beggars all miss with chance 0.25^8."""
    bot = await new_bot(Profile())
    await load_in_arena(d, BEGGAR)
    await d.do("set char beggar hp 30000", expect=None)
    await d.set_char(bot.name, "hp", 30000)
    await d.goto(LIMBO)
    await d.do(f"transfer {bot.name} {ARENA}", expect=r"Ok\.")
    await flush(bot)
    await bot.do("kill beggar", expect=r"beggar")
    text = await listen_for(bot, r"Help!  Please somebody help me!", 125)
    assert "Help!  Please somebody help me!" in text, text
    await d.do(f"at {ARENA} peace", expect=None)

    death = r"Now I go to a better place|Forgive me God for I have sinned"
    seen = ""
    for _ in range(8):
        await load_in_arena(d, BEGGAR)
        await d.goto(LIMBO)
        await d.do(f"at {ARENA} slay beggar", expect=r"You slay|They aren't|Slay")
        seen += await flush(bot)
        if re.search(death, seen):
            break
    assert re.search(death, seen), seen
