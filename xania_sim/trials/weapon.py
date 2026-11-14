"""The weapon trial: ``python -m xania_sim weapon``. Does a weapon flag do what it says, in a real fight?

A bot of some class and level wields a weapon and fights a punching bag: a level-1 beggar given 30,000 hit points,
so the fight lasts as long as the trial wants. From level 20 every damage message carries its amount (DamageMessages
.cpp make_damage_amount_label), both the hits the bot lands and the ones it takes, and the bot's prompt shows its
hit points after each round. So each round can be accounted for exactly:

    change in the bot's hp  =  drained  -  damage taken

and ``drained`` compared with what a model of the flag predicts from the bot's own hits. For Vampiric the models are
the code as it is, ``(damage / 100) * 10`` per hit (fight.cpp damage()), and a plain 10% (``damage / 10``). A round
that a regen tick lands in matches neither (the tick adds hit points, or clamps them back to max): those are counted
as ``unexplained`` rather than guessed at.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from xania_sim.core import world as W
from xania_sim.core.director import Director
from xania_sim.core.names import bot_names, mob_words
from xania_sim.core.server import TestMud
from xania_sim.core.session import Bot, Profile
from xania_sim.record.heartbeat import Heartbeat
from xania_sim.record.result import Outcome

ARENA = 3108  # Midgaard park, "Small path in the park": no resets, no room flags
BAG = 3065  # a beggar (Midgaard, level 1): hits for 1-3, so the bot never comes close to dying
BAG_HP = 30000  # act_wiz.cpp do_mset "hp": the most it allows
AMOUNT = re.compile(r"\((\d+)\)\s*$")
# (weapon vnum, level): the lowest-level vampiric weapon at 20 (the first level that shows damage amounts), and two
# high-level ones at their own levels, where hits start to pass 100.
DEFAULT_CASES = [(4100, 20), (10066, 65), (28021, 91)]

MODELS = {
    "code today: (damage / 100) * 10": lambda d: (d // 100) * 10,
    "10% of the damage: damage / 10": lambda d: d // 10,
}


def round_amounts(text: str) -> tuple[list[int], list[int]]:
    """The damage amounts in one round's output: (dealt by the bot, taken by the bot)."""
    dealt, taken = [], []
    for line in text.splitlines():
        line = line.strip()
        m = AMOUNT.search(line)
        if not m:
            continue
        if line.startswith(("You ", "Your ")):
            dealt.append(int(m.group(1)))
        elif " your " in line:
            taken.append(int(m.group(1)))
    return dealt, taken


def alignment_for(obj) -> str:
    """An alignment (g/n/e) the weapon doesn't zap (extra flags AntiGood/AntiEvil/AntiNeutral)."""
    from xania_explorer.areafile import flags as F

    anti = set(F.decode(obj.extra_flags, F.OBJ_EXTRA))
    for letter, flag in (("n", "AntiNeutral"), ("g", "AntiGood"), ("e", "AntiEvil")):
        if flag not in anti:
            return letter
    raise RuntimeError(f"{obj.short_descr} zaps every alignment")


@dataclass
class Case:
    vnum: int
    level: int
    weapon: str = ""
    flags: list[str] = field(default_factory=list)
    rounds: int = 0
    hits: list[int] = field(default_factory=list)
    hp_gained: int = 0  # sum over explained rounds of (hp change + damage taken), i.e. what was drained
    matches: dict[str, int] = field(default_factory=dict)  # model -> rounds it explains exactly
    predicted: dict[str, int] = field(default_factory=dict)  # model -> drain it predicts over all rounds
    unexplained: int = 0
    examples: list[str] = field(default_factory=list)
    error: str = ""

    def summary(self) -> dict:
        n = len(self.hits)
        return {"weapon": f"{self.weapon} (#{self.vnum})", "flags": self.flags, "level": self.level,
                "rounds": self.rounds, "hits": n, "mean_hit": round(sum(self.hits) / n, 1) if n else None,
                "max_hit": max(self.hits) if n else None, "hits_100_plus": sum(h >= 100 for h in self.hits),
                "drained_seen": self.hp_gained, "drain_predicted": self.predicted,
                "rounds_explained_by": self.matches, "rounds_unexplained": self.unexplained, "error": self.error}


