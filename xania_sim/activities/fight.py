"""Fighting and recovering, survival first: recognising mobs in a room, choosing only fights the bot should win,
following a fight through its prompts, fleeing early, and resting somewhere safe.

What bots learn from fights goes into the shared lore (table ``fights``): outcome, HP lost and xp per mob, class and
level. Observed results override Explorer's predictions once there are enough of them: a mob that has beaten bots
of this level band is off the list for everyone, whatever the model says.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

from xania_explorer import api

from xania_sim.record.rooms import EXITS, plain
from xania_sim.core.session import Bot, Response
from xania_sim.record.store import BAND_WIDTH, band_of

SAFE_SHARE = 0.5  # start a fight only if it's predicted (or seen) to cost under half the bot's HP
FLEE_AT = 0.35  # flee when HP falls below this share
REST_BELOW = 0.7  # rest before fighting again below this share of HP
MIN_FIGHTS = 3  # observations in a band before they override the prediction
BAD_RATE = 0.34  # deaths + flees share that makes a mob unsafe for the band

KILLED = "You have been KILLED"
DEAD = re.compile(r"^(?P<who>.+?) is DEAD!!", re.M)
XP = re.compile(r"You receive (\d+) experience points")
LEVEL_UP = "You raise a level"
# bash and trip leave the victim resting (fight.cpp do_bash/do_trip) and it stays down until it stands
KNOCKED = re.compile(r"sends you sprawling|trips you and you go down")
# signs that a bot that meant to rest is in a fight (an aggressive mob, or one that followed it)
ATTACKED = re.compile(r"You are already fighting|is here, fighting YOU|No way! +You are still fighting|"
                      r"You are stunned|mortally wounded|You are incapacitated")


def attacked(text: str, hp_before: int | None = None, hp_now: int | None = None) -> bool:
    """True if ``text`` (or HP going down while resting) says the bot is being attacked."""
    return bool(ATTACKED.search(text)) or (hp_before is not None and hp_now is not None and hp_now < hp_before)
NOT_HERE = re.compile(r"They aren't here|You can't|Kill whom|Not on that target|You must MURDER")
_TAGS = re.compile(r"^(?:\([^)]*\) )+")
_POSED = re.compile(r"^(?P<short>.+?) is (?:here, fighting|standing|sitting|resting|sleeping|fighting|stunned|lying)\b")

FIGHTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS fights (
    id INTEGER PRIMARY KEY, mob INTEGER NOT NULL, bot TEXT NOT NULL, cls TEXT NOT NULL, level INTEGER NOT NULL,
    outcome TEXT NOT NULL, rounds INTEGER, hp_before INTEGER, hp_after INTEGER, max_hp INTEGER, xp INTEGER,
    at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS fights_mob ON fights (mob, level);
"""


class MobIndex:
    """Recognises mobs from what a room shows: their long description, or "<short> is <position>"."""

    def __init__(self, world):
        self.world = world
        self.by_long: dict[str, list[int]] = {}
        self.by_short: dict[str, list[int]] = {}
        for v, m in world.mobs.items():
            first = next((ln.strip() for ln in plain(m.long_descr).split("\n") if ln.strip()), "")
            if first:
                self.by_long.setdefault(first, []).append(v)
            self.by_short.setdefault(plain(m.short_descr).strip().lower(), []).append(v)

    def keyword(self, vnum: int) -> str:
        return self.world.mobs[vnum].keywords.split()[0]

    def in_view(self, text: str, here: int | None = None) -> list[int]:
        """Mob vnums shown after the exits line of a room view (one entry per mob seen)."""
        m = list(EXITS.finditer(text))
        lines = text[m[-1].end():].split("\n") if m else text.split("\n")
        out = []
        for raw in lines:
            line = _TAGS.sub("", raw.strip())
            if not line:
                continue
            cands = self.by_long.get(line)
            if cands is None and (p := _POSED.match(line)):
                cands = self.by_short.get(p.group("short").lower())
            if cands:
                out.append(self._pick(cands, here))
        return out

    def _pick(self, cands: list[int], here: int | None) -> int:
        if len(cands) == 1 or here is None:
            return cands[0]
        area = self.world.room_area.get(here)
        local = [v for v in cands if self.world.mob_area.get(v) is area]
        return (local or cands)[0]


@dataclass
class FightResult:
    mob: int
    outcome: str  # won, fled, died, absent, ended
    rounds: int
    hp_before: int
    hp_after: int
    max_hp: int
    xp: int
    levelled: bool
    text: str
    mana_before: int = 0
    mana_after: int = 0


