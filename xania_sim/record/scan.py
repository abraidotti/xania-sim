"""What the game's source says about skills and spells, and a fingerprint of each so changes are noticed.

Levels per class come from Xania Explorer's skill-table parser (used as a library). Everything else is read here
and kept deliberately shallow: each skill-table entry's text, its target and minimum position, and the text of its
spell function. A fingerprint hashes those texts (comments and spacing ignored), so a new spell or a rebalanced
one shows up as new or changed without the sim having to understand the C++. What a spell actually does is
learned by bots trying it (see store.py).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

ENTRY = re.compile(r'\{\s*"(?P<name>[^"]+)",\s*(?:(?://[^\n]*|/\*.*?\*/)\s*)*(?P<fun>spell_\w+|nullptr)\s*,', re.S)
TARGET = re.compile(r"Target::(\w+)")
POSITION = re.compile(r"Position::Type::(\w+)")
_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def _matching_brace(text: str, start: int) -> int:
    """Index just past the brace that closes the one at ``start``, skipping comments, strings and char literals."""
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if c == "'":  # a char literal ('x', '\n'); otherwise a digit separator (1'000)
            if text.startswith("\\", i + 1):
                i = text.find("'", i + 3) + 1 or n
            elif i + 2 < n and text[i + 2] == "'":
                i += 3
            else:
                i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced braces")


def normalise(code: str) -> str:
    return " ".join(_COMMENTS.sub(" ", code).split())


def skill_entries(text: str) -> dict[str, tuple[str, str]]:
    """Skill-table entries in ``SkillTables.cpp`` text: name -> (spell function or "", entry text)."""
    out = {}
    for m in ENTRY.finditer(text):
        end = _matching_brace(text, m.start())
        fun = m.group("fun")
        out.setdefault(m.group("name"), ("" if fun == "nullptr" else fun, text[m.start():end]))
    return out


def function_bodies(sources: dict[str, str], names: set[str]) -> dict[str, str]:
    """``void spell_x(...) { ... }`` definitions for the given names, from any of the source files."""
    out = {}
    for text in sources.values():
        for m in re.finditer(r"\bvoid (spell_\w+)\s*\([^;{]*\)\s*\{", text):
            if m.group(1) in names and m.group(1) not in out:
                out[m.group(1)] = text[m.start():_matching_brace(text, m.end() - 1)]
    return out


@dataclass(frozen=True)
class AbilityFacts:
    name: str
    is_spell: bool
    spell_fun: str
    target: str  # Target:: in the skill table, e.g. CharOffensive, CharDefensive, CharSelf, Ignore
    position: str  # minimum Position::Type to use it
    min_mana: int
    beats: int  # lag in pulses
    levels: dict[str, int | None] = field(default_factory=dict)  # class -> level it can be learned (None = never)
    fingerprint: str = ""


def scan_source(src_dir: Path) -> dict[str, AbilityFacts]:
    from xania_explorer.source.cpp_tables import CLASSES, load_skills

    table_text = (src_dir / "SkillTables.cpp").read_text(encoding="latin-1")
    entries = skill_entries(table_text)
    sources = {p.name: p.read_text(encoding="latin-1") for p in sorted(src_dir.glob("*.cpp"))}
    bodies = function_bodies(sources, {fun for fun, _ in entries.values() if fun})
    skills = load_skills(src_dir)
    out = {}
    for name, (fun, entry) in entries.items():
        if name == "reserved":
            continue
        s = skills.get(name)
        t, p = TARGET.search(entry), POSITION.search(entry)
        digest = hashlib.sha1((normalise(entry) + "\n" + normalise(bodies.get(fun, ""))).encode()).hexdigest()[:16]
        out[name] = AbilityFacts(
            name, bool(s.is_spell) if s else bool(fun), fun, t.group(1) if t else "", p.group(1) if p else "",
            s.min_mana if s else 0, s.beats if s else 0,
            {c: (s.learn_level(c) if s else None) for c in CLASSES}, digest)
    if len(out) < 50 or "magic missile" not in out:
        raise ValueError(f"the skill table in {src_dir} doesn't look right ({len(out)} entries)")
    return out


def crosscheck(listed: dict[str, tuple[int, int | None]], facts: dict[str, AbilityFacts], cls: str) -> list[str]:
    """Disagreements between a character's own ``spells``/``skills`` lists and the scanned source: abilities the
    game knows but the source doesn't, or at a different level. Any mean the running game isn't this source."""
    out = []
    for name, (level, _) in sorted(listed.items()):
        f = facts.get(name)
        if f is None:
            out.append(f"{name}: in the game but not in the source")
        elif f.levels.get(cls) not in (None, level) and level != 0:
            out.append(f"{name}: level {level} in the game, {f.levels.get(cls)} in the source")
    return out


# -- what a character's own lists say (SkillsTabulator.cpp) ------------------------------------------------
_CELL = re.compile(r"(?:L\s*(?P<level>\d+): |     )(?P<name>[a-z][a-z' -]*?)\s+"
                   r"(?:(?P<mana>\d+)m\s+(?P<pct>\d+)%|(?P<spct>\d+)%|n/a)")


def parse_ability_list(text: str) -> dict[str, tuple[int, int | None]]:
    """``spells`` / ``skills`` output: name -> (level it's learned at, % learned or None if not yet usable)."""
    out, level = {}, 0
    for m in _CELL.finditer(text):
        if m.group("level"):
            level = int(m.group("level"))
        pct = m.group("pct") or m.group("spct")
        out[m.group("name").strip()] = (level, int(pct) if pct else None)
    return out
