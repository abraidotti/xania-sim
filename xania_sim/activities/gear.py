"""Gear: what the bot carries and wears, putting things on, and wearing whatever is better than what it has.

``inventory`` lists under "You are carrying:" and ``equipment`` under "You are using:" with one "<location> item"
line per slot (act_info.cpp:1321-1326). ``wear``/``wield``/``hold`` (act_obj.cpp wear_obj, 140-425) replace what's
in the slot and answer "You wear $p ...", "You wield $p.", "You hold $p in your hands.", "You light $p and hold it."
or refuse: "You must be level N to use this object." (act_obj.cpp:137), "It is too heavy for you to wield.", "You need
two hands free for that weapon.", "You can't wear, wield, or hold that.", "You can't remove $p because it is
cursed..." (the item in the slot won't come off); an item of the wrong alignment goes on and is then dropped: "You are
zapped by $p and drop it." (handler.cpp:433).
"""

from __future__ import annotations

import re

from xania_sim.activities.items import Objects, parse_list
from xania_sim.record.result import Outcome

EQUIPMENT_LINE = re.compile(r"^<([^>]+)>\s+(.+?)\s*$", re.M)
WORN = re.compile(r"^You (?:wear|wield|hold|light) (?P<what>.+?)(?: on | around | as | about | in your|\.| and hold)",
                  re.M)
REFUSED = re.compile(r"You must be level \d+ to use this object|It is too heavy for you to wield|"
                     r"You need two hands free for that weapon|You can't wear, wield, or hold that|"
                     r"You can't remove .+? because it is cursed|You are zapped by .+? and drop it|"
                     r"You do not have that item|You don't have that", re.M)
WEAR_REPLY = re.compile(WORN.pattern + "|" + REFUSED.pattern, re.M)


def parse_equipment(text: str) -> list[tuple[str, str]]:
    """[(location, item)] from ``equipment`` output."""
    return [(m.group(1), m.group(2)) for m in EQUIPMENT_LINE.finditer(text)]


async def inventory(bot) -> list[tuple[str, int]]:
    r = await bot.do("inventory", expect=r"You are carrying:")
    return parse_list(r.text, r"^You are carrying:")


async def equipment(bot) -> list[tuple[str, str]]:
    r = await bot.do("equipment", expect=r"You are using:")
    return parse_equipment(r.text)


async def wear(bot, keyword: str, command: str = "wear") -> Outcome:
    """``command`` is wear, wield or hold. A zapped item counts as refused (it ends up on the floor)."""
    r = await bot.do(f"{command} {keyword}", expect=WEAR_REPLY)
    refused = REFUSED.search(r.text)
    if refused:
        return Outcome("gear", False, f"{command} {keyword}: refused", [refused.group(0)], {"keyword": keyword})
    m = WORN.search(r.text)
    if m:
        return Outcome("gear", True, f"{command} {m['what']}", [m.group(0)], {"item": m["what"]})
    return Outcome("gear", None, f"{command} {keyword}: no answer", [r.text.strip()[:200]], {"keyword": keyword})


# act_info.cpp:66-76 worn_string_for: the "<...>" label of each wear location, and the slot it is
LOCATION_SLOT = {"used as light": "light", "worn on finger": "finger", "worn around neck": "neck",
                 "worn on body": "body", "worn on head": "head", "worn on legs": "legs", "worn on feet": "feet",
                 "worn on hands": "hands", "worn on arms": "arms", "worn as shield": "shield",
                 "worn about body": "about", "worn about waist": "waist", "worn around wrist": "wrist",
                 "wielded": "wield", "held": "hold", "worn on ears": "ears"}
CAPACITY = {"finger": 2, "neck": 2, "wrist": 2}  # two of each (Worn.hpp); one of everything else


def worn_by_slot(objects: Objects, worn: list[tuple[str, str]], level: int | None = None) -> dict[str, list[float]]:
    """The value of what's worn in each slot (one entry per item; a slot with nothing has no entry)."""
    from xania_sim.activities.items import clean

    out: dict[str, list[float]] = {}
    for loc, item in worn:
        slot = LOCATION_SLOT.get(loc)
        if slot:
            v = objects.best_vnum(clean(item)[0], level)
            out.setdefault(slot, []).append(objects.value(v) if v else 0.0)
    return out


def upgrades(objects: Objects, level: int, carried: list[tuple[str, int]], worn: list[tuple[str, str]]) -> list[str]:
    """Carried items worth wearing, best first: high enough level for the bot, and worth more than the weakest thing
    in a slot they fit (or there's a free one)."""
    have = worn_by_slot(objects, worn, level)
    out = []
    for name, _ in carried:
        v = objects.best_vnum(name, level)
        if v is None or objects.value(v) <= 0 or objects.level(v) > level:
            continue
        for slot in objects.slots(v):
            there = have.get(slot, [])
            if len(there) < CAPACITY.get(slot, 1) or objects.value(v) > min(there):
                out.append(name)
                break
    return sorted(out, key=lambda n: -objects.value(objects.best_vnum(n, level)))


def command_for(objects: Objects, vnum: int) -> str:
    slots = objects.slots(vnum)
    return "wield" if "wield" in slots else "hold" if slots and slots[0] in ("hold", "light") else "wear"


async def equip_best(bot, world, level: int, objects: Objects | None = None) -> Outcome:
    """Put on every carried item that's an upgrade, best first. ``data``: worn (names), refused (name: reply)."""
    objects = objects or Objects(world)
    carried, worn = await inventory(bot), await equipment(bot)
    put_on, refused = [], {}
    for name in upgrades(objects, level, carried, worn):
        out = await wear(bot, objects.keyword(name), command_for(objects, objects.best_vnum(name, level)))
        if out.ok:
            put_on.append(name)
        elif out.ok is False:
            refused[name] = out.evidence[0]
    ok = None if not put_on and not refused else not refused
    return Outcome("gear", ok, f"put on {len(put_on)} item(s)" + (f", {len(refused)} refused" if refused else ""),
                   list(refused.values()), {"worn": put_on, "refused": refused})
