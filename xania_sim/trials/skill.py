"""The skill trial: ``python -m xania_sim skill NAME``. Does a skill or spell work the way the game says it does?

For each class that learns it (Explorer's class tables: ``api.abilities``), at the level it's learned (and any
``--levels``), on a test MUD:

1. **Stage** a bot: that level, shop gear, every class skill up to that level at 75% (setup.stage).
2. **Practise**: the skill set to 1%, a few practice sessions at the nearest practice mob. Finding if the game
   won't let the class practise it at that level ("You can't practice that", act_info.cpp do_practice).
3. **Use**: the skill at 75%, then fights in an empty park room against a mob the bot is predicted to beat, with
   tactics limited to this one skill (combat skills, attack spells, buffs, heals); anything else is used once on
   the bot itself out of combat. Findings if the game says the bot doesn't know it at the level it's learned, or
   if a use gets no reply at all (the "missing fail message" kind of bug).
4. **Measure**: success rate, damage seen against Explorer's prediction (spells), mana and lag, the messages seen
   for success and failure, the practice gain per session, and the gain from use.

Damage that differs from the prediction is a measurement, not a finding: it calibrates Explorer.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from xania_explorer import api

from xania_sim.activities import practice as P
from xania_sim.activities.fight import fight, safe_targets
from xania_sim.activities.move import ROOM_REPLY, Navigator
from xania_sim.activities.tactics import BUFFS, HEALS, MOVES, UNKNOWN, Tactics
from xania_sim.core import world as W
from xania_sim.core.director import Director
from xania_sim.core.names import bot_names, mob_words
from xania_sim.core.server import TestMud
from xania_sim.core.session import Bot
from xania_sim.record.findings import Finding, bug_line_findings
from xania_sim.record.result import Outcome
from xania_sim.record.rooms import Fingerprints

ARENA = 3108  # Midgaard park, "Small path in the park": no resets, no room flags
TEMPLE = 3001
DAMAGE_SHOWN = 20  # damage amounts appear in the messages from this level (DamageMessages.cpp make_damage_amount_label)
PRACTICE_SESSIONS = 3
SELF_REPLY = re.compile(r"\S")  # anything at all counts as a reply


@dataclass
class ClassRun:
    cls: str
    level: int
    staged: bool = False
    practice_from: int | None = None
    practice_to: int | None = None
    practice_reply: str = ""
    uses: int = 0
    successes: int = 0
    silent: int = 0
    damage_seen: list[int] = field(default_factory=list)
    damage_predicted: float | None = None
    mana: list[int] = field(default_factory=list)
    lag: list[int] = field(default_factory=list)
    messages: dict[str, list[str]] = field(default_factory=dict)  # ok / failed / unclear -> distinct lines
    percent_before_use: int | None = None
    percent_after_use: int | None = None
    fights: list[str] = field(default_factory=list)
    unknown: str = ""

    def summary(self) -> dict:
        n = self.uses
        return {"level": self.level, "uses": n, "success_rate": round(self.successes / n, 2) if n else None,
                "damage_seen": round(sum(self.damage_seen) / len(self.damage_seen), 1) if self.damage_seen else None,
                "damage_predicted": None if self.damage_predicted is None else round(self.damage_predicted, 1),
                "mana": round(sum(self.mana) / len(self.mana), 1) if self.mana else None,
                "practice": f"{self.practice_from}% -> {self.practice_to}%" if self.practice_to is not None else None,
                "use_gain": (self.percent_after_use - self.percent_before_use)
                if None not in (self.percent_after_use, self.percent_before_use) else None,
                "fights": ", ".join(self.fights), "silent_uses": self.silent}


def learners(skill: str) -> dict[str, int]:
    """{class: level it's learned at} for the classes that learn it as their own (a rating above 0 in the skill
    table, skills.cpp:574-577); cross-class training at level 60 (skills.cpp:37) doesn't count."""
    from xania_explorer.source.cpp_tables import CLASSES

    from xania_sim.setup.loadouts import load_skills

    s = load_skills().get(skill)
    if s is None:
        return {}
    every = {cls: max(1, s.levels[i]) for i, cls in enumerate(CLASSES) if s.ratings[i] > 0}
    # many tables give every other class a skill at 60, the same level as cross-class training: by default test the
    # classes it really belongs to, unless that's all there is
    own = {c: lv for c, lv in every.items() if lv < CROSS_CLASS}
    return own or every


CROSS_CLASS = 60  # skills.cpp:37 LevelCrossClassTraining


