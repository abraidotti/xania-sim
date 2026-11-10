"""Pure functions that turn MUD text into data. Kept free of I/O so they're tested against captured output."""

from __future__ import annotations

import re
from dataclasses import dataclass

# The prompt every bot sets once it's in the game (tokens: xania/src/PromptFormat.cpp). It marks the end of each
# command's output, and carries the numbers a bot watches. %c is the wait state (lag in pulses).
PROMPT_FORMAT = "<bot %h/%H %m/%M %v/%V %X %g %c>%n"
PROMPT = re.compile(r"<bot (-?\d+)/(-?\d+) (-?\d+)/(-?\d+) (-?\d+)/(-?\d+) (-?\d+) (-?\d+) (-?\d+)>\n")
HIT_RETURN = re.compile(r"\[Hit [Rr]eturn to continue\]\n?")


@dataclass(frozen=True)
class Prompt:
    hp: int
    max_hp: int
    mana: int
    max_mana: int
    moves: int
    max_moves: int
    xp: int
    gold: int
    wait: int

    @property
    def hp_share(self) -> float:
        return self.hp / self.max_hp if self.max_hp else 0.0


def parse_prompt(m: re.Match) -> Prompt:
    return Prompt(*(int(g) for g in m.groups()))


def lines(text: str) -> list[str]:
    """Output split into lines, without the blank ones the MUD pads with."""
    return [ln.rstrip() for ln in text.split("\n") if ln.strip()]
