"""Combat tactics with canned prompts and replies: buffs, the mana reserve, lag, spells by damage per mana, benching a
skill that keeps failing, and the events a use leaves behind."""

import re
import sqlite3
from types import SimpleNamespace as NS

import pytest
from xania_explorer.paths import default_xania_dir

from xania_sim import tactics as T
from xania_sim.events import Recorder, parse
from xania_sim.fight import fight
from xania_sim.parsers import Prompt
from xania_sim.session import Response

SRC = default_xania_dir() / "src"
pytestmark = pytest.mark.skipif(not (SRC / "magic.cpp").exists(), reason="needs the Xania source for skill tables")


def P(hp=400, max_hp=400, mana=300, max_mana=300, wait=0):
    return Prompt(hp, max_hp, mana, max_mana, 100, 100, 0, 0, wait)


def dice(avg):
    return NS(average=avg)


# no real area files: predict_spell only reads the source tables, and predict_fight's absence is tolerated
WORLD = NS(src_dir=SRC, mobs={1: NS(level=38, alignment=0, short_descr="the troll", hit=dice(600)),
                              2: NS(level=38, alignment=-1000, short_descr="the demon", hit=dice(600))})

MAGE = {"magic missile": 75, "burning hands": 75, "fireball": 75, "acid blast": 75, "armor": 75, "shield": 75,
        "stone skin": 75, "sanctuary": 75, "haste": 75, "word of recall": 75, "dagger": 75}
KNIGHT = {"kick": 75, "headbutt": 75, "trip": 75, "bash": 75, "sword": 75, "parry": 75}


class StubBot:
    """Replies by command (a list is consumed in order), each ending in the given prompt."""

    def __init__(self, replies: dict, prompt: Prompt, rounds=()):
        self.replies, self.prompt, self.sent = replies, prompt, []
        self.rounds = list(rounds)  # (text, prompt) handed out by listen()

    async def do(self, command, expect=None, timeout=10):
        self.sent.append(command)
        key = next((k for k in self.replies if re.fullmatch(k, command)), None)
        reply = self.replies.get(key, "Huh?\n")
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else "Huh?\n"
        if isinstance(reply, tuple):
            reply, self.prompt = reply
        return Response(command, reply, self.prompt)

    async def listen(self, timeout):
        if not self.rounds:
            return Response("", "", self.prompt, complete=False)
        text, self.prompt = self.rounds.pop(0)
        return Response("", text, self.prompt)


def test_mana_for_spell_follows_magic_cpp():
    assert T.mana_for_spell(40, 22, 15) == 15  # 100 / (2 + 18) = 5, raised to min_mana
    assert T.mana_for_spell(10, 7, 5) == 20  # 100 / (2 + 3)
    assert T.mana_for_spell(1, 1, 5) == 50


def test_buffs_best_first_and_skip_what_is_up():
    t = T.Tactics("mage", 40, MAGE, WORLD)
    assert t.buffs_wanted({"armor"}, 300, 300) == ["sanctuary", "haste", "stone skin", "shield"]
    assert t.buffs_wanted({"sanctuary", "haste", "stone skin", "shield", "armor"}, 300, 300) == []


def test_buffs_stop_at_the_reserve_and_leave_mana_for_attacks():
    t = T.Tactics("mage", 40, MAGE, WORLD)
    reserve = t.reserve(300)
    assert reserve == max(t.mana("word of recall"), int(T.RESERVE_SHARE * 300))
    # 150 mana: reserve 36, three fireballs 45: 69 left, not enough for sanctuary (75)
    wanted = t.buffs_wanted(set(), 150, 300)
    assert "sanctuary" not in wanted and wanted[0] == "haste"
    assert sum(t.mana(b) for b in wanted) <= 150 - reserve - 3 * 15
    assert t.buffs_wanted(set(), reserve + 10, 300) == []


