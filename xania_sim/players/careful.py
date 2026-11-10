"""Levelling, survival first: go where the safe fights are, fight them, rest when hurt, repeat.

Targets are mobs ``fight.safe_targets`` allows at the bot's level (Explorer's prediction, overridden by what bots
have seen). The bot visits the rooms where they spawn, nearest and most rewarding first, avoiding rooms with
aggressive mobs it can't handle (``nav.danger_rooms``), and fights one mob at a time. Below ``REST_BELOW`` of its
HP it recalls to the temple and sleeps; if a fight goes badly it flees and recalls, and never goes back to that
room. Every fight goes into the shared fight log.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

from xania_explorer import api

from xania_sim.activities.fight import FLEE_AT, SAFE_SHARE, FightLog, FightResult, MobIndex, REST_BELOW, fight, recover, safe_targets
from xania_sim.activities.move import ROOM_REPLY, Navigator, danger_rooms
from xania_sim.core.session import Bot

LEVEL = re.compile(r"Level:?\s+(\d+)")
TOGGLES = {"autogold": ("Automatic gold looting set", "Autogold removed"),
           "autosac": ("Automatic corpse sacrificing set", "Autosacrificing removed")}


@dataclass
class LevelReport:
    start_level: int
    level: int = 0
    kills: int = 0
    deaths: int = 0
    flees: int = 0
    xp: int = 0
    seconds: float = 0.0
    reason: str = ""
    fights: list[FightResult] = field(default_factory=list)


async def set_toggle(bot: Bot, command: str, on: bool) -> None:
    on_text, off_text = TOGGLES[command]
    for _ in range(2):
        r = await bot.do(command, expect=f"{on_text}|{off_text}")
        if (on_text in r.text) == on:
            return


class Leveller:
    def __init__(self, bot: Bot, world, cls: str, nav: Navigator | None = None, log: FightLog | None = None,
                 mobs: MobIndex | None = None, recorder=None):
        self.bot, self.world, self.cls = bot, world, cls
        self.recorder = recorder  # events.Recorder: told the target of each fight and the level after each check
        self.nav = nav or Navigator(bot, world)
        self.log, self.mobs = log, mobs or MobIndex(world)
        self.never: set[int] = set()  # rooms the bot fled from
        self._targets: dict[int, dict[int, float]] = {}
        self._danger: dict[int, set[int]] = {}
        self._damage: dict[tuple[int, int], float] = {}
        self._spawns: dict[int, list[int]] | None = None

    async def level(self) -> int:
        r = await self.bot.do("score", expect=LEVEL)
        m = LEVEL.search(r.text)
        level = int(m.group(1)) if m else 0
        if self.recorder is not None and level:
            self.recorder.level = level
        return level

    def targets(self, level: int) -> dict[int, float]:
        if level not in self._targets:
            self._targets[level] = safe_targets(self.world, self.cls, level, self.log)
        return self._targets[level]

    def spawns(self) -> dict[int, list[int]]:
        if self._spawns is None:
            from xania_explorer.assess import mobs as M

            self._spawns = {i.mob.vnum: [s.room_vnum for s in i.spawns] for i in M.collect(self.world)}
        return self._spawns

    def hunting_rooms(self, level: int, limit: int = 8) -> list[int]:
        """Rooms where safe targets spawn, best value per distance first."""
        targets = self.targets(level)
        value: dict[int, float] = {}
        for mob, v in targets.items():
            for room in self.spawns().get(mob, []):
                if room not in self.nav.avoid and room not in self.never:
                    value[room] = value.get(room, 0.0) + v
        dist = api.distances(self.world, self.nav.here) if self.nav.here is not None else {}
        reachable = [r for r in value if r in dist or not dist]
        return sorted(reachable, key=lambda r: -value[r] / (1 + dist.get(r, 50) / 8))[:limit]

    def crowd_damage(self, mobs: list[int], level: int) -> float:
        """Predicted HP lost if every mob in the room joins the fight (each one's straight-fight damage, summed)."""
        total = 0.0
        for v in mobs:
            key = (level, v)
            if key not in self._damage:
                try:
                    self._damage[key] = api.predict_fight(self.world, self.cls, level, v).damage_taken
                except (KeyError, ValueError):
                    self._damage[key] = 0.0  # not a fighter (shopkeepers, pets)
            total += self._damage[key]
        return total

    async def rest(self, timeout: float = 900) -> None:
        await self.nav.recall()
        await recover(self.bot, timeout=timeout, on_attack=self.nav.escape)

    flee_at = FLEE_AT  # subclasses (or callers) can set their own
    practise_skills = True  # spend practice sessions at the nearest guildmaster at the start and after each level

    async def train(self) -> None:
        if not self.practise_skills:
            return
        from xania_sim.activities.move import NavError
        from xania_sim.activities.practice import practise

        try:
            self.last_practice = await practise(self.bot, self.nav, self.world, self.cls)
        except NavError:
            await self.nav.locate()

    async def run(self, goal_level: int, budget: float = 1800, stop=None) -> LevelReport:
        """Level until ``goal_level``, ``budget`` seconds, or ``stop()`` turns true (checked between fights)."""
        t0 = time.monotonic()
        for toggle in TOGGLES:
            await set_toggle(self.bot, toggle, True)
        level = await self.level()
        rep = LevelReport(level, level)
        if self.nav.here is None:
            await self.nav.locate()
        await self.train()
        empty_sweeps = 0
        while level < goal_level:
            if time.monotonic() - t0 > budget:
                rep.reason = "out of time"
                break
            if stop is not None and stop():
                rep.reason = "stopped"
                break
            left = budget - (time.monotonic() - t0)
            self.nav.rest_timeout = max(30.0, left)  # no rest outlasts the budget
            if self.bot.prompt.hp_share < REST_BELOW:
                await self.rest(timeout=max(30.0, left))
            if level not in self._danger:
                self._danger[level] = danger_rooms(self.world, level, self.cls)
            self.nav.avoid = self._danger[level] | self.never
            targets = self.targets(level)
            if not targets:
                rep.reason = f"no safe targets at level {level}"
                break
            fought = False
            for room in self.hunting_rooms(level):
                walk = await self.nav.walk_to(room)
                if not walk.reached:
                    continue
                look = await self.bot.do("look", expect=ROOM_REPLY)
                everyone = self.mobs.in_view(look.text, self.nav.here)
                present = [v for v in everyone if v in targets]
                if not present:
                    continue
                if self.crowd_damage(everyone, level) >= SAFE_SHARE * self.bot.prompt.hp:
                    continue  # others here could join in: not worth the risk
                mob = max(present, key=targets.get)
                here = self.nav.here
                if self.recorder is not None:
                    self.recorder.target = self.world.mobs[mob].short_descr
                r = await fight(self.bot, mob, self.mobs.keyword(mob), flee_at=self.flee_at, escape=self.nav.escape)
                if r.outcome == "absent":
                    continue
                fought = True
                rep.fights.append(r)
                rep.xp += r.xp
                if self.log is not None:
                    self.log.record(self.bot.name, self.cls, level, r)
                if r.outcome == "won":
                    rep.kills += 1
                elif r.outcome == "fled":
                    rep.flees += 1
                    if here is not None:
                        self.never.add(here)
                elif r.outcome == "died":
                    rep.deaths += 1
                    if here is not None:
                        self.never.add(here)
                    self.nav.here = None
                    await self.nav.locate()
                if r.levelled:
                    level = await self.level()
                    rep.level = level
                    await self.train()
                self._targets.pop(level, None)  # observations changed: recompute this level's targets
                break
            if not fought:
                empty_sweeps += 1
                if empty_sweeps >= 3:  # everything nearby is dead: wait for the areas to reset
                    await self.rest(timeout=max(30.0, budget - (time.monotonic() - t0)))
                    await asyncio.sleep(30)
                    empty_sweeps = 0
            else:
                empty_sweeps = 0
        rep.level = level
        rep.seconds = time.monotonic() - t0
        if level >= goal_level:
            rep.reason = "reached the goal"
        return rep