class FightLog:
    """What bots have seen fighting each mob, shared through the lore database."""

    def __init__(self, db, clock=time.time):
        self.db, self.clock = db, clock
        db.executescript(FIGHTS_SCHEMA)

    def record(self, bot: str, cls: str, level: int, r: FightResult) -> None:
        with self.db:
            self.db.execute("INSERT INTO fights (mob, bot, cls, level, outcome, rounds, hp_before, hp_after, max_hp, "
                            "xp, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (r.mob, bot, cls, level, r.outcome, r.rounds, r.hp_before, r.hp_after, r.max_hp, r.xp,
                             self.clock()))

    def deaths(self, mob: int, level: int) -> int:
        """How many bots of ``level``'s band this mob has killed."""
        band = band_of(level)
        lo, hi = band * BAND_WIDTH + 1, (band + 1) * BAND_WIDTH
        return self.db.execute("SELECT COUNT(*) FROM fights WHERE mob = ? AND level BETWEEN ? AND ? AND "
                               "outcome = 'died'", (mob, lo, hi)).fetchone()[0]

    def seen(self, mob: int, level: int) -> tuple[int, float, float]:
        """(fights, share that ended in death or flight, mean share of max HP lost) in ``level``'s band."""
        band = band_of(level)
        lo, hi = band * BAND_WIDTH + 1, (band + 1) * BAND_WIDTH
        rows = self.db.execute("SELECT outcome, hp_before, hp_after, max_hp FROM fights WHERE mob = ? AND level "
                               "BETWEEN ? AND ? AND outcome IN ('won', 'fled', 'died')", (mob, lo, hi)).fetchall()
        if not rows:
            return 0, 0.0, 0.0
        bad = sum(o in ("fled", "died") for o, *_ in rows) / len(rows)
        lost = sum(max(0, b - a) / m for _, b, a, m in rows if m) / len(rows)
        return len(rows), bad, lost


def safe_targets(world, cls: str, level: int, log: FightLog | None = None, caster: bool = False) -> dict[int, float]:
    """Mobs worth fighting at this level, by expected xp per round: predicted to cost under SAFE_SHARE of HP (or
    proven safe by enough observed wins), and never ones that observations show beating bots of this band.
    ``caster``: the bot buffs itself and casts its attack spells (tactics on), as in Explorer's caster model."""
    from xania_explorer.assess import combat as CB

    out = {}
    for m in CB.analyse(world).mobs:
        if not level - 4 <= m.mob.level <= level + 3 or m.info.population <= 0:
            continue
        try:
            f = api.predict_fight(world, cls, level, m.mob.vnum, caster=caster)
        except (KeyError, ValueError):
            continue
        safe = not f.deadly and f.hp_lost_share < SAFE_SHARE and f.rounds != float("inf") and f.xp > 0
        if log is not None:
            n, bad, lost = log.seen(m.mob.vnum, level)
            if n >= MIN_FIGHTS:
                safe = bad < BAD_RATE and lost < SAFE_SHARE
            if log.deaths(m.mob.vnum, level):
                safe = False  # survival first: one death in this band is enough
        if safe:
            out[m.mob.vnum] = f.xp / max(1.0, f.rounds)
    return out


async def _open(bot: Bot, keyword: str, tactics) -> Response:
    """The first blow: the tactics' opener (backstab, an attack spell), or ``kill`` if there's none or it was refused
    (e.g. "hurt and suspicious", fight.cpp:2127)."""
    if tactics is not None:
        act = tactics.opener(bot.prompt)
        if act.kind != "melee":
            use = await tactics.perform(bot, act)
            if use.ok is not None or KILLED in use.text or DEAD.search(use.text):
                return Response(act.command, use.text, bot.prompt)
            if NOT_HERE.search(use.text) and "They aren't here" in use.text:
                return Response(act.command, use.text, bot.prompt)
    return await bot.do(f"kill {keyword}", expect=r"\w", timeout=5)