def test_a_healer_keeps_a_cure_in_reserve():
    t = T.Tactics("cleric", 30, {"cure critical": 75, "heal": 75, "flamestrike": 75}, WORLD)
    assert t.reserve(100) == t.mana("heal")  # heal (50) fits in half the bar: it's the cure kept back
    # with only the reserve left, no attack spell
    assert t.choose(P(mana=t.reserve(100) + 5, max_mana=100)) is None


def test_attack_spell_chosen_by_damage_per_mana():
    t = T.Tactics("mage", 40, MAGE, WORLD)
    t.begin(1, "troll")
    names = [s[0] for s in t.attack_spells(1)]
    assert names[0] == "fireball" and names.index("acid blast") < names.index("magic missile")
    act = t.choose(P())
    assert act.kind == "spell" and act.command == "cast 'fireball'" and act.mana == 15
    # the opener is the hardest-hitting spell, aimed at the mob
    assert t.opener(P()).command == "cast 'acid blast' troll"


def test_alignment_spells_only_where_they_work():
    t = T.Tactics("cleric", 40, {"dispel evil": 75, "exorcise": 75, "demonfire": 75, "flamestrike": 75}, WORLD)
    assert [s[0] for s in t.attack_spells(1)] == ["flamestrike"]  # neutral troll: no dispel evil
    assert [s[0] for s in t.attack_spells(2)] == ["flamestrike", "dispel evil"]  # 3.8 vs 3.4 damage per mana
    # exorcise and demonfire shift the caster's alignment and can turn on it: never used


def test_lag_waits_and_near_the_flee_line_only_quick_actions():
    t = T.Tactics("knight", 40, {"trip": 75, "kick": 75}, WORLD)
    t.begin(1, "troll")
    t._fight.dpr = 60  # a long fight where tripping is worth a lot
    assert t.choose(P(wait=12)) is None  # lagged: a command would only queue (MudImpl.cpp:303-307)
    assert t.choose(P(wait=7)).name == "trip"  # the lag left after a cast at the next round: it queues briefly
    near = P(hp=int(400 * (0.35 + 0.05)))
    assert t.choose(near, flee_at=0.35).name == "kick"  # trip lags 24 pulses: not this close to fleeing
    assert t.choose(P(hp=near.hp, wait=2), flee_at=0.35) is None  # and nothing that would queue before a flee


def test_heal_comes_first_when_hurt_above_the_flee_line():
    t = T.Tactics("cleric", 30, {"heal": 75, "cure light": 75, "flamestrike": 75}, WORLD)
    act = t.choose(P(hp=180, max_hp=400, mana=200, max_mana=200), flee_at=0.35)
    assert act.kind == "heal" and act.name == "heal"
    # below the flee line the tactics don't heal: fleeing is fight()'s
    assert t.choose(P(hp=100, max_hp=400, mana=200, max_mana=200), flee_at=0.35).kind != "heal"


async def test_a_skill_that_keeps_failing_is_benched():
    t = T.Tactics("knight", 40, {"headbutt": 75, "kick": 75}, WORLD)
    t.begin(1, "troll")
    assert t.choose(P()).name == "headbutt"
    dodge = "The troll dodges your headbutt. You feel disoriented.\n"
    bot = StubBot({"headbutt": [dodge] * 3}, P())
    for _ in range(T.FAIL_STREAK):
        use = await t.perform(bot, t.choose(P()))
        assert use.ok is False
    assert t.choose(P()).name == "kick"
    t.begin(1, "troll")  # a new fight: the streak is forgotten but the record isn't
    assert t.success("headbutt") < 0.75


async def test_unusable_and_unknown_replies():
    t = T.Tactics("knight", 40, {"trip": 75, "kick": 75}, WORLD)
    t.begin(1, "troll")
    t._fight.dpr = 60
    bot = StubBot({"trip": "Its feet aren't on the ground.\n", "kick": "You better leave the martial arts to "
                   "fighters.\n"}, P())
    use = await t.perform(bot, t.choose(P()))
    assert use.ok is None and "trip" in t._fight.benched
    await t.perform(bot, t.choose(P()))
    assert "kick" in t.never and t.choose(P()) is None


