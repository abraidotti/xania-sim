"""Death and getting your things back.

Dying (fight.cpp raw_kill): "You have been KILLED!!" (fight.cpp:740); a player's corpse (object 11, keyword
"corpse", short "corpse of <name>": fight.cpp make_corpse, 1188-1230) keeps everything it carried and wore and lasts
25-40 ticks; the player wakes at the Midgaard altar (room 3054: handler.cpp:615) resting, with at least 1 HP
(fight.cpp:1323-1324). Several corpses can share a room (mobs' are object 10, also "corpse"), so the bot looks in
``1.corpse``, ``2.corpse``, ... for the one with its name.
"""

from __future__ import annotations

import re

from xania_sim.activities.items import Objects, parse_list
from xania_sim.activities.loot import LOOK_IN_REPLY, loot
from xania_sim.record.result import Outcome

DIED = re.compile(r"You have been KILLED!!")
ALTAR = 3054  # VnumRooms.hpp MidgaardAltar


def died(text: str) -> bool:
    return bool(DIED.search(text))


async def find_own_corpse(bot, max_corpses: int = 6) -> str | None:
    """The ``N.corpse`` that is this bot's, or None."""
    for n in range(1, max_corpses + 1):
        r = await bot.do(f"look in {n}.corpse", expect=LOOK_IN_REPLY)
        if re.search(rf"corpse of {re.escape(bot.name)} contains:", r.text, re.I):
            return f"{n}.corpse"
        if " contains:" not in r.text:
            return None  # no more corpses here
    return None


async def recover(bot, nav, world, room: int, objects: Objects | None = None, wear: bool = True) -> Outcome:
    """Walk from wherever the bot is (the altar, after dying) back to ``room`` and take everything from its corpse,
    then put its gear back on. ``data``: taken, refused, reached, worn_again (the game's reply to ``wear all``)."""
    await nav.stand()
    if nav.here is None:
        await nav.locate()
    avoided = room in nav.avoid
    nav.avoid.discard(room)  # the room it died in may be marked dangerous; it has to go back once
    try:
        walk = await nav.walk_to(room)
    finally:
        if avoided:
            nav.avoid.add(room)
    if not walk.reached:
        return Outcome("death", False, f"couldn't get back to room {room}", [], {"reached": False})
    corpse = await find_own_corpse(bot)
    if corpse is None:
        return Outcome("death", False, "no corpse with my name here (decayed, or taken?)", [], {"reached": True})
    got = await loot(bot, world, corpse, objects=objects or Objects(world))
    data = {"reached": True, **got.data}
    if wear and got.data["taken"]:
        r = await bot.do("wear all", expect=r"You wear|You wield|You hold|You light|You can't|Wear what|"
                                            r"You do not have")
        data["worn_again"] = len(re.findall(r"^You (?:wear|wield|hold|light) ", r.text, re.M))
    ok = not got.data["refused"]
    return Outcome("death", ok, f"recovered {len(got.data['taken'])} item(s) from {corpse}", got.evidence, data)


def corpse_contents(text: str) -> list[tuple[str, int]]:
    return parse_list(text, r" contains:$")
