"""Combat tactics (M9): using the skills and spells a bot has, survival first.

A ``Tactics`` is built from the bot's class, level and what ``practice`` lists (trainer.parse_table: only what the
bot can use at its level, PracticeTabulator.cpp:14-18). It decides three things:

- **Buffs before a fight** (``prepare``): ``affect`` shows what's up (act_info.cpp:1113-1122: "Spell: 'armor' ...");
  missing buffs are cast best first while mana stays above the reserve plus a few attack casts.
- **The opener** (``opener``): backstab when the bot has it and a weapon (fight.cpp:2087-2140), else its strongest
  attack spell, else ``kill``.
- **Each round** (``choose``): heal when HP is low but above the flee line, rescue a groupmate being hit, else the
  action worth the most damage per round of lag: attack spells (chosen by expected damage per mana from Explorer's
  ``predict_spell`` at the bot's level and skill against the mob's level), kick, headbutt, trip and bash (a mob put
  down stays down for the rest of the fight and takes 1.5x melee damage: fight.cpp:517-518, 1130-1135), disarm and
  dirt kicking once each, berserk as a warrior's emergency heal.

Survival rules: a mana reserve is never spent on attacks or buffs (enough for ``word of recall`` and, for anyone with
a cure, that cure); a command typed in a wait state queues until it ends (MudImpl.cpp:303-307), so actions go out
only when the lag left is short (``LAG_SLACK``), and near the flee line only with no lag at all and only one-round
actions, so nothing stands between the bot and its flee; fleeing stays with ``fight()``. A skill that keeps failing is benched: three misses in a row end its use for the
fight, and an observed success rate under ``MIN_SUCCESS`` (after a skill-% prior) ends it for good.

Every use becomes a ``tactic`` event (events.Recorder.record): name, kind, target, ok, damage seen in the damage
messages (shown with amounts from level 20: DamageMessages.cpp make_damage_amount_label), mana spent, lag, and the
damage Explorer predicted, so trials can calibrate Explorer per skill and spell.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from xania_explorer import api

from .events import parse
from .session import Bot

PULSES_PER_SECOND = 4  # Constants.hpp:25
ROUND_PULSES = 12  # PULSE_VIOLENCE = 3 * PULSE_PER_SECOND (Constants.hpp:26): one combat round
LAG_SLACK = 8  # act when the prompt's wait (%c, PromptFormat.cpp:49) is at most this: the command queues for at
# most 2 s, clear of the next round (a cast's 12 beats leave about 7 at the round prompt after it)
RESERVE_SHARE = 0.12  # the mana reserve is at least this share of max mana
ATTACK_CASTS = 3  # buffs leave mana for this many casts of the best attack spell
HEAL_AT = 0.55  # heal mid-fight below this share of HP (and above the flee line)
HEAL_MIN = 0.08  # ... with a cure worth at least this share of max HP
NEAR_FLEE = 0.15  # within this share above the flee line, only one-round actions
FAIL_STREAK = 3  # failures in a row that bench a skill for the rest of the fight
MIN_SUCCESS = 0.25  # observed success rate (with the prior) below which a skill isn't used again
PRIOR_WEIGHT = 4  # the skill % counts as this many tries in the success estimate
REPLY_TIMEOUT = 3.5  # seconds to wait for an action's reply: a round, so a lost reply never delays a flee for long

# What each buff is worth (rough; higher first) and the reply that says it's already up. Effects and messages are
# magic.cpp spell_*: sanctuary divides damage taken by 1.6 (fight.cpp:664-665), haste gives an extra attack
# (fight.cpp:268) and speed in bash/trip/dirt, stone skin -40 AC, armor and shield -20 AC, frenzy hit/dam +level/6,
# bless hitroll +level/8, giant strength +str, protection evil -1/4 damage from evil attackers (fight.cpp:667-668).
BUFFS: dict[str, tuple[float, str]] = {
    "sanctuary": (10, r"already shrouded in a scintillating aura"),  # magic.cpp:3162
    "haste": (8, r"^(?!)"),  # magic.cpp:2489-2496 has no "already": it strips and recasts, so `affect` decides
    "stone skin": (6, r"cannot harden your skin any further"),  # magic.cpp:3271
    "frenzy": (5, r"already in a frenzy|Why don't you just relax|doesn't seem to like"),  # magic.cpp:2327-2341
    "shield": (4, r"already affected by a resplendent force shield"),  # magic.cpp:3212
    "armor": (4, r"already protected by more powerful armor"),  # magic.cpp:714
    "bless": (3, r"Greater blessings have already"),  # magic.cpp:742
    "giant strength": (3, r"already as strong as you can get"),  # magic.cpp:2427
    "protection evil": (2, r"Greater magic already protects you from evil"),  # magic.cpp:3049
}
BUFF_DONE = (r"You feel someone protecting you|You feel righteous|You are surrounded by a force shield|"
             r"You are surrounded by a white aura|You feel yourself moving more quickly|Your muscles surge|"
             r"Your skin turns to stone|You are filled with holy wrath|You feel protected from evil")
# heal on oneself: magic.cpp:1275 (cure light 1d8 + level/3), 1308 (serious 2d8 + level/2), 1242 (critical
# 3d8 + level - 6), 2586 (heal 2 x (3d8 + level - 6)); best first
HEALS: dict[str, object] = {
    "heal": lambda lvl: 2 * (13.5 + lvl - 6),
    "cure critical": lambda lvl: 13.5 + lvl - 6,
    "cure serious": lambda lvl: 9 + lvl / 2,
    "cure light": lambda lvl: 4.5 + lvl / 3,
}
RECALL_SPELL = "word of recall"
# attack spells left out: call lightning needs outdoors and rain but takes the mana first (magic.cpp:806-812);
# chain lightning bounces round the room (groupmates too); energy drain costs the caster alignment; exorcise and
# demonfire move the caster's alignment 50 a cast (magic.cpp:1359, 1387), halve their damage when caster and victim
# are within 100 of each other (1368, 1395) and turn on the caster past that (1354, 1382)
NOT_ATTACKS = {"call lightning", "chain lightning", "energy drain", "earthquake", "exorcise", "demonfire"}
# the noun a damage message names (SkillTables.cpp noun_damage), where it isn't the skill's own name
NOUNS = {"chill touch": "chilling touch", "cause light": "spell", "cause serious": "spell", "cause critical": "spell",
         "harm": "harm spell", "demonfire": "malice", "exorcise": "exorcism", "dirt kicking": "kicked dirt"}

# replies that mean "not now, not this target": the action is benched for the fight (fight.cpp, magic.cpp)
UNUSABLE = re.compile(r"You'll have to let \w+ get back up|is already down|feet aren't on the ground|"
                      r"already been blinded|There isn't any dirt|not wielding a weapon|You must wield a weapon|"
                      r"You need to wield a weapon|hurt and suspicious|can't backstab someone who's fighting|"
                      r"talon-like grip|Kill stealing|You can't get up enough energy|You get a little madder|"
                      r"too mellow to berserk|doesn't need rescuing|isn't fighting right now|Too late\.|"
                      r"You need bad weather|You must be outside|protects \w|does not seem to be affected|"
                      r"can't concentrate enough|They aren't here|Cast the spell on whom|But you aren't")
# replies that mean the bot doesn't have it after all: benched for good
UNKNOWN = re.compile(r"You don't know any spells of that name|What's that\?|You turn red in the face|"
                     r"leave the martial arts|You don't know how to disarm|You get your feet dirty|"
                     r"That might not be a good idea|^Huh\?", re.M)
# do_cast's refusals and failures (magic.cpp:591-637) and the spells' own (magic.cpp spell_*)
CAST_FAIL = (r"You lost your concentration|You don't have enough mana|You don't know any spells|"
             r"can't concentrate|Cast the spell on whom|They aren't here|Not on that target|You need bad weather|"
             r"You must be outside|protects \w|does not seem to be affected|turn upon you|You have been KILLED")
HEAL_DONE = r"You feel better|A warm feeling fills your body"  # magic.cpp:1245, 1278, 1311, 2589


@dataclass(frozen=True)
class Move:
    """A fighting skill: its command, what one success does on average, and the replies that say how it went."""

    name: str
    command: str  # formatted with {target}
    ok: str  # regex for a success, besides a damage line that isn't a miss (r"^(?!)": none)
    fail: str  # regex for a failure, besides damage lines that are all misses
    lag_fail: float  # lag after a failure, as a share of the skill's beats
    once: bool = False  # at most one success per fight
    starts: bool = False  # can open a fight


# fight.cpp do_kick (2362-2397: 1..level damage, beats 12), do_headbutt (2259-2321: level/3..level, beats 12, miss
# lags half), do_trip (1900-1992: 2..2+2*size, beats 24, miss lags 2/3), do_bash (1680-1779: 2..2+2*size+chance/20,
# beats 24; a miss puts the basher down and lags 3/2), do_disarm (2401-2471), do_dirt (1781-1898: the target
# argument is inverted, see below), do_rescue (2192-2245), do_berserk (1596-1678)
MOVES = {
    "kick": Move("kick", "kick", r"^(?!)", r"^(?!)", 1.0),
    "headbutt": Move("headbutt", "headbutt", r"Blood runs|blinded by the blood", r"dodges your headbutt", 0.5),
    "trip": Move("trip", "trip", r"^You trip ", r"^(?!)", 2 / 3, once=True),
    "bash": Move("bash", "bash", r"^You slam into", r"^You fall flat on your face", 1.5, once=True),
    "disarm": Move("disarm", "disarm", r"^You disarm ", r"fail to disarm|won't budge", 1.0, once=True),
    # do_dirt (fight.cpp:1797-1806) tests !args.empty() the wrong way round: with no argument it looks for a
    # character named "" ("They aren't here."), and with any argument it kicks dirt at the current opponent
    "dirt kicking": Move("dirt kicking", "dirt {target}", r"blinded by the dirt", r"^(?!)", 1.0, once=True),
    "berserk": Move("berserk", "berserk", r"Your pulse races", r"Your pulse speeds up", 3.0, once=True),
    "rescue": Move("rescue", "rescue {target}", r"^You rescue ", r"You fail the rescue", 1.0),
    "backstab": Move("backstab", "backstab {target}", r"^(?!)", r"^(?!)", 1.0, once=True, starts=True),
}


def mana_for_spell(level: int, learned_at: int, min_mana: int) -> int:
    """magic.cpp:578-583 (mana_for_spell)."""
    if level + 2 == learned_at:
        return 50
    return max(min_mana, 100 // max(1, 2 + level - learned_at))


AFFECT = re.compile(r"^(?:Spell|Skill): '(?P<name>[^']+)'", re.M)


def parse_affects(text: str) -> set[str]:
    """Names of what's affecting the bot, from ``affect`` (act_info.cpp:1113-1122)."""
    return {m["name"] for m in AFFECT.finditer(text)}


