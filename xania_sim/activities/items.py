"""What bots know about objects, from the area files: recognising an item by the name the game shows, the keyword
to pick it up by, and how much it's worth wearing. Shared by loot, gear, trade and death.

Lists of objects (``inventory``, ``look in``) come from show_list_to_char (act_info.cpp): one line per item, with
``( 3)`` in front when several are the same, and ``(Glowing)``/``(Humming)``/``(Invis)`` flags before the name; an
empty list says "     Nothing." when the caller asks for it.
"""

from __future__ import annotations

import re

COLOUR = re.compile(r"\|.")  # Xania's colour codes in area text (|R, |w, ...)
FLAGS = re.compile(r"^(?:\((?:Glowing|Humming|Invis|Magical|Red Aura|Blue Aura|Hidden)\)\s*)+")
COUNT = re.compile(r"^\(\s*(\d+)\)\s*")
MONEY = re.compile(r"^(?:(\d+|one) gold coins?|a gold coin|a pile of gold coins)$", re.I)  # limbo.are money objects


def clean(line: str) -> tuple[str, int]:
    """("a long sword", 3) from "( 3) (Glowing) a long sword"."""
    line = line.strip()
    n = 1
    m = COUNT.match(line)
    if m:
        n, line = int(m.group(1)), line[m.end():]
    return FLAGS.sub("", line).strip().rstrip("."), n


def parse_list(text: str, header: str) -> list[tuple[str, int]]:
    """The items under a header line ("You are carrying:", "X contains:") up to a blank line or the end."""
    lines = text.replace("\r", "").split("\n")
    start = next((i for i, ln in enumerate(lines) if re.search(header, ln)), None)
    if start is None:
        return []
    out = []
    for ln in lines[start + 1:]:
        if not ln.strip() or ln.lstrip().startswith("<"):
            break
        name, n = clean(ln)
        if name and name.lower() != "nothing":
            out.append((name, n))
    return out


class Objects:
    """The world's objects, indexed by the name players see."""

    def __init__(self, world):
        self.world = world
        self.by_name: dict[str, list[int]] = {}
        for vnum, o in world.objects.items():
            self.by_name.setdefault(COLOUR.sub("", o.short_descr).strip().lower(), []).append(vnum)
        from xania_explorer.assess import equipment as E
        from xania_explorer.assess import weapons as WP

        gear, weapons = E.collect(world), WP.collect(world)
        self._value = {i.vnum: float(i.ac + i.hitroll + i.damroll) for i in gear}
        self._value.update({w.vnum: float(w.average) for w in weapons})
        self._slots = {i.vnum: list(i.slots) for i in gear}
        self._slots.update({w.vnum: ["wield"] for w in weapons})
        self._slots.update({v: ["light"] for v, o in world.objects.items() if o.type_name == "Light"})

    def vnums(self, name: str) -> list[int]:
        return self.by_name.get(name.strip().lower(), [])

    def keyword(self, name: str) -> str:
        """The keyword to pick ``name`` up by: money is "coins"; otherwise the object's first keyword (or the last
        word of its name if the area files don't know it)."""
        if MONEY.match(name.strip()):
            return "coins"
        vs = self.vnums(name)
        if vs:
            return self.world.objects[vs[0]].keywords.split()[0]
        words = re.findall(r"[A-Za-z]+", name)
        return words[-1].lower() if words else name

    def is_money(self, name: str) -> bool:
        return bool(MONEY.match(name.strip()))

    def value(self, vnum: int) -> float:
        """How much an item is worth wearing: AC + hitroll + damroll for armour (counted alike in the to-hit roll,
        HitChance.cpp:31-67), average damage for weapons; 0 if it isn't gear."""
        return self._value.get(vnum, 0.0)

    def slots(self, vnum: int) -> list[str]:
        """Where it can go: wear slots (Explorer's names: body, finger, ...), "wield" for weapons, "light"."""
        return self._slots.get(vnum, [])

    def level(self, vnum: int) -> int:
        o = self.world.objects.get(vnum)
        return int(o.level) if o is not None else 0

    def best_vnum(self, name: str, level: int | None = None) -> int | None:
        """The object a name most likely is: several objects share names ("a dagger"), so prefer those a character of
        ``level`` could use, then the most valuable."""
        vs = self.vnums(name)
        if level is not None:
            vs = [v for v in vs if self.level(v) <= level] or vs
        return max(vs, key=self.value) if vs else None
