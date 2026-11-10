"""Getting around: knowing where the bot is, planning routes on Explorer's map, and walking them.

The navigator keeps a belief about the bot's room (``here``). Each move it expects the room the map says the exit
leads to and checks the room it sees against that; if they disagree it relocates by fingerprint, preferring rooms
near where it was. When it can't tell, it recalls to the Midgaard temple, which it always recognises.

Routes avoid danger (``danger_rooms``: where aggressive mobs that would attack spawn, unless Explorer says the fight
is comfortably survivable) and rooms the bot can't enter (air without flying, deep water without a boat). Exits that
turn out to be blocked (locked doors, guild rooms, private rooms...) are remembered and routed around.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter, deque
from dataclasses import dataclass, field

from xania_explorer import api
from xania_explorer.areafile import flags as F

from xania_sim.record.rooms import Fingerprints, RoomView, parse_view, visible_to
from xania_sim.core.session import Bot, Response

RECALL = api.RECALL_ROOM
AIR, DEEP_WATER = F.SECTORS.index("Air"), F.SECTORS.index("NonSwimmableWater")
SAFE_SHARE = 0.5  # survival-first: a fight is "safe" if it's predicted to cost under half the bot's HP

# Refusals from move_char (xania/src/act_move.cpp) and position checks, and what they mean for the route.
BLOCKED = re.compile(r"Alas, you cannot go that way|The \w+ is closed|protected from trespass|That room is private|"
                     r"You aren't allowed in there|Only members of the .* may enter|You can't fly|"
                     r"You need a boat to go there|You may not pass")
LOCKED = re.compile(r"It's locked|You can't do that")
EXHAUSTED = re.compile(r"You are too exhausted")
FIGHTING = re.compile(r"No way! +You are still fighting")
# arriving in a room where something attacks at once (an aggressive mob): room_look shows "... fighting YOU!"
AMBUSHED = re.compile(r" is here, fighting YOU!")
ASLEEP = re.compile(r"In your dreams, or what\?|Nah\.\.\. You feel too relaxed")

# What each command's own reply looks like (Bot.do reads past unprompted output until one appears).
ROOM_REPLY = r"^\[Exits:|It is pitch black|You can't see anything, you're sleeping"
MOVE_REPLY = re.compile("|".join([ROOM_REPLY, BLOCKED.pattern, EXHAUSTED.pattern, FIGHTING.pattern, ASLEEP.pattern,
                                  r"You can't move", r"You're too"]), re.M)
OPEN_REPLY = r"^Ok\.|It's already open|It's locked|You can't do that|Open what|That's not a container"
DOWN = re.compile(r"You're resting|You're sleeping|Try standing up|In your dreams|You're sitting")
RECALL_REPLY = re.compile(ROOM_REPLY + r"|You are completely lost|You are there already|has forsaken you|"
                          r"finish this fight first|You failed!|" + DOWN.pattern, re.M)
POSITION_REPLY = r"^You (rest|sit down|stand up|stop resting|wake|are already|can't wake|go to sleep)|^You're "
TIME_REPLY = r"o'clock"
FLEE_REPLY = (r"You flee from combat|PANIC! You couldn't escape|You aren't fighting anyone|too lethargic to flee|"
              r"too stunned|You're resting|You're sleeping|Try standing up|You have been KILLED")


class NavError(Exception):
    pass


@dataclass
class WalkResult:
    reached: bool
    steps: int
    here: int | None
    reason: str = ""


@dataclass
class Coverage:
    area: str
    rooms: list[int]
    visited: set[int] = field(default_factory=set)
    skipped: dict[int, str] = field(default_factory=dict)  # room -> why it wasn't visited

    @property
    def share(self) -> float:
        return len(self.visited & set(self.rooms)) / len(self.rooms) if self.rooms else 1.0


def danger_rooms(world, level: int, cls: str = "knight") -> set[int]:
    """Rooms where an aggressive mob that would attack a level-``level`` player spawns (aggr_update, update.cpp:
    mob level >= player level - 5, not wimpy), unless the fight is predicted to cost under ``SAFE_SHARE`` of HP."""
    from xania_explorer.assess import mobs as M

    out: set[int] = set()
    for info in M.collect(world):
        mob = info.mob
        acts = set(mob.act_names)
        if "Aggressive" not in acts or "Wimpy" in acts or mob.level < level - 5:
            continue
        try:
            f = api.predict_fight(world, cls, max(1, level), mob.vnum)
            if not f.deadly and f.hp_lost_share < SAFE_SHARE:
                continue
        except (KeyError, ValueError):
            pass  # not modelled: treat as dangerous
        out.update(s.room_vnum for s in info.spawns)
    return out


class Navigator:
    def __init__(self, bot: Bot, world, fingerprints: Fingerprints | None = None, *, avoid: set[int] | None = None,
                 can_fly: bool = False, has_boat: bool = False, level: int | None = None):
        self.bot, self.world = bot, world
        self.level = level  # the bot's level, if known: rooms it can't see (NewbiesOnly above 5...) can't be entered
        self.fp = fingerprints or Fingerprints(world)
        self.avoid = set(avoid or ())
        self.can_fly, self.has_boat = can_fly, has_boat
        self.here: int | None = None
        self.blocked: dict[tuple[int, int], str] = {}  # (room, direction) -> why
        self.visited: set[int] = set()
        self.escapes: Counter = Counter()  # escape() outcomes (fled, died, calm), for reports
        # awaited with the room's text when something attacks the bot as it arrives; default: escape
        self.on_ambush = None
        self.recalls = 0  # every recall, for the loop guard
        self.loops: list[str] = []  # walks given up as loops ("looping at room 3001 (5 replans, 4 recalls)")

    # -- where am I ------------------------------------------------------------------------------
    def _arrive(self, vnum: int | None) -> None:
        self.here = vnum
        if vnum is not None:
            self.visited.add(vnum)

    def _near(self, vnum: int | None, radius: int = 3) -> set[int]:
        if vnum is None:
            return set()
        return {v for v, d in api.distances(self.world, vnum).items() if d <= radius}

    def recognise(self, view: RoomView | None, expected: int | None = None) -> int | None:
        if view is None or view.dark:
            return expected if view is not None and view.dark else None  # dark: trust dead reckoning
        if expected is not None and self.fp.matches(expected, view):
            return expected
        near = self.fp.candidates(view, self._near(self.here))
        if len(near) == 1:
            return near[0]
        found = self.fp.candidates(view)
        return found[0] if len(found) == 1 else None

    async def locate(self) -> int | None:
        """Look, and work out the room. Recalls to the temple if the room can't be told apart from others."""
        r = await self.bot.do("look", expect=ROOM_REPLY)
        self._arrive(self.recognise(parse_view(r.text), self.here))
        if self.here is None:
            await self.recall()
        return self.here

    async def stand(self) -> None:
        """Get up (waking first if asleep). Mid-fight this puts the bot back on its feet, so it can flee."""
        r = await self.bot.do("stand", expect=POSITION_REPLY)
        if re.search(r"You can't wake|In your dreams", r.text):
            await self.bot.do("wake", expect=POSITION_REPLY)
            await self.bot.do("stand", expect=POSITION_REPLY)

    async def recall(self, _escaping: bool = False) -> None:
        self.recalls += 1
        r = await self.bot.do("recall", expect=RECALL_REPLY)
        if DOWN.search(r.text):
            await self.stand()
            r = await self.bot.do("recall", expect=RECALL_REPLY)
        if "You failed!" in r.text or FIGHTING.search(r.text) or "finish this fight" in r.text:
            if _escaping:
                raise NavError(f"{self.bot.name} can't recall during a fight")
            await self.escape()  # attacked: get out of the fight first (escape recalls once clear)
            return
        if "You are there already" in r.text:
            self._arrive(RECALL)
            return
        view = parse_view(r.text)
        if view and self.fp.matches(RECALL, view):
            self._arrive(RECALL)
        else:
            self._arrive(self.recognise(view))
        if self.here is None:
            raise NavError(f"{self.bot.name} recalled but couldn't tell where it is: {r.text[-200:]!r}")

    async def escape(self, tries: int = 8) -> str:
        """Get out of an unwanted fight (survival first): flee until it works, then recall to safety; the room we
        fled from joins ``avoid``. Returns "fled", "died" (killed before getting away: the bot is back at the
        temple) or "calm" (no longer fighting). Raises NavError only if every attempt fails."""
        fled_from = self.here
        outcome = ""
        for _ in range(tries):
            r = await self.bot.do("flee", expect=FLEE_REPLY)
            if "You have been KILLED" in r.text:
                outcome = "died"
            elif "You flee from combat" in r.text:
                outcome = "fled"
            elif "aren't fighting anyone" in r.text:
                outcome = "calm"
            elif DOWN.search(r.text):
                # tripped, bashed or asleep mid-fight: the fight goes on while the bot is down, so get up and retry
                await self.stand()
            elif EXHAUSTED.search(r.text):
                # no moves to run with: pray instead (recall works mid-fight, by chance, and costs xp)
                rr = await self.bot.do("recall", expect=RECALL_REPLY)
                if "You have been KILLED" in rr.text:
                    outcome = "died"
                elif parse_view(rr.text) is not None or "You are there already" in rr.text:
                    view = parse_view(rr.text)
                    self._arrive(RECALL if view is None or self.fp.matches(RECALL, view) else self.recognise(view))
                    if fled_from is not None:
                        self.avoid.add(fled_from)
                    return "fled"
            elif "too stunned" in r.text:
                heard = await self.bot.listen(4)  # wait to recover (or to die)
                if "You have been KILLED" in heard.text:
                    outcome = "died"
            if outcome:
                break
        if not outcome:
            raise NavError(f"{self.bot.name} couldn't escape a fight")
        self.escapes[outcome] += 1
        if fled_from is not None and outcome != "calm":
            self.avoid.add(fled_from)
        self.here = None
        if outcome == "died":
            await self.bot.do("stand", expect=POSITION_REPLY)
            await self.locate()
        else:
            try:
                await self.recall(_escaping=True)
            except NavError:
                await self.locate()
        return outcome

    # -- planning ---------------------------------------------------------------------------------
    def _enterable(self, vnum: int) -> bool:
        room = self.world.rooms[vnum]
        sector = room.sector
        if (sector == AIR and not self.can_fly) or (sector == DEEP_WATER and not (self.can_fly or self.has_boat)):
            return False
        return visible_to(room, self.level)

    def plan(self, goal: int, start: int | None = None) -> list[api.Step] | None:
        """Shortest route avoiding danger, unenterable rooms and known blocked exits (the goal itself may be in a
        dangerous room only if it's the goal)."""
        start = self.here if start is None else start
        if start is None:
            return None
        if start == goal:
            return []
        came: dict[int, api.Step | None] = {start: None}
        queue = deque([start])
        while queue:
            v = queue.popleft()
            for s in api.steps_from(self.world, v):
                to = s.to_vnum
                if to in came or (v, s.direction) in self.blocked or not self._enterable(to):
                    continue
                if to in self.avoid and to != goal:
                    continue
                came[to] = s
                if to == goal:
                    route = []
                    while (st := came[to]) is not None:
                        route.append(st)
                        to = st.from_vnum
                    return route[::-1]
                queue.append(to)
        return None

    # -- moving -------------------------------------------------------------------------------------
    rest_timeout = 600.0  # longest wait for moves; a Leveller lowers it to what's left of its budget

    async def _rest_until(self, share: float = 0.9, timeout: float | None = None) -> None:
        """Sleep until moves are back to ``share`` of max (regen comes on ticks; sleeping adds the full Dex bonus to
        move regen, resting half: update.cpp move_gain). If something attacks meanwhile, wake and escape."""
        from xania_sim.activities.fight import attacked

        r = await self.bot.do("sleep", expect=POSITION_REPLY + "|You are already fighting")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(timeout or self.rest_timeout, self.rest_timeout)
        hit = attacked(r.text)
        while not hit and self.bot.prompt.moves < share * self.bot.prompt.max_moves and loop.time() < deadline:
            before = self.bot.prompt.hp
            await asyncio.sleep(5)
            r = await self.bot.do("time", expect=TIME_REPLY)  # any harmless command, to read a fresh prompt
            hit = attacked(r.text, before, self.bot.prompt.hp)
        await self.bot.do("wake", expect=POSITION_REPLY)
        await self.bot.do("stand", expect=POSITION_REPLY)
        if hit:
            await self.escape()

    async def step(self, s: api.Step) -> bool:
        """Take one step. Returns True on arriving where expected; otherwise updates ``here``/``blocked``."""
        if s.door:
            r = await self.bot.do(f"open {s.command}", expect=OPEN_REPLY)
            if LOCKED.search(r.text):
                self.blocked[(s.from_vnum, s.direction)] = "locked"
                return False
        for _ in range(2):
            r = await self.bot.do(s.command, expect=MOVE_REPLY)
            if EXHAUSTED.search(r.text):
                await self._rest_until()
                continue
            if ASLEEP.search(r.text):
                await self.bot.do("stand", expect=POSITION_REPLY)
                continue
            break
        if FIGHTING.search(r.text):  # something attacked while the bot stood here
            if self.on_ambush is not None:
                await self.on_ambush(r.text)
            else:
                await self.escape()
            return False
        view = parse_view(r.text)
        if view is None:
            m = BLOCKED.search(r.text)
            if m:
                self.blocked[(s.from_vnum, s.direction)] = m.group(0)
            else:  # an unknown reply: don't trust where we are
                self.here = None
            return False
        self._arrive(self.recognise(view, s.to_vnum))
        if AMBUSHED.search(r.text):  # attacked on arrival: by default get out (the room joins avoid)
            if self.on_ambush is not None:
                await self.on_ambush(r.text)
            else:
                await self.escape()
            return False
        return self.here == s.to_vnum

    REPLANS_PER_ROOM = 4  # a walk that re-plans from the same room more often than this is going round in circles
    RECALLS_PER_WALK = 3

    async def walk_to(self, goal: int, max_steps: int = 400) -> WalkResult:
        """Walk to ``goal``, re-planning after a failed step. Gives up on a loop (the same room again and again, or
        recall after recall: e.g. a room it can't recognise), so a bad map entry costs seconds, not all its moves."""
        if self.here is None:
            await self.locate()
        steps, replans, recalls0 = 0, Counter(), self.recalls
        while steps < max_steps:
            if self.here == goal:
                return WalkResult(True, steps, self.here)
            if self.here is None:
                await self.locate()
            replans[self.here] += 1
            if replans[self.here] > self.REPLANS_PER_ROOM or self.recalls - recalls0 > self.RECALLS_PER_WALK:
                why = (f"looping at room {self.here} ({replans[self.here]} re-plans, "
                       f"{self.recalls - recalls0} recalls, going to {goal})")
                self.loops.append(why)
                return WalkResult(False, steps, self.here, why)
            route = self.plan(goal)
            if route is None:
                return WalkResult(False, steps, self.here, "no route")
            for s in route:
                ok = await self.step(s)
                steps += 1
                if not ok or steps >= max_steps:
                    break  # re-plan from wherever we are
        return WalkResult(self.here == goal, steps, self.here, "" if self.here == goal else "step limit")

    async def explore(self, area: str, max_steps: int = 2000, seconds: float | None = None) -> Coverage:
        """Visit every room of an area the bot can safely reach, nearest first. ``seconds``: stop starting new walks
        after this long (checked between walks, so a fight on the way is never cut short)."""
        deadline = None if seconds is None else asyncio.get_running_loop().time() + seconds
        rooms = api.rooms_in(self.world, area)
        cov = Coverage(area, rooms)
        if self.here is None:
            await self.locate()
        todo = set(rooms) - self.visited
        for v in todo & self.avoid:
            cov.skipped[v] = "dangerous"
        todo -= self.avoid
        steps = 0
        while todo and steps < max_steps:
            todo -= self.visited
            if not todo:
                break
            if deadline is not None and asyncio.get_running_loop().time() > deadline:
                for v in todo:
                    cov.skipped.setdefault(v, "out of time")
                break
            dist = api.distances(self.world, self.here) if self.here is not None else {}
            # nearest by map distance, then check it's plannable under our constraints
            target, route = None, None
            for v in sorted(todo, key=lambda v: dist.get(v, 10**6)):
                route = self.plan(v)
                if route is not None:
                    target = v
                    break
                cov.skipped[v] = "no safe route"
            if target is None:
                break
            todo.discard(target)
            result = await self.walk_to(target, max_steps=min(400, max_steps - steps))
            steps += result.steps
            if not result.reached:
                cov.skipped[target] = result.reason or "blocked"
                if len(self.loops) >= 3:  # navigation itself is failing here: stop rather than keep circling
                    break
        cov.visited = self.visited & set(rooms)
        for v in cov.visited:
            cov.skipped.pop(v, None)
        return cov