@dataclass
class Action:
    name: str
    kind: str  # spell, heal, buff, skill, melee
    command: str
    mana: int = 0
    lag: int = 0  # beats (pulses) on success
    expected: float = 0.0  # expected damage (or healing) of one use


@dataclass
class Use:
    action: Action
    ok: bool | None  # None: no telling (no reply seen)
    damage: int | None  # damage seen in the messages (None if none named it)
    mana: int  # mana spent, from the prompts
    lag: int  # the wait after it
    text: str


@dataclass
class _Fight:
    mob: int | None
    keyword: str
    rounds: float  # predicted rounds to kill
    dpr: float  # predicted melee damage per round
    done: set[str] = field(default_factory=set)  # once-per-fight moves that worked
    benched: set[str] = field(default_factory=set)
    streak: dict[str, int] = field(default_factory=dict)
    actions: int = 0


class Tactics:
    """``t = await Tactics.read(bot, "mage", 40, world)``; then ``fight(..., tactics=t)``."""

    def __init__(self, cls: str, level: int, skills: dict[str, int], world=None, alignment: int = 0,
                 group: tuple[str, ...] = (), recorder=None):
        self.cls, self.world, self.alignment, self.recorder = cls, world, alignment, recorder
        self.group = {g.capitalize() for g in group}
        self.stats: dict[str, list[int]] = {}  # name -> [tries, successes], across fights
        self.never: set[str] = set()  # skills the game says the bot doesn't have
        self.uses: list[Use] = []
        self._spell_cache: dict[tuple, tuple[float, int]] = {}
        self._fight = _Fight(None, "", 3.0, 0.0)
        self.update(level, skills)

    @classmethod
    async def read(cls, bot: Bot, cls_name: str, level: int, world=None, **kw) -> Tactics:
        from .trainer import SESSIONS, parse_table

        r = await bot.do("practice", expect=SESSIONS)
        return cls(cls_name, level, parse_table(r.text)[0], world, **kw)

    def update(self, level: int, skills: dict[str, int]) -> None:
        """New level or new percentages (after a level-up, practice, or use)."""
        self.level, self.skills = level, dict(skills)
        self.info = {a.name: a for a in api.abilities(self.cls)
                     if a.level <= level and skills.get(a.name, 0) > 0}
        self._spell_cache.clear()

    async def refresh(self, bot: Bot, level: int | None = None) -> None:
        from .trainer import SESSIONS, parse_table

        r = await bot.do("practice", expect=SESSIONS)
        self.update(level or self.level, parse_table(r.text)[0])

    # -- what things cost ----------------------------------------------------------------------------------------
    def knows(self, name: str) -> bool:
        return name not in self.never and (name in self.info or (name in MOVES and self.skills.get(name, 0) > 0))

    def mana(self, name: str) -> int:
        a = self.info.get(name)
        if a is None or not a.is_spell:
            return 50 if name == "berserk" else 0  # do_berserk takes 50 (fight.cpp:1617, 1637)
        return mana_for_spell(self.level, a.level, a.min_mana)

    def beats(self, name: str) -> int:
        a = self.info.get(name)
        return a.beats if a is not None else ROUND_PULSES

    def reserve(self, max_mana: int) -> int:
        """Mana never spent on attacks or buffs: a word of recall, and the best cure the bot knows."""
        need = self.mana(RECALL_SPELL) if self.knows(RECALL_SPELL) else 0
        cure = next((h for h in HEALS if self.knows(h) and self.mana(h) <= max_mana / 2), None)
        if cure is not None:
            need += self.mana(cure)
        return max(need, int(RESERVE_SHARE * max_mana))

    def success(self, name: str) -> float:
        """Estimated chance a use works: observed results with the skill % as a prior worth PRIOR_WEIGHT tries."""
        tries, wins = self.stats.get(name, (0, 0))
        prior = self.skills.get(name, 0) / 100
        return (wins + prior * PRIOR_WEIGHT) / (tries + PRIOR_WEIGHT)

    # -- spells ----------------------------------------------------------------------------------------------------
    def _mob(self, mob: int | None):
        return self.world.mobs.get(mob) if self.world is not None and mob is not None else None

    def spell_damage(self, name: str, mob: int | None) -> float:
        """Expected damage per cast attempt (Explorer predict_spell, at this skill %, against the mob's level)."""
        m = self._mob(mob)
        victim_level = m.level if m is not None else self.level
        key = (name, victim_level)
        if key not in self._spell_cache:
            try:
                p = api.predict_spell(self.world, self.cls, name, self.level, victim_level,
                                      skill=self.skills.get(name, 75))
                self._spell_cache[key] = (p.per_cast, p.beats)
            except (KeyError, ValueError, AttributeError, TypeError):
                self._spell_cache[key] = (0.0, ROUND_PULSES)
        return self._spell_cache[key][0]

    def _aligned(self, name: str, mob: int | None) -> bool:
        """Dispel evil and good only where they work (magic.cpp:1524-1536, 1547-1559: not cast by the same side,
        nothing on the neutral; good and evil are +/-350, Alignment.hpp:25-27)."""
        m = self._mob(mob)
        a = m.alignment if m is not None else 0
        me = self.alignment
        return {"dispel evil": me > -350 and a <= -350, "dispel good": me < 350 and a >= 350}.get(name, True)

    def attack_spells(self, mob: int | None) -> list[tuple[str, float, int]]:
        """(spell, expected damage per cast, mana), best damage per mana first."""
        if self.world is None:
            return []
        out = []
        for name, a in self.info.items():
            if not a.is_spell or name in NOT_ATTACKS or name in self._fight.benched or name in self.never:
                continue
            if not self._aligned(name, mob):
                continue
            dmg = self.spell_damage(name, mob)
            if dmg > 0:
                out.append((name, dmg, self.mana(name)))
        return sorted(out, key=lambda s: (-s[1] / max(1, s[2]), -s[1]))

    # -- buffs -----------------------------------------------------------------------------------------------------
    def buffs_wanted(self, up: set[str], mana: int, max_mana: int) -> list[str]:
        """Buffs to cast now, best first, within the mana above the reserve and a few attack casts."""
        spells = self.attack_spells(None)
        budget = mana - self.reserve(max_mana) - (ATTACK_CASTS * spells[0][2] if spells else 0)
        out = []
        for name in sorted(BUFFS, key=lambda b: -BUFFS[b][0]):
            if name in up or not self.knows(name) or (name == "frenzy" and "berserk" in up):
                continue
            cost = self.mana(name)
            if cost <= budget:
                out.append(name)
                budget -= cost
        return out

    async def prepare(self, bot: Bot) -> list[str]:
        """Cast missing buffs before a fight; returns the ones that took."""
        if not any(self.knows(b) for b in BUFFS):
            return []
        r = await bot.do("affect", expect=r"You are affected by")
        up = parse_affects(r.text)
        done = []
        for name in self.buffs_wanted(up, bot.prompt.mana, bot.prompt.max_mana):
            if bot.prompt.mana - self.mana(name) < self.reserve(bot.prompt.max_mana):
                break
            await unlag(bot)  # out of a fight there's time: let each cast's lag pass before the next
            act = Action(name, "buff", f"cast '{name}'", self.mana(name), self.beats(name))
            use = await self.perform(bot, act)
            if use.ok:
                done.append(name)
        return done

    # -- choosing --------------------------------------------------------------------------------------------------
    def begin(self, mob: int | None, keyword: str) -> None:
        """A new fight. Its length and the bot's melee damage per round come from Explorer's straight-fight model
        (they value trip and bash, whose worth grows with the rounds left)."""
        self._fight = _Fight(mob, keyword, 3.0, 0.0)
        m = self._mob(mob)
        if m is None:
            return
        try:
            f = api.predict_fight(self.world, self.cls, self.level, mob)
        except (KeyError, ValueError, AttributeError):
            return
        if 0 < f.rounds < float("inf"):
            self._fight.rounds = max(1.0, f.rounds)
            self._fight.dpr = m.hit.average / f.rounds

    def opener(self, prompt) -> Action:
        """How to start the fight: backstab, the strongest affordable attack spell, or plain ``kill``."""
        kw = self._fight.keyword
        if self.knows("backstab") and self.success("backstab") >= MIN_SUCCESS and "backstab" not in self.never:
            return Action("backstab", "skill", f"backstab {kw}", 0, self.beats("backstab"))
        spells = [s for s in self.attack_spells(self._fight.mob)
                  if prompt.mana - s[2] >= self.reserve(prompt.max_mana)]
        if spells:
            name, dmg, cost = max(spells, key=lambda s: s[1])
            return Action(name, "spell", f"cast '{name}' {kw}", cost, self.beats(name), dmg)
        return Action("kill", "melee", f"kill {kw}")

    def _usable(self, name: str) -> bool:
        f = self._fight
        if not self.knows(name) or name in f.benched or (MOVES[name].once and name in f.done):
            return False
        return self.success(name) >= MIN_SUCCESS and f.streak.get(name, 0) < FAIL_STREAK

    def _move_value(self, name: str, prompt) -> tuple[float, float]:
        """(expected damage, expected lag in pulses) of one use of a fighting skill."""
        lvl, p = self.level, self.success(name)
        mv = MOVES[name]
        beats = self.beats(name)
        lag = p * beats + (1 - p) * beats * mv.lag_fail
        left = max(1.0, self._fight.rounds - self._fight.actions)
        dmg = {"kick": (1 + lvl) / 2, "headbutt": (lvl / 3 + lvl) / 2, "trip": 4.0, "bash": 5.0}.get(name, 0.0)
        if name in ("trip", "bash"):
            dmg += 0.5 * self._fight.dpr * left  # down for the rest of the fight: 1.5x melee (fight.cpp:517-518)
        if name == "disarm":
            dmg = 0.3 * self._fight.dpr * left  # stands in for the mob's lost weapon damage (not modelled)
        if name == "dirt kicking":
            dmg = 3.5 + 0.1 * self._fight.dpr * left  # blind: hitroll -4 (fight.cpp:1885-1891)
        return p * dmg, lag

    def choose(self, prompt, flee_at: float = 0.35, ally: str | None = None) -> Action | None:
        """What to do this round, or None (lagged, or nothing worth it: melee goes on by itself)."""
        share = prompt.hp_share
        near = share < flee_at + NEAR_FLEE
        if prompt.wait > (0 if near else LAG_SLACK):
            return None  # it would queue (MudImpl.cpp:303-307); near the flee line nothing may stand before a flee
        reserve = self.reserve(prompt.max_mana)
        recall = self.mana(RECALL_SPELL) if self.knows(RECALL_SPELL) else 0
        # 1. heal: hurt but not fleeing; may dip into the reserve, but never below a recall
        if flee_at <= share < HEAL_AT:
            for h in HEALS:
                amount = HEALS[h](self.level)
                if self.knows(h) and prompt.mana - self.mana(h) >= recall and amount >= HEAL_MIN * prompt.max_hp:
                    return Action(h, "heal", f"cast '{h}'", self.mana(h), self.beats(h), amount)
        # 2. rescue a groupmate who's being hit, if the bot can take it
        if ally and share >= 0.6 and self._usable("rescue"):
            return Action("rescue", "skill", f"rescue {ally}", 0, self.beats("rescue"))
        # 3. berserk as a warrior's emergency heal (+2 x level HP, fight.cpp:1641) when hurt, never when near fleeing
        #    (a failure lags three rounds, fight.cpp:1671); it also divides every skill by 1.5 (Char.cpp:280-281)
        if (not near and share < 0.6 and self._usable("berserk") and prompt.mana - 50 >= reserve
                and self.success("berserk") >= 0.7):
            return Action("berserk", "skill", "berserk", 50, ROUND_PULSES, 2 * self.level)
        options: list[tuple[float, Action]] = []
        spells = [s for s in self.attack_spells(self._fight.mob) if prompt.mana - s[2] >= reserve]
        if spells:
            name, dmg, cost = spells[0]  # best damage per mana
            beats = self.beats(name)
            if not near or beats <= ROUND_PULSES:
                options.append((dmg * ROUND_PULSES / max(1, beats),
                                Action(name, "spell", f"cast '{name}'", cost, beats, dmg)))
        for name in ("kick", "headbutt", "trip", "bash", "disarm", "dirt kicking"):
            if not self._usable(name):
                continue
            dmg, lag = self._move_value(name, prompt)
            if dmg <= 0 or (near and max(lag, self.beats(name)) > ROUND_PULSES):
                continue
            cmd = MOVES[name].command.format(target=self._fight.keyword)
            options.append((dmg * ROUND_PULSES / max(1.0, lag), Action(name, "skill", cmd, 0, self.beats(name), dmg)))
        if not options:
            return None
        return max(options, key=lambda o: o[0])[1]

    # -- doing -----------------------------------------------------------------------------------------------------
    def _expect(self, act: Action) -> str:
        """The reply that tells how ``act`` went (Bot.do reads past combat rounds until it appears)."""
        noun = re.escape(NOUNS.get(act.name, act.name))
        if act.kind == "buff":
            return "|".join([BUFF_DONE, BUFFS[act.name][1], CAST_FAIL])
        if act.kind == "heal":
            return "|".join([HEAL_DONE, CAST_FAIL])
        if act.kind == "spell":
            return "|".join([rf"^Your {noun} ", rf"is unaffected by your {noun}", CAST_FAIL])
        mv = MOVES.get(act.name)
        parts = [rf"^Your {noun} ", rf"is unaffected by your {noun}", UNUSABLE.pattern, UNKNOWN.pattern,
                 r"You have been KILLED"]
        if mv is not None:
            parts += [mv.ok, mv.fail]
        if act.kind == "melee":
            parts = [r"\w"]
        return "|".join(p for p in parts if p != r"^(?!)")

    async def perform(self, bot: Bot, act: Action) -> Use:
        """Send one action, read how it went, learn from it and record it."""
        before = bot.prompt.mana
        r = await bot.do(act.command, expect=self._expect(act),
                         timeout=REPLY_TIMEOUT + bot.prompt.wait / PULSES_PER_SECOND)
        ok, damage = self.outcome(act, r.text)
        spent = max(0, before - r.prompt.mana)
        use = Use(act, ok, damage, spent, r.prompt.wait, r.text)
        self._learn(act, use)
        self.uses.append(use)
        self._fight.actions += 1
        if self.recorder is not None and ok is not None:
            m = self._mob(self._fight.mob)
            self.recorder.record("tactic", {
                "name": act.name, "kind": act.kind, "target": m.short_descr if m is not None else self._fight.keyword,
                "mob": self._fight.mob, "ok": ok, "damage": damage, "mana": spent, "lag": use.lag,
                "predicted": round(act.expected, 1), "skill": self.skills.get(act.name), "level": self.level})
        if "You fall flat on your face" in r.text:  # a missed bash leaves the basher resting (fight.cpp:1773)
            stand = await bot.do("stand", expect=r"You stand up|You are already|You're |Nah|You can't", timeout=12)
            use.text += stand.text
        return use

    def outcome(self, act: Action, text: str) -> tuple[bool | None, int | None]:
        """(worked?, damage seen) from the reply to one action."""
        if UNKNOWN.search(text) and act.kind != "buff":
            self.never.add(act.name)
            return None, None
        if UNUSABLE.search(text) or re.search(r"You don't have enough mana|You can't concentrate", text):
            self._fight.benched.add(act.name)
            return None, None
        noun = NOUNS.get(act.name, act.name)
        hits = [e.data for e in parse(text) if e.kind == "damage" and e.data["attack"] == noun]
        damage = sum(h["amount"] for h in hits) if hits else None
        if act.kind in ("spell", "heal", "buff"):
            if "You lost your concentration" in text:
                return False, 0 if act.kind == "spell" else None
            if act.kind == "buff":
                return bool(re.search(BUFF_DONE, text)), None
            if act.kind == "heal":
                return bool(re.search(HEAL_DONE, text)), None
            return (True if hits or "is unaffected by your" in text else None), damage
        mv = MOVES.get(act.name)
        if mv is None:
            return None, damage
        if re.search(mv.fail, text, re.M) or (hits and all(h["missed"] for h in hits)):
            return False, damage or 0
        if (mv.ok != r"^(?!)" and re.search(mv.ok, text, re.M)) or any(not h["missed"] for h in hits):
            return True, damage
        return None, damage

    def _learn(self, act: Action, use: Use) -> None:
        if use.ok is None or act.kind not in ("skill", "spell"):
            return
        s = self.stats.setdefault(act.name, [0, 0])
        s[0] += 1
        f = self._fight
        if use.ok:
            s[1] += 1
            f.streak[act.name] = 0
            if act.name in MOVES and MOVES[act.name].once:
                f.done.add(act.name)
        else:
            f.streak[act.name] = f.streak.get(act.name, 0) + 1


async def unlag(bot: Bot) -> None:
    """Wait out the lag the last prompt showed (a command sent now would only queue: MudImpl.cpp:303-307)."""
    if bot.prompt is not None and bot.prompt.wait > 0:
        await asyncio.sleep(bot.prompt.wait / PULSES_PER_SECOND + 0.1)


# who a mob is hitting, from the room's damage messages: "The goblin's slash scratches Bram's arm." (to_room,
# DamageMessages.cpp); used to spot a groupmate who needs rescuing
_HITS = re.compile(r"^(?!Your )(?P<mob>[^'\n]+?)'s [\w' ]+? (?P<who>[A-Z]\w+)'s [\w ]+[.!]", re.M)


def ally_in_trouble(text: str, group: set[str]) -> str | None:
    """The last groupmate seen being hit in ``text``, or None."""
    who = None
    for m in _HITS.finditer(text):
        if m["who"] in group:
            who = m["who"]
    return who
