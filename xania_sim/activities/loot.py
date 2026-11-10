"""Looting: looking in a corpse (or any container) and taking what's in it, one item at a time.

Never ``get all``: with ``all`` do_get passes ``arg1[3]`` (act_obj.cpp:707), which aborts debug builds (xania
issue #1). Replies (act_obj.cpp do_get / get_obj, 520-600, 630-700): "You get $p from $P." on success; "You can't
take that.", "$d: you can't carry that many items.", "$d: you can't carry that much weight.", "Corpse looting is
not permitted.", "<deity> forbids you from possessing more than one $p.", "You are not powerful enough to use it.",
"I see no $T here.", "I see nothing like that in the $T." on failure. ``look in`` lists the contents under
"$p contains:" (act_info.cpp:763).
"""

from __future__ import annotations

import re

from xania_sim.activities.items import Objects, parse_list
from xania_sim.record.result import Outcome

GOT = re.compile(r"^You get (?P<what>.+?) from (?P<from>.+?)\.$", re.M)
GET_FAILED = re.compile(r"You can't take that|can't carry that many items|can't carry that much weight|"
                        r"Corpse looting is not permitted|forbids you from possessing more than one|"
                        r"not powerful enough to use it|I see no |I see nothing like that", re.M)
GET_REPLY = re.compile(GOT.pattern + "|" + GET_FAILED.pattern, re.M)
CONTAINS = r" contains:$"
LOOK_IN_REPLY = re.compile(r" contains:|is not a container|I see no|You do not see that here|is closed|"
                           r"It is empty|That is not a container", re.M)
GOLD = re.compile(r"(\d+) gold coins?")


async def look_in(bot, container: str = "corpse") -> list[tuple[str, int]]:
    """What's in a container in the room or carried, as [(name, count)]."""
    r = await bot.do(f"look in {container}", expect=LOOK_IN_REPLY)
    return parse_list(r.text, CONTAINS)


async def take(bot, keyword: str, container: str = "corpse") -> Outcome:
    r = await bot.do(f"get {keyword} {container}", expect=GET_REPLY)
    m = GOT.search(r.text)
    if m:
        return Outcome("loot", True, f"got {m['what']}", [m.group(0)], {"item": m["what"]})
    fail = GET_FAILED.search(r.text)
    return Outcome("loot", False, f"couldn't get {keyword}", [fail.group(0) if fail else r.text.strip()[:200]],
                   {"keyword": keyword})


async def loot(bot, world, container: str = "corpse", want=None, sacrifice: bool = False,
               objects: Objects | None = None) -> Outcome:
    """Take what's in ``container`` that ``want(name)`` accepts (default: everything), money included, one item at
    a time; optionally sacrifice it once empty. ``data``: taken, refused (name: the game's reply), left, gold."""
    objects = objects or Objects(world)
    contents = await look_in(bot, container)
    taken, refused, left, gold = [], {}, [], 0
    for name, count in contents:
        if want is not None and not objects.is_money(name) and not want(name):
            left.append(name)
            continue
        for _ in range(count):
            o = await take(bot, objects.keyword(name), container)
            if not o.ok:
                refused[name] = o.evidence[0]
                break
            taken.append(o.data["item"])
            g = GOLD.search(o.data["item"])
            gold += int(g.group(1)) if g else 0
    evidence = [f"{container}: " + (", ".join(n if c == 1 else f"{n} x{c}" for n, c in contents) or "empty")]
    if sacrifice and not left and not refused:
        r = await bot.do(f"sacrifice {container}", expect=r"sacrifice|gives you|isn't|I see no|You can't")
        evidence.append(r.text.strip().splitlines()[-1] if r.text.strip() else "")
    ok = None if not contents else not refused
    summary = f"took {len(taken)} of {sum(c for _, c in contents)} from the {container}" + (
        f", {gold} gold" if gold else "")
    return Outcome("loot", ok, summary, evidence + list(refused.values()),
                   {"taken": taken, "refused": refused, "left": left, "gold": gold})
