"""Bot names the MUD will accept (``MudImpl::validate_player_name``, xania/src/MudImpl.cpp).

A name must be 2-12 letters, not made only of i's and l's, not a reserved word, and not match any mob: the MUD
refuses a name that equals or is a prefix of a word in some mob's keywords (``is_name``, string_utils.cpp).
"""

from __future__ import annotations

import string
from collections.abc import Iterable

RESERVED = {"all", "auto", "immortal", "self", "someone", "something", "the", "you"}


def mob_words(world) -> set[str]:
    """Every keyword of every mob prototype in an Explorer world, lower-cased."""
    return {w.lower() for m in world.mobs.values() for w in m.keywords.split()}


def name_problem(name: str, mob_keywords: Iterable[str] = ()) -> str | None:
    """Why the MUD would refuse ``name``, or None if it's acceptable."""
    low = name.lower()
    if low in RESERVED:
        return "reserved word"
    if low == "death":
        return None
    if not 2 <= len(name) <= 12:
        return "must be 2-12 letters"
    if not name.isascii() or not name.isalpha():
        return "letters only"
    if set(low) <= {"i", "l"}:
        return "only i and l"
    clash = next((w for w in mob_keywords if w.startswith(low)), None)
    if clash:
        return f"matches the mob keyword {clash!r}"
    return None


def bot_names(stem: str, count: int, mob_keywords: Iterable[str] = ()) -> list[str]:
    """``count`` distinct acceptable names: the stem plus a letter suffix (a, b, ..., z, ba, bb, ...)."""
    words = set(mob_keywords)
    out, i = [], 0
    while len(out) < count:
        n, suffix = i, ""
        while True:
            suffix = string.ascii_lowercase[n % 26] + suffix
            n //= 26
            if not n:
                break
        name = (stem + suffix).capitalize()
        if name_problem(name, words) is None:
            out.append(name)
        i += 1
        if i > count + 10000:
            raise ValueError(f"can't make {count} acceptable names from {stem!r}")
    return out
