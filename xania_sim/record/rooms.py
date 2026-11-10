"""Recognising rooms: what the MUD prints when a bot arrives or looks, matched against Explorer's map.

``room_look`` (xania/src/act_info.cpp) prints the room name (possibly several lines), a blank line, the description,
then ``[Exits: north down]`` listing only open exits (closed doors are hidden), then objects and people. Dark rooms
print "It is pitch black ..." instead. Room names aren't unique (about 600 are shared), so a room is recognised by
a fingerprint: name, the description's first line, and exits consistent with the map; the navigator also uses
where it expected to arrive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from xania_explorer.areafile import flags as F

EXITS = re.compile(r"^\[Exits:(?P<dirs>[a-z ]*)\]\s*$", re.M)
DARK = "It is pitch black"
_COLOUR = re.compile(r"\|(.)")


def plain(text: str) -> str:
    """Xania colour codes removed (``|R`` etc.; ``||`` is a literal bar), as a colourless client sees it."""
    return _COLOUR.sub(lambda m: "|" if m.group(1) == "|" else "", text)


def name_lines(name: str) -> list[str]:
    return [ln.strip() for ln in plain(name).split("\n") if ln.strip()]


def first_line(description: str) -> str:
    return next((ln.strip() for ln in plain(description).split("\n") if ln.strip()), "")


@dataclass(frozen=True)
class RoomView:
    lines: tuple[str, ...]  # the lines printed before [Exits: ...]
    exits: frozenset[str]  # open exits, by direction name
    dark: bool = False


def parse_view(text: str) -> RoomView | None:
    """The last room view in some output, or None if there isn't one (e.g. "Alas, you cannot go that way.")."""
    if DARK in text and not EXITS.search(text):
        return RoomView((), frozenset(), dark=True)
    matches = list(EXITS.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    start = matches[-2].end() if len(matches) > 1 else 0
    before = tuple(ln.strip() for ln in text[start:m.start()].split("\n") if ln.strip())
    dirs = frozenset(d for d in m.group("dirs").split() if d != "none")
    return RoomView(before, dirs)


@dataclass
class Fingerprints:
    """Precomputed per-room recognition data for a world."""

    world: object
    names: dict[int, list[str]] = field(default_factory=dict)
    first: dict[int, str] = field(default_factory=dict)
    shown: dict[int, frozenset[str]] = field(default_factory=dict)  # exits that can be listed
    always: dict[int, frozenset[str]] = field(default_factory=dict)  # exits listed unless closed: non-doors

    def __post_init__(self):
        rooms = self.world.rooms
        for v, r in rooms.items():
            self.names[v] = name_lines(r.name)
            self.first[v] = first_line(r.description)
            real = {d: e for d, e in r.exits.items() if not e.cosmetic and e.to_vnum in rooms}
            self.shown[v] = frozenset(F.DIRECTIONS[d] for d in real)
            self.always[v] = frozenset(F.DIRECTIONS[d] for d, e in real.items() if not e.is_door)

    def matches(self, vnum: int, view: RoomView) -> bool:
        if view.dark:
            return False
        names = self.names.get(vnum)
        if not names or names[0] not in view.lines:
            return False
        first = self.first[vnum]
        if first and first not in view.lines:
            return False
        return self.always[vnum] <= view.exits <= self.shown[vnum]

    def candidates(self, view: RoomView, among=None) -> list[int]:
        pool = self.world.rooms if among is None else among
        return [v for v in pool if self.matches(v, view)]