async def _setup(d: Director, bot: Bot, world, case: Case, cls: str) -> None:
    obj = world.objects[case.vnum]
    await d.goto(ARENA)
    await d.do("purge", expect=r"Ok\.|\n")
    await d.bring(bot.name)  # first, as setup/stage.py does: advance wants the player where the director can see it
    r = await d.advance(bot.name, case.level)
    if "Raising a player" not in r:
        raise RuntimeError(f"couldn't advance the bot to {case.level}: {r.strip()[-160:]}")
    await d.set_skill(bot.name, "all", 100)
    # the starting kit's shield blocks two-handed weapons (and a shield costs the 5% no-shield bonus in one_hit)
    await bot.do("remove shield", expect=r"You stop using|You do not have|can't")
    r = await d.give_obj(bot.name, case.vnum, obj.keywords.split()[0])
    if "You give" not in r:
        raise RuntimeError(f"couldn't give the bot {obj.short_descr}: {r.strip()[-120:]}")
    words = " ".join(obj.keywords.split()[:2])
    await bot.do(f"wield '{words}'", expect=r"You wield|wield it|too heavy|must be level|can't|You need|"
                                            r"You do not have")
    # The reply alone can't be trusted: an anti-alignment weapon is wielded and then zapped out of the bot's hands
    # ("You are zapped by $p and drop it.", act_obj.cpp wear_obj), and output can arrive a prompt late.
    eq = await bot.do("equipment", expect=r"You are using:")
    using = eq.text.split("You are using:", 1)[-1]
    name = re.sub(r"\|.", "", obj.short_descr).lower()  # colour codes (|g ... |w) don't reach a no-ANSI bot
    if name not in using.lower():  # the slot label may be the object's own wear string, so match the name
        raise RuntimeError(f"the bot isn't wielding {obj.short_descr} (equipment: "
                           f"{' / '.join(ln.strip() for ln in using.splitlines() if ln.strip())[:200]})")
    await d.do(f"load mob {BAG}", expect=r"Ok\.|No mob")
    await d.set_char("beggar", "hp", BAG_HP)
    await d.restore("beggar")
    await d.restore(bot.name)
    await d.goto(3001)


async def _fight(bot: Bot, case: Case, rounds: int) -> None:
    r = await bot.do("kill beggar", expect=r"\(\d+\)|You miss|beggar|They aren't here")
    if "aren't here" in r.text:
        raise RuntimeError("the beggar isn't in the arena")
    last_hp = r.prompt.hp  # the first round (in the reply) is skipped: there's no hp before it to compare with
    pending = ""
    while case.rounds < rounds:
        out = await bot.listen(timeout=8)
        if not out.complete:
            raise RuntimeError("the fight stopped (no round in 8 s)")
        text = pending + out.text
        pending = ""
        if "is DEAD" in text:
            raise RuntimeError("the beggar died (its hit points weren't raised)")
        dealt, taken = round_amounts(text)
        if not dealt and not taken:
            last_hp = out.prompt.hp
            continue
        case.rounds += 1
        drained = out.prompt.hp - last_hp + sum(taken)
        last_hp = out.prompt.hp
        case.hits += dealt
        explained = False
        for name, model in MODELS.items():
            want = sum(model(h) for h in dealt)
            case.predicted[name] = case.predicted.get(name, 0) + want
            if drained == want:
                case.matches[name] = case.matches.get(name, 0) + 1
                explained = True
        if explained:
            case.hp_gained += drained
        else:
            case.unexplained += 1
        if len(case.examples) < 12 and dealt:
            case.examples.append(f"hits {dealt}, taken {taken}, hp {out.prompt.hp}: drained {drained}")


async def run_weapon(result, cases: list[tuple[int, int]] | None = None, rounds: int = 40, cls: str = "knight",
                     port: int = 9100) -> list:
    from xania_explorer.areafile import flags as F

    world = W.load()
    names = iter(bot_names("weapon", 200, mob_words(world)))
    runs = Path("runs") / time.strftime("weapon-%Y%m%d-%H%M%S")
    beat = Heartbeat(runs, "weapon")
    async with TestMud(port=port) as mud:
        d = await Director.create(mud)
        try:
            for vnum, level in cases or DEFAULT_CASES:
                obj = world.objects[vnum]
                case = Case(vnum, level, obj.short_descr, F.decode(int(obj.values[4]), F.WEAPON_FLAG))
                beat(f"{vnum}@{level}", "setting up")
                bot = Bot(next(names), "weaponpass1", host=mud.host, port=mud.port, transcript_dir=runs)
                try:
                    await bot.enter(Profile(cls=cls, alignment=alignment_for(obj)))
                    await _setup(d, bot, world, case, cls)
                    await _fight(bot, case, rounds)
                except Exception as e:  # noqa: BLE001 - report it and go on to the next case
                    case.error = f"{type(e).__name__}: {e}"
                finally:
                    await d.goto(ARENA)
                    await d.peace()
                    try:
                        await bot.quit()  # not close: a link-dead bot stays in the world
                    except Exception:  # noqa: BLE001
                        await bot.close()
                s = case.summary()
                beat(f"{vnum}@{level}", "done", **{k: v for k, v in s.items() if k != "flags"})
                best = max(case.matches, key=case.matches.get) if case.matches else None
                ok = None if case.error or not case.rounds else True
                text = (f"{case.weapon} at {cls} {level}: {case.rounds} rounds, {len(case.hits)} hits "
                        f"(mean {s['mean_hit']}, max {s['max_hit']}, {s['hits_100_plus']} of 100+), drained "
                        f"{case.hp_gained} HP; best model: {best}" if not case.error else
                        f"{case.weapon} at {cls} {level}: {case.error}")
                result.outcomes.append(Outcome("weapon", ok, text, case.examples, s))
                result.measurements[f"{vnum}@{level}"] = s
        finally:
            await d.close()
    errors = [o.summary for o in result.outcomes if o.activity == "weapon" and o.data.get("error")]
    if errors and len(errors) == len(cases or DEFAULT_CASES):
        raise RuntimeError(f"no case ran: {errors[0]}")  # "couldn't run" (exit 2), not a clean result
    return []
