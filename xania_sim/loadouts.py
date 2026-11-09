"""Skill and spell loadouts: what a staged bot knows, and how well, for a (class, level, race).

Named loadouts (``LOADOUTS``), each a full specification: staging first sets every skill to 0, then these:

- ``none``: nothing (every skill 0, the school weapon's 40% included);
- ``passives``: the skills that work on their own (parry, dodge, shield block, second/third attack, enhanced damage,
  fast healing, meditation and the weapon skills) that the class can use by the level, at 75;
- ``class``: everything the class can use by the level, at 75 (cross-class skills from level 60 included);
- ``natural``: an estimate of what a bot that levelled normally would have (see ``natural_skills``);
- ``custom``: from a TOML file, e.g. ``bash = 90`` and ``fireball = 75``, optionally ``base = "class"`` to start
  from another loadout instead of ``none``.

Whether a skill is usable is ``get_skill_level`` (skills.cpp:566-602), ported as ``skill_level``: the class's level
for the skill when its rating is positive, ``LevelCrossClassTraining`` (60, skills.cpp:37) for "attainable" ones,
30 or 60 for the assassin specials, lowered to 30 for a race's bonus skills, and **0 for unattainable ones** (rating
0). The game then lets a character use a skill when ``level >= get_skill_level`` (Char.cpp:226 get_skill,
magic.cpp:596 do_cast, fight.cpp:1683 do_bash), so an unattainable skill set by an immortal works at any level, and
``practice`` lists it (PracticeTabulator.cpp:19). 75 is every class's adept rating (Class.cpp), the most practice
gives.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from xania_explorer.source.character_tables import load_classes, load_pc_races, load_stat_tables
from xania_explorer.source.cpp_tables import (CLASSES, LEVEL_CROSS_CLASS_TRAINING, LEVEL_SPECIAL_CLASS_TRAINING,
                                              SKILL_RATING_ATTAINABLE, SKILL_RATING_SPECIAL, expand_group,
                                              load_groups, load_skills, starting_skills)

from .trainer import ENOUGH, PRIORITY, plan

LOADOUTS = ("none", "passives", "class", "natural", "custom")
VALUE = 75
WEAPON_SKILLS = ("sword", "dagger", "spear", "mace", "axe", "flail", "whip", "polearm")
PASSIVES = ("parry", "dodge", "shield block", "second attack", "third attack", "enhanced damage", "fast healing",
            "meditation", "hand to hand", *WEAPON_SKILLS)
SCHOOL_WEAPON_SKILL = {"mage": "dagger", "cleric": "mace", "knight": "sword", "barbarian": "axe"}  # Class.cpp
NEWBIE_WEAPON_PCT = 40  # MudImpl.cpp:64 NewbieWeaponSkillPct
NEWBIE_PRACS = 5  # MudImpl.cpp:66
USES_PER_LEVEL = 10  # natural: how often a bot uses each skill it relies on, per level (an assumption)
IMPROVE_MULTIPLIER = 6  # check_improve's difficulty multiplier for combat skills (fight.cpp:277, :511, :1123)


def skill_level(cls: str, skill: str, race: str = "human", assassin: bool = False, src_dir=None) -> int:
    """Port of ``get_skill_level`` (skills.cpp:566): the level from which a PC can use ``skill``; 0 = unattainable
    (which the callers then treat as usable at any level)."""
    s = load_skills(src_dir)[skill]
    i = CLASSES.index(cls)
    rating, level = s.ratings[i], 0
    if rating > 0:
        level = s.levels[i]
    if rating == SKILL_RATING_ATTAINABLE:
        level = LEVEL_CROSS_CLASS_TRAINING
    if rating == SKILL_RATING_SPECIAL:
        level = LEVEL_SPECIAL_CLASS_TRAINING if assassin else LEVEL_CROSS_CLASS_TRAINING
    if skill in race_skills(race, src_dir) and level > LEVEL_SPECIAL_CLASS_TRAINING:
        level = LEVEL_SPECIAL_CLASS_TRAINING
    return level


def race_skills(race: str, src_dir=None) -> set[str]:
    """A race's bonus skills (Races.cpp pc_race_table). get_skill_level compares the skill itself, so a bonus
    *group* (dragon's "draconian") gives no level bonus to its members."""
    return set(load_pc_races(src_dir)[race].bonus_skills)


def usable(cls: str, skill: str, level: int, race: str = "human", src_dir=None) -> bool:
    """What the game does: ``level < get_skill_level`` refuses; anything else (unattainable included) works."""
    return level >= skill_level(cls, skill, race, src_dir=src_dir)


def difficulty(cls: str, skill: str, src_dir=None) -> int:
    """``get_skill_difficulty`` (skills.cpp:604) for a usable skill: the rating, 8 cross-class, 5 special."""
    rating = load_skills(src_dir)[skill].ratings[CLASSES.index(cls)]
    return {SKILL_RATING_ATTAINABLE: 8, SKILL_RATING_SPECIAL: 5}.get(rating, max(rating, 0))


@dataclass(frozen=True)
class Entry:
    skill: str
    value: int
    level: int  # get_skill_level for this class and race
    usable: bool  # at the loadout's level, as the game decides
    why: str


@dataclass
class Loadout:
    name: str
    cls: str
    level: int
    race: str
    entries: dict[str, Entry] = field(default_factory=dict)
    reset: int = 0  # staging sets every skill to this first ("set skill <name> all <reset>")

    @property
    def settings(self) -> dict[str, int]:
        return {e.skill: e.value for e in self.entries.values()}

    @property
    def unusable(self) -> list[Entry]:
        return [e for e in self.entries.values() if not e.usable and e.value > 0]

    def expected_practice(self) -> dict[str, int]:
        """What ``practice`` should list: skills above 0 the character is high enough for (PracticeTabulator.cpp)."""
        return {e.skill: e.value for e in self.entries.values() if e.usable and e.value > 0}

    def describe(self) -> str:
        lines = [f"{self.name} loadout, level {self.level} {self.race} {self.cls}: {len(self.entries)} skills"]
        for e in sorted(self.entries.values(), key=lambda e: (e.level, e.skill)):
            lines.append(f"  {e.skill:<22} {e.value:>3}%  {'ok ' if e.usable else 'NO '} {e.why}")
        return "\n".join(lines)


def entry(cls: str, skill: str, value: int, level: int, race: str = "human", src_dir=None) -> Entry:
    s = load_skills(src_dir)[skill]
    rating = s.ratings[CLASSES.index(cls)]
    lvl = skill_level(cls, skill, race, src_dir=src_dir)
    if lvl == 0:
        why = "unattainable for the class: get_skill_level is 0, so the game lets any level use it"
    elif level < lvl:
        why = f"needs level {lvl}"
    elif rating == SKILL_RATING_ATTAINABLE:
        why = f"cross-class from level {lvl}"
    elif skill in race_skills(race, src_dir) and lvl == LEVEL_SPECIAL_CLASS_TRAINING:
        why = f"race bonus from level {lvl}"
    else:
        why = f"from level {lvl}"
    return Entry(skill, value, lvl, level >= lvl, why)


def abilities(src_dir=None) -> list[str]:
    return [n for n in load_skills(src_dir) if n != "reserved"]


def class_skills(cls: str, level: int, race: str = "human", src_dir=None) -> list[str]:
    """Every skill and spell the class can learn and use by ``level`` (unattainable ones excluded)."""
    return [n for n in abilities(src_dir) if 0 < skill_level(cls, n, race, src_dir=src_dir) <= level]


def build_loadout(name: str, cls: str, level: int, race: str = "human", path: str | Path | None = None,
                  src_dir=None) -> Loadout:
    if name == "custom":
        if path is None:
            raise ValueError("the custom loadout needs a TOML file")
        return load_custom(path, cls, level, race, src_dir)
    if name not in LOADOUTS:
        raise KeyError(f"no loadout named {name!r} (have {', '.join(LOADOUTS)})")
    if name == "none":
        values = {}
    elif name == "passives":
        values = {s: VALUE for s in PASSIVES if s in class_skills(cls, level, race, src_dir)}
    elif name == "class":
        values = {s: VALUE for s in class_skills(cls, level, race, src_dir)}
    else:
        values = natural_skills(cls, level, race, src_dir)
    return Loadout(name, cls, level, race, {s: entry(cls, s, v, level, race, src_dir) for s, v in values.items()})


def load_custom(path: str | Path, cls: str, level: int, race: str = "human", src_dir=None) -> Loadout:
    """``{skill = percent}`` from a TOML file; ``base = "<loadout>"`` starts from that loadout (default none)."""
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    base = data.pop("base", "none")
    if base == "custom":
        raise ValueError("a custom loadout can't be based on another custom one")
    out = build_loadout(base, cls, level, race, src_dir=src_dir)
    known = set(abilities(src_dir))
    for skill, value in data.items():
        if skill not in known:
            raise KeyError(f"{path}: no skill or spell named {skill!r}")
        if not isinstance(value, int) or not 0 <= value <= 100:
            raise ValueError(f"{path}: {skill} must be a whole percentage 0-100, not {value!r}")
        out.entries[skill] = entry(cls, skill, value, level, race, src_dir)
    out.name = f"custom ({Path(path).name})"
    return out


def natural_skills(cls: str, level: int, race: str = "human", src_dir=None) -> dict[str, int]:
    """An estimate of a bot's skills after levelling to ``level`` the way xania-sim's bots do.

    Assumptions: it knows only what a new character gets (rom basics + class basics + class default, and its race's
    bonus skills; bots never ``gain``), its school weapon at 40%, everything else at 1%. It gets 5 practice sessions
    at creation and ``wis_app[wis]`` per level (update.cpp:515), and spends them as trainer.py does (``plan``: the
    class's priorities, round robin, up to 75), each session adding ``int_app[int].learn / difficulty``
    (act_info.cpp:1604). Each skill in the class's priority list (and each passive it knows) is used
    ``USES_PER_LEVEL`` times a level, successfully, and each use adds 1% with Learning.cpp's chance:
    ``(1 - (10 * learn / (6 * difficulty * 4) + level) / 1000) * clamp(100 - learned, 5, 95) / 100``. Stats are
    the race's starting ones, +3 on the class's prime stat (MudImpl.cpp:985)."""
    known = _known(cls, race, src_dir)
    learn, pracs = _learn_and_pracs(cls, race, src_dir)
    weapons = set(WEAPON_SKILLS)
    used = ((set(PRIORITY.get(cls, [])) | set(PASSIVES)) - weapons | {SCHOOL_WEAPON_SKILL[cls]}) & known
    pct = {s: 1.0 for s in known}
    pct[SCHOOL_WEAPON_SKILL[cls]] = NEWBIE_WEAPON_PCT
    sessions = NEWBIE_PRACS
    for lvl in range(1, level + 1):
        if lvl > 1:
            sessions += pracs
        now = {s: int(p) for s, p in pct.items() if usable(cls, s, lvl, race, src_dir)}
        spend = plan(cls, now, sessions)
        sessions -= len(spend)
        for s in spend:
            pct[s] = min(ENOUGH, pct[s] + learn // max(1, difficulty(cls, s, src_dir)))
        for s in used:
            if s not in now:
                continue
            diff = max(1, difficulty(cls, s, src_dir))
            p_pass = max(0, 1000 - (10 * learn // (IMPROVE_MULTIPLIER * diff * 4) + lvl)) / 1000
            for _ in range(USES_PER_LEVEL):
                pct[s] = min(100.0, pct[s] + p_pass * min(95, max(5, 100 - pct[s])) / 100)
    return {s: int(p) for s, p in pct.items() if usable(cls, s, level, race, src_dir)}


def _known(cls: str, race: str, src_dir=None) -> set[str]:
    groups = load_groups(src_dir)
    skills = set(load_skills(src_dir))
    known = set(starting_skills(cls, src_dir))
    for bonus in race_skills(race, src_dir):
        known |= expand_group(bonus, groups) if bonus in groups else {bonus}
    return {s for s in known if s in skills}


def _learn_and_pracs(cls: str, race: str, src_dir=None) -> tuple[int, int]:
    """(int_app learn, wis_app practices per level) for the race's starting stats with the prime bonus."""
    stats = list(load_pc_races(src_dir)[race].stats)  # str int wis dex con
    prime = ["str", "int", "wis", "dex", "con"].index(load_classes(src_dir)[cls].prime_stat)
    if race != "dragon":
        stats[prime] += 3
    t = load_stat_tables(src_dir)
    return t.int_app[stats[1]], t.wis_app[stats[2]]