async def test_uses_become_tactic_events_with_damage_and_mana():
    db = sqlite3.connect(":memory:")
    bot = StubBot({"cast 'fireball'": ("Your fireball ***  MUTILATES  *** the troll's head! (93)\n", P(mana=285))},
                  P())
    bot.name, bot.observers = "Wren", []
    rec = Recorder(db, bot, cls="mage")
    t = T.Tactics("mage", 40, MAGE, WORLD, recorder=rec)
    t.begin(1, "troll")
    use = await t.perform(bot, t.choose(P()))
    assert (use.ok, use.damage, use.mana) == (True, 93, 15)
    (kind, data), = db.execute("SELECT kind, data FROM events").fetchall()
    assert kind == "tactic" and '"damage": 93' in data and '"name": "fireball"' in data and '"mana": 15' in data


def test_damage_and_ability_lines_parse():
    ev = parse("Your kick misses the troll's left leg.\nYour fireball ***  MUTILATES  *** the troll's head! (93)\n"
               "You slam into the troll, and send it flying!\nYou fall flat on your face!\n"
               "The troll is unaffected by your chilling touch!\nYour pulse speeds up, but nothing happens.\n")
    assert [(e.kind, e.data.get("attack") or e.data.get("name")) for e in ev] == [
        ("damage", "kick"), ("damage", "fireball"), ("ability", "bash"), ("ability", "bash"),
        ("damage", "chilling touch"), ("ability", "berserk")]
    assert ev[0].data["missed"] and ev[1].data["amount"] == 93 and ev[1].data["victim"] == "the troll"
    # a small hit can read "misses" and still do damage (DamageMessages.cpp:183-195)
    [e] = parse("Your stab misses a wandering wolf's left thigh. (5)\n")
    assert not e.data["missed"] and e.data["amount"] == 5


def test_recorder_keeps_quiet_about_ability_and_damage_lines():
    db = sqlite3.connect(":memory:")
    bot = NS(name="Wren", observers=[])
    rec = Recorder(db, bot)
    rec.feed("Your slash scratches the troll's arm. (4)\nYou fail the rescue.\nThe troll is DEAD!!\n", None)
    assert [k for k, in db.execute("SELECT kind FROM events")] == ["kill"]


def test_an_ally_being_hit_is_spotted():
    text = "The troll's slash scratches Bram's left arm.\nYour slash misses the troll's head.\n"
    assert T.ally_in_trouble(text, {"Bram"}) == "Bram"
    assert T.ally_in_trouble(text, {"Hassan"}) is None


async def test_fight_uses_tactics_between_rounds():
    t = T.Tactics("knight", 40, {"kick": 75}, WORLD)
    bot = StubBot({"kill troll": "Your slash hits the troll's arm. (20)\n",
                   "kick": ("Your kick hits the troll's leg. (25)\n", P(hp=380, wait=12))},
                  P(), rounds=[("The troll's bite scratches you.\n", P(hp=370, wait=0)),
                               ("Your slash kills.\nThe troll is DEAD!!\nYou receive 300 experience points.\n",
                                P(hp=370)), ("", P(hp=370))])
    r = await fight(bot, 1, "troll", tactics=t)
    assert r.outcome == "won" and r.xp == 300
    assert bot.sent[:2] == ["kill troll", "kick"]
    assert t.uses[0].ok and t.uses[0].damage == 25


async def test_fight_without_tactics_is_unchanged():
    bot = StubBot({"kill troll": "Your slash hits the troll's arm. (20)\n"}, P(),
                  rounds=[("The troll is DEAD!!\n", P()), ("", P())])
    r = await fight(bot, 1, "troll")
    assert r.outcome == "won" and bot.sent == ["kill troll"]
