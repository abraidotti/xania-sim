"""Groups: following a leader, taking followers into a group, reading the group, splitting gold.

Messages (act_comm.cpp): ``follow`` "You now follow $N." (451), "You stop following $N." (467), "They aren't here.";
``group <name>`` "$N joins your group." to the leader and "You join $n's group." to the member (632-634), "$N isn't
following you." (590), "But you are following someone else!", "You remove $N from your group." (617); ``group``
lists "<leader>'s group:" then "[{level} {class}] {name} {hp}/{max} hp {mana}/{max} mana {mv}/{max} mv {xp} xp"
per member (566-573); ``split`` "You split N gold coins.  Your share is M gold coins." (act_obj.cpp:95).
Fighting together is autoassist (the ``autoassist`` toggle) and rescue is a tactic (activities/tactics.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from xania_sim.record.result import Outcome

FOLLOWING = re.compile(r"You now follow (?P<who>[^.\n]+)\.")
FOLLOW_REPLY = re.compile(FOLLOWING.pattern + r"|They aren't here|You already follow|You stop following|"
                          r"Follow whom", re.M)
JOINED = re.compile(r"(?P<who>\w+) joins your group\.")  # anywhere: combat output can share the line
GROUP_FAILED = re.compile(r"isn't following you|They aren't here|But you are following someone else|"
                          r"is already in a group|You remove .+? from your group", re.M)
MEMBER = re.compile(r"^\[\s*(?P<level>\d+) (?P<cls>\S+)\] (?P<name>.+?)\s+(?P<hp>-?\d+)/\s*(?P<max_hp>\d+) hp\s+"
                    r"(?P<mana>-?\d+)/\s*(?P<max_mana>\d+) mana\s+(?P<mv>-?\d+)/\s*(?P<max_mv>\d+) mv\s+"
                    r"(?P<xp>-?\d+) xp\s*$", re.M)
LEADER = re.compile(r"^(?P<leader>.+?)'s group:$", re.M)
SPLIT = re.compile(r"You split (?P<amount>\d+) gold coins\.\s+Your share is (?P<share>\d+) gold coins\.")


@dataclass(frozen=True)
class Member:
    name: str
    level: int
    cls: str
    hp: int
    max_hp: int
    mana: int
    max_mana: int


def parse_group(text: str) -> tuple[str | None, list[Member]]:
    leader = LEADER.search(text)
    members = [Member(m["name"].strip(), int(m["level"]), m["cls"], int(m["hp"]), int(m["max_hp"]), int(m["mana"]),
                      int(m["max_mana"])) for m in MEMBER.finditer(text)]
    return (leader["leader"] if leader else None), members


async def follow(bot, leader: str) -> Outcome:
    r = await bot.do(f"follow {leader}", expect=FOLLOW_REPLY)
    m = FOLLOWING.search(r.text)
    if m:
        return Outcome("group", True, f"following {m['who']}", [m.group(0)], {"leader": m["who"]})
    return Outcome("group", False, f"couldn't follow {leader}", [r.text.strip()[:200]], {})


async def invite(bot, name: str) -> Outcome:
    """Take a follower into the group (they must be following this bot)."""
    r = await bot.do(f"group {name}", expect=re.compile(JOINED.pattern + "|" + GROUP_FAILED.pattern, re.M))
    m = JOINED.search(r.text)
    if m:
        return Outcome("group", True, f"{m['who']} joined", [m.group(0)], {"member": m["who"]})
    f = GROUP_FAILED.search(r.text)
    return Outcome("group", False, f"{name} didn't join", [f.group(0) if f else r.text.strip()[:200]], {})


async def members(bot) -> tuple[str | None, list[Member]]:
    r = await bot.do("group", expect=r"'s group:")
    return parse_group(r.text)


async def form(leader_bot, followers: list) -> Outcome:
    """Each follower bot follows the leader, then the leader groups them all. ``data``: members (names)."""
    joined, failed = [], {}
    for b in followers:
        f = await follow(b, leader_bot.name)
        if not f.ok:
            failed[b.name] = f.evidence[0]
            continue
        g = await invite(leader_bot, b.name)
        (joined.append(b.name) if g.ok else failed.__setitem__(b.name, g.evidence[0]))
    return Outcome("group", not failed, f"{len(joined)} of {len(followers)} joined {leader_bot.name}'s group",
                   list(failed.values()), {"members": joined, "failed": failed})


async def split(bot, amount: int) -> Outcome:
    r = await bot.do(f"split {amount}", expect=r"You split|hand out zero|You don.t have that much gold|someone here to "
                                               r"split it with|cheapskate|Split")
    m = SPLIT.search(r.text)
    if m:
        return Outcome("group", True, f"split {m['amount']}, kept {m['share']}", [m.group(0)],
                       {"amount": int(m["amount"]), "share": int(m["share"])})
    return Outcome("group", False, f"couldn't split {amount}", [r.text.strip()[:200]], {})