async def fight(bot: Bot, mob: int, keyword: str, flee_at: float = FLEE_AT, escape=None,
                round_timeout: float = 9, tactics=None, engaged: bool = False) -> FightResult:
    """Attack and follow the fight to its end through the prompts it produces. ``escape`` (e.g. Navigator.escape)
    is awaited when HP falls below ``flee_at``. With ``tactics`` (tactics.Tactics) the bot opens with its best
    opener and uses skills and spells between rounds; fleeing stays as it is, and is checked first every round."""
    p0 = bot.prompt
    if tactics is not None:
        tactics.begin(mob, keyword)
    if engaged:  # already fighting (it attacked first): follow the fight, don't open one (a hidden mob can't be named)
        text, rounds = "", 0
    else:
        r = await _open(bot, keyword, tactics)
        text, rounds = r.text, 0
    if not engaged and NOT_HERE.search(r.text) and not DEAD.search(r.text):
        return FightResult(mob, "absent", 0, p0.hp, bot.prompt.hp, bot.prompt.max_hp, 0, False, text,
                           p0.mana, bot.prompt.mana)
    outcome = "ended"
    last_hp, worst_round = p0.hp, 0
    acted = False  # whether the last pass through the loop used a skill (then the round still has to come)
    while True:
        if KILLED in text:
            outcome = "died"
            break
        if DEAD.search(text):
            outcome = "won"
            more = await bot.listen(2)  # the xp and level-up lines can follow the death cry
            text += more.text
            break
        hp = bot.prompt.hp
        worst_round = max(worst_round, last_hp - hp)
        last_hp = hp
        # flee below the threshold, or when one more round like the worst so far would leave the bot near death
        if bot.prompt.hp_share < flee_at or hp - worst_round <= 0.15 * bot.prompt.max_hp:
            if escape is not None:
                outcome = await escape() or "fled"
                outcome = {"calm": "ended"}.get(outcome, outcome)
            else:
                r = await bot.do("flee", expect=r"flee|PANIC|aren't fighting|stunned|KILLED")
                outcome = "died" if KILLED in r.text else "fled"
            break
        if tactics is not None and not acted:
            act = tactics.choose(bot.prompt, flee_at, ally_hit(text[-600:], tactics))
            if act is not None:
                use = await tactics.perform(bot, act)
                text += use.text
                acted = True
                continue  # check for a death, and the flee line, before waiting on the next round
        acted = False
        heard = await bot.listen(round_timeout)
        if not heard.complete:
            break  # no rounds for a while: the fight is over (or never started)
        text += heard.text
        rounds += 1
        if KNOCKED.search(heard.text):
            text += (await bot.do("stand", expect=REST_REPLY)).text  # keep any round that arrives meanwhile
    xp = sum(int(x) for x in XP.findall(text))
    return FightResult(mob, outcome, rounds, p0.hp, bot.prompt.hp, bot.prompt.max_hp, xp, LEVEL_UP in text, text,
                       p0.mana, bot.prompt.mana)


def ally_hit(text: str, tactics) -> str | None:
    if not tactics.group:
        return None
    from xania_sim.activities.tactics import ally_in_trouble

    return ally_in_trouble(text, tactics.group)


REST_REPLY = r"^You (rest|sit down|stand up|stop resting|wake|are already|go to sleep|can't)|^You're |Nah"


async def recover(bot: Bot, hp: float = 0.95, mana: float = 0.8, timeout: float = 900, on_attack=None) -> bool:
    """Sleep until HP and mana are back (regen comes on ticks, about every 30-60 s), then stand. If the bot is
    attacked meanwhile it stops at once, stands and awaits ``on_attack()`` (e.g. Navigator.escape); returns False
    then, True if it rested in peace."""
    p = bot.prompt

    def done():
        return p.hp >= hp * p.max_hp and (p.max_mana == 0 or p.mana >= mana * p.max_mana)

    if done():
        return True
    r = await bot.do("sleep", expect=REST_REPLY + "|You are already fighting")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    peaceful = not attacked(r.text)
    while peaceful and loop.time() < deadline:
        before = p.hp
        await asyncio.sleep(8)
        r = await bot.do("time", expect="o'clock")
        p = r.prompt
        if attacked(r.text, before, p.hp):
            peaceful = False
        elif done():
            break
    await bot.do("wake", expect=REST_REPLY)
    await bot.do("stand", expect=REST_REPLY)
    if not peaceful and on_attack is not None:
        await on_attack()
    return peaceful


@dataclass(frozen=True)
class Comparison:
    """One mob against one class at one level: what Explorer predicted next to what bots saw."""

    mob: int
    name: str
    cls: str
    level: int
    fights: int
    won: float  # share of fights won
    lost_share: float  # mean share of max HP lost
    xp: float  # mean xp from a win
    predicted_lost_share: float | None
    predicted_xp: float | None
    predicted_deadly: bool | None


def compare(world, log: FightLog, min_fights: int = 1) -> list[Comparison]:
    """Observed fights grouped by mob, class and level, beside ``predict_fight`` for the same; most fights first."""
    rows = log.db.execute(
        "SELECT mob, cls, level, COUNT(*), AVG(outcome = 'won'), "
        "AVG(CASE WHEN max_hp > 0 THEN MAX(0, hp_before - hp_after) * 1.0 / max_hp END), "
        "AVG(CASE WHEN outcome = 'won' THEN xp END) FROM fights WHERE outcome IN ('won', 'fled', 'died') "
        "GROUP BY mob, cls, level HAVING COUNT(*) >= ? ORDER BY COUNT(*) DESC", (min_fights,)).fetchall()
    out = []
    for mob, cls, level, n, won, lost, xp in rows:
        try:
            f = api.predict_fight(world, cls, level, mob)
            p = (f.hp_lost_share, float(f.xp), f.deadly)
        except (KeyError, ValueError):
            p = (None, None, None)
        name = plain(world.mobs[mob].short_descr) if mob in world.mobs else f"mob {mob}"
        out.append(Comparison(mob, name, cls, level, n, won or 0.0, lost or 0.0, xp or 0.0, *p))
    return out
