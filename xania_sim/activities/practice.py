"""Spending practice sessions: after a level (or whenever it has sessions left), a bot walks to the nearest mob that
gives practice (act flag Practice: guildmasters, the priest of Seth by Mud School) and practises what keeps it
alive and levelling, in its class's order of priority.

``practice`` with no argument lists what the bot knows (PracticeTabulator.cpp: three columns of "name  NN%", then
"You have N practice sessions left."); ``practice '<skill>'`` spends one session there (act_info.cpp do_practice)
and needs a practice mob in the room.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from xania_explorer import api

from xania_sim.activities.move import Navigator

ENOUGH = 75  # stop practising a skill at this percentage (the rest comes from use)
TABLE = re.compile(r"([a-z][a-z' ]*?[a-z])\s+(\d{1,3})%")
SESSIONS = re.compile(r"You have (\d+) practice sessions? left")
PRACTICE_REPLY = (r"You practice|You are now learned|already learned|You can't practice that|"
                  r"no practice sessions left|You can't do that here|In your dreams")
PRIORITY = {
    "knight": ["sword", "parry", "shield block", "second attack", "enhanced damage", "dodge", "rescue", "kick",
               "bash", "third attack", "dagger", "mace"],
    "barbarian": ["axe", "sword", "parry", "second attack", "enhanced damage", "dodge", "berserk", "kick", "bash",
                  "third attack", "mace", "dagger"],
    "cleric": ["mace", "cure light", "armor", "bless", "cure serious", "shield block", "parry", "dodge",
               "cause light", "cure critical", "heal", "second attack"],
    "mage": ["dagger", "magic missile", "chill touch", "burning hands", "armor", "dodge", "shocking grasp",
             "lightning bolt", "colour spray", "fireball", "second attack"],
}


@dataclass
class PracticeReport:
    sessions_before: int = 0
    sessions_left: int = 0
    practised: dict[str, int] = field(default_factory=dict)  # skill -> sessions spent
    room: int | None = None
    reason: str = ""


def parse_table(text: str) -> tuple[dict[str, int], int]:
    """({skill: percent}, sessions left) from ``practice`` output."""
    skills = {m.group(1).strip(): int(m.group(2)) for m in TABLE.finditer(text)}
    m = SESSIONS.search(text)
    return skills, int(m.group(1)) if m else 0


def plan(cls: str, skills: dict[str, int], sessions: int) -> list[str]:
    """Which skills to practise, one entry per session, best first: the class's priorities that the bot knows and
    hasn't got to ``ENOUGH``, each in turn (round robin, so the top few all improve)."""
    wanted = [s for s in PRIORITY.get(cls, []) if s in skills and skills[s] < ENOUGH][:4]
    if not wanted:
        wanted = sorted((s for s, p in skills.items() if p < ENOUGH), key=lambda s: skills[s])[:3]
    out, i = [], 0
    while wanted and len(out) < sessions:
        out.append(wanted[i % len(wanted)])
        i += 1
    return out


def practice_rooms(world) -> list[int]:
    from xania_explorer.assess import mobs as M

    rooms = []
    for info in M.collect(world):
        if "Practice" in str(getattr(info.mob, "act_names", "")) and "Aggressive" not in str(info.mob.act_names):
            rooms += [s.room_vnum for s in info.spawns]
    return sorted(set(rooms))


async def practise(bot, nav: Navigator, world, cls: str, tries: int = 4) -> PracticeReport:
    r = await bot.do("practice", expect=SESSIONS)
    skills, sessions = parse_table(r.text)
    rep = PracticeReport(sessions, sessions)
    todo = plan(cls, skills, sessions)
    if not todo:
        rep.reason = "nothing to practise" if sessions else "no sessions"
        return rep
    if nav.here is None:
        await nav.locate()
    dist = api.distances(world, nav.here) if nav.here is not None else {}
    rooms = sorted((v for v in practice_rooms(world) if v in dist), key=lambda v: dist[v])
    for room in rooms[:tries]:
        walk = await nav.walk_to(room)
        if not walk.reached:
            continue
        while todo:
            skill = todo[0]
            r = await bot.do(f"practice '{skill}'", expect=PRACTICE_REPLY)
            if "can't do that here" in r.text:
                break  # the practice mob isn't here (dead, wandered): try the next room
            todo.pop(0)
            if re.search(r"You practice|You are now learned", r.text):
                rep.practised[skill] = rep.practised.get(skill, 0) + 1
                rep.sessions_left -= 1
            elif "no practice sessions" in r.text:
                todo.clear()
            elif "already learned" in r.text or "can't practice" in r.text:
                todo = [s for s in todo if s != skill]
        if not todo:
            rep.room = room
            rep.reason = "done"
            return rep
    rep.reason = "no practice mob reachable"
    return rep