def kind_of(skill: str, is_spell: bool) -> str:
    if skill in MOVES:
        return "combat skill"
    if skill in BUFFS:
        return "buff"
    if skill in HEALS:
        return "heal"
    return "spell" if is_spell else "skill"


def _target(world, cls: str, level: int, caster: bool) -> int | None:
    """The highest-level mob the bot is predicted to beat comfortably, that the director can load."""
    safe = safe_targets(world, cls, level, caster=caster)
    mobs = [m for m in safe if m in world.mobs]
    return max(mobs, key=lambda m: (world.mobs[m].level, safe[m]), default=None)


async def _percent(bot, skill: str) -> int | None:
    r = await bot.do("practice", expect=P.SESSIONS)
    return P.parse_table(r.text)[0].get(skill)


async def _set_percent(bot, d: Director, skill: str, value: int, tries: int = 4) -> int | None:
    """Set a skill and read it back from the bot's own ``practice``: ``set skill`` is silent and goes through the
    director's connection, so the bot can read its table before the change lands."""
    await d.set_skill(bot.name, skill, value)
    seen = None
    for _ in range(tries):
        seen = await _percent(bot, skill)
        if seen == value:
            break
        await bot.listen(0.5)
    return seen


async def _practise(run: ClassRun, bot, d: Director, nav: Navigator, world, skill: str) -> None:
    await d.set_char(bot.name, "practice", PRACTICE_SESSIONS)
    run.practice_from = await _set_percent(bot, d, skill, 1)
    dist = api.distances(world, nav.here) if nav.here is not None else {}
    for room in sorted((r for r in P.practice_rooms(world) if r in dist), key=lambda r: dist[r])[:3]:
        if not (await nav.walk_to(room)).reached:
            continue
        replies = []
        for _ in range(PRACTICE_SESSIONS):
            r = await bot.do(f"practice '{skill}'", expect=P.PRACTICE_REPLY)
            replies.append(r.text.strip().splitlines()[-1] if r.text.strip() else "")
            if "can't do that here" in r.text:
                break  # the practice mob isn't here: try the next room
            if not re.search(r"You practice|You are now learned", r.text):
                break
        if any("can't do that here" in x for x in replies):
            continue
        run.practice_reply = next((x for x in replies if x), "")
        break
    run.practice_to = await _percent(bot, skill)


async def _into_arena(d: Director, bot, mob: int) -> None:
    await d.goto(ARENA)
    await d.do("purge", expect=r"Ok\.|\n")
    await d.do(f"load mob {mob}", expect=r"Ok\.|No mob")
    await d.bring(bot.name)
    await d.restore(bot.name)
    await d.goto(TEMPLE)
    await bot.do("stand", expect=r"You stand|You are already|You're|Nah")
    await bot.do("look", expect=ROOM_REPLY)


def _note(run: ClassRun, ok: bool | None, text: str) -> None:
    key = {True: "ok", False: "failed", None: "unclear"}[ok]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("<")]
    first = lines[0][:160] if lines else ""
    bucket = run.messages.setdefault(key, [])
    if first and first not in bucket and len(bucket) < 6:
        bucket.append(first)


async def _use_in_fights(run: ClassRun, bot, d: Director, world, skill: str, cls: str, level: int, fights: int,
                         kind: str) -> None:
    mob = _target(world, cls, level, caster=kind != "combat skill")
    if mob is None:
        run.fights.append("no target predicted safe")
        return
    tactics = Tactics(cls, level, {skill: 75}, world=world)
    run.damage_predicted = tactics.spell_damage(skill, mob) if kind == "spell" else None
    for _ in range(fights):
        await _into_arena(d, bot, mob)
        if kind == "buff":
            await tactics.prepare(bot)
        nav = Navigator(bot, world)
        nav.here = ARENA
        r = await fight(bot, mob, world.mobs[mob].keywords.split()[0], escape=nav.escape, tactics=tactics)
        run.fights.append(f"{world.mobs[mob].short_descr}: {r.outcome}")
        if r.outcome == "died":
            break
    for u in tactics.uses:
        if u.action.name != skill:
            continue
        run.uses += 1
        run.successes += bool(u.ok)
        if u.damage is not None and u.ok and level >= DAMAGE_SHOWN:
            run.damage_seen.append(u.damage)
        run.mana.append(u.mana)
        run.lag.append(u.lag)
        if not u.text.strip():
            run.silent += 1
        _note(run, u.ok, u.text)


async def _use_on_self(run: ClassRun, bot, skill: str, is_spell: bool) -> None:
    command = f"cast '{skill}'" if is_spell else skill
    for _ in range(3):
        r = await bot.do(command, expect=SELF_REPLY, timeout=6)
        run.uses += 1
        text = r.text.strip()
        if not text:
            run.silent += 1
        unknown = UNKNOWN.search(text)
        if unknown:
            run.unknown = unknown.group(0)
            break
        ok = not re.search(r"lost your concentration|failed|You fail|nothing happens|don't have enough mana", text, re.I)
        run.successes += ok
        _note(run, ok, text)
        await bot.do("rest", expect=r"You rest|You are already|You sit|Nah")
        await bot.do("stand", expect=r"You stand|You are already|Nah")


async def run_skill(result, skill: str, port: int = 9100, classes: list[str] | None = None,
                    levels: list[int] | None = None, fights: int = 4) -> list[Finding]:
    from xania_sim.setup.stage import stage

    world = W.load()
    teach = learners(skill)
    if not teach:
        raise ValueError(f"no class learns {skill!r} (by Explorer's class tables)")
    if classes:
        teach = {c: lv for c, lv in teach.items() if c in classes}
    info = next(a for c in teach for a in api.abilities(c) if a.name == skill)
    kind = kind_of(skill, info.is_spell)
    result.measurements.update(skill=skill, kind=kind, learned_by=", ".join(f"{c} {lv}" for c, lv in teach.items()))
    found: list[Finding] = []
    names = iter(bot_names("skill", 500, mob_words(world)))
    runs = Path("runs") / time.strftime("skill-%Y%m%d-%H%M%S")
    repro = f"python -m xania_sim skill '{skill}'"
    async with TestMud(port=port) as mud:
        d = await Director.create(mud)
        try:
            for cls, learned in teach.items():
                for level in sorted({learned, *(levels or [])}):
                    run = ClassRun(cls, level)
                    bot = Bot(next(names), "skillpass1", host=mud.host, port=mud.port, transcript_dir=runs)
                    rep = await stage(mud, d, bot, level, "shop", "class", cls=cls, world=world)
                    run.staged = rep.ok
                    nav = Navigator(bot, world, Fingerprints(world))
                    nav.here = TEMPLE
                    await _practise(run, bot, d, nav, world, skill)
                    run.percent_before_use = await _set_percent(bot, d, skill, 75)
                    if kind in ("combat skill", "spell", "buff", "heal"):  # tactics uses what it can in fights
                        await _use_in_fights(run, bot, d, world, skill, cls, level, fights, kind)
                    if run.uses == 0:
                        await d.goto(TEMPLE)
                        await d.bring(bot.name)
                        await d.restore(bot.name)
                        await _use_on_self(run, bot, skill, info.is_spell)
                    run.percent_after_use = await _percent(bot, skill)
                    found += _findings(run, skill, repro)
                    result.measurements[f"{cls} {level}"] = run.summary()
                    result.outcomes.append(Outcome("skill", run.uses > 0 and not run.unknown,
                                                   f"{cls} {level}: {run.uses} uses, {run.successes} worked",
                                                   [m for ms in run.messages.values() for m in ms][:8],
                                                   {"messages": run.messages}))
                    try:
                        await bot.quit()
                    except Exception:
                        await bot.close()
        finally:
            await d.close()
        found += bug_line_findings(mud.log("xania"), repro)
    result.artifacts["transcripts"] = str(runs)
    return found


def _findings(run: ClassRun, skill: str, repro: str) -> list[Finding]:
    out = []
    where = f"{run.cls} {run.level}"
    if run.unknown:
        out.append(Finding("skill", f"skill:{skill}:unknown:{run.cls}", f"{skill}: a {run.cls} can't use it at the "
                           f"level it's learned ({run.level})", "high",
                           f"The class table gives {skill} to {run.cls}s at {run.level}, but the game said: "
                           f"{run.unknown}", repro, {"lines": [run.unknown], "context": where}))
    if run.practice_reply and re.search(r"can't practice|In your dreams", run.practice_reply):
        out.append(Finding("skill", f"skill:{skill}:practice:{run.cls}", f"{skill}: a {run.cls} can't practise it at "
                           f"level {run.level}", "medium", run.practice_reply, repro,
                           {"lines": [run.practice_reply], "context": where}))
    if run.silent:
        out.append(Finding("skill", f"skill:{skill}:silent", f"{skill}: a use got no message at all", "low",
                           f"{run.silent} of {run.uses} uses by a {where} produced no reply", repro,
                           {"lines": [], "context": where}))
    return out
