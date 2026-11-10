"""The area trial: ``python -m xania_sim area NAME``. How does an area play for the characters it's meant for, and
for the ones just outside its range?

Levels come from the area's header (``{lo hi}``) when it has one, else from Explorer's combat mob levels: **low-5,
low, median, high, high+5** (clamped to 1-91), where median is the median level of its combat mobs. At each level
one careful player per class (``--classes`` for fewer), staged with a shop kit and the natural skill loadout
(``--kit``), is put at the area's entrance and plays it for ``--minutes``: explores every room it can safely
reach, fights mobs predicted safe that spawn there (fleeing and resting as usual), loots the corpses, and sells
the loot at a shop in the area (or the nearest one). The classes of one level play at the same time, so they share
the area as players would.

By default the whole world is loaded; ``--cut-down`` loads only the area, Limbo, Midgaard, Mud School and the
files without rooms (helps, socials), for a new area that isn't linked in yet or to keep other areas out of it.

The report puts the levels and classes side by side, observed against Explorer's predictions: rooms reached,
kills, deaths, flees, xp per minute, HP lost per kill, gold. Findings: a crash, xania's BUG lines, a careful player
dying at a level inside the area's range, and rooms Explorer says are reachable that no bot could get to.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from xania_explorer import api

from xania_sim.activities import trade as T
from xania_sim.activities.fight import FightLog, FightResult, MobIndex, safe_targets
from xania_sim.activities.items import Objects
from xania_sim.activities.loot import loot
from xania_sim.activities.move import Navigator, danger_rooms
from xania_sim.activities.tactics import Tactics
from xania_sim.core import world as W
from xania_sim.core.director import Director
from xania_sim.core.names import bot_names, mob_words
from xania_sim.core.server import TestMud, checkout_path
from xania_sim.core.session import Bot
from xania_sim.players.careful import Leveller, set_toggle
from xania_sim.record.findings import Finding, bug_line_findings, crash_finding, transcript_tails
from xania_sim.record.heartbeat import Heartbeat, Phases, null_beat
from xania_sim.record.result import Outcome
from xania_sim.record.rooms import Fingerprints

HERO = 91  # the highest mortal level
TEMPLE = 3001
CUT_DOWN_KEEP = ("Limbo.are", "Midgaard.are", "MudSchool.are")
UNREACHED = {"no route", "blocked", "step limit"}  # walking failed where the map had a route


# -- planning -----------------------------------------------------------------------------------------------------

@dataclass
class AreaPlan:
    name: str
    filename: str
    advertised: tuple[int, int] | None
    median: int
    span: tuple[int, int]
    levels: dict[str, int]  # label (low-5, low, median, high, high+5) -> level, duplicates dropped
    rooms: list[int]
    entry: int
    mobs: set[int]  # combat mob vnums that spawn in the area
    shops: list[int]  # shop rooms in the area
    aggressive_rooms: set[int] = field(default_factory=set)  # area rooms where an aggressive mob spawns


def levels_for(advertised: tuple[int, int] | None, median: int, span: tuple[int, int]) -> dict[str, int]:
    """{label: level}, lowest first. When two land on the same level (clamped, or a one-level range), the range's
    own labels (low, median, high) win over the ones outside it."""
    lo, hi = advertised if advertised else span
    want = {"low": lo, "median": median, "high": hi, "low-5": lo - 5, "high+5": hi + 5}
    out: dict[str, int] = {}
    for label, lv in want.items():
        lv = max(1, min(HERO, lv))
        if lv not in out.values():
            out[label] = lv
    return dict(sorted(out.items(), key=lambda kv: kv[1]))


def plan_area(world, area: str) -> AreaPlan:
    from xania_explorer.assess import mobs as M

    a = world.area_named(area)
    if a is None:
        raise KeyError(f"no area named {area!r}")
    rooms = api.rooms_in(world, a.filename)
    inside = set(rooms)
    try:
        p = api.area_profile(world, a.filename)
        advertised, median, span = p.advertised, p.level, p.span
    except KeyError:  # no combat mobs: header levels or nothing
        h = a.file.header
        advertised = None if h.all_levels else (h.min_level, h.max_level)
        median = advertised[0] if advertised else 1
        span = advertised or (1, 1)
    dist = api.distances(world, TEMPLE)
    doors = [s for s in api.entrances(world, a.filename) if s.from_vnum in dist]
    entry = min(doors, key=lambda s: dist[s.from_vnum]).to_vnum if doors else rooms[0]
    mobs = {i.mob.vnum for i in M.collect(world) if any(s.room_vnum in inside for s in i.spawns)}
    shops = sorted(r for r in T.shop_rooms(world) if r in inside)
    aggressive = {s.room_vnum for i in M.collect(world) if "Aggressive" in i.mob.act_names
                  for s in i.spawns if s.room_vnum in inside}
    return AreaPlan(a.name, a.filename, advertised, median, span, levels_for(advertised, median, span), rooms, entry,
                    mobs, shops, aggressive)


def cut_down_list(world, filename: str) -> list[str]:
    """The cut-down world's area.lst: files without rooms (helps, socials), the hub areas and this one, in the
    checkout's load order (vnum ranges and resets depend on it)."""
    roomless = {f.filename for f in world.files if not f.rooms}
    keep = roomless | set(CUT_DOWN_KEEP) | {filename}
    return [n for n in world.load_order if n in keep]


def cut_down_world(names: list[str], folder: Path):
    """An Explorer World of just these files (copied next to the run), so plans match what the MUD loads."""
    from xania_explorer.paths import XaniaPaths

    root = Path(checkout_path())
    area = folder / "area"
    area.mkdir(parents=True, exist_ok=True)
    for n in names:
        shutil.copyfile(root / "area" / n, area / n)
    (area / "area.lst").write_text("\n".join(names + ["$"]) + "\n", encoding="latin-1")
    return api.load_world(XaniaPaths(root, area, root / "src"))


# -- predictions --------------------------------------------------------------------------------------------------

@dataclass
class Prediction:
    safe: list[int]  # area mobs predicted safe to fight
    deadly_share: float  # share of the area's population whose fight is predicted deadly
    hp_per_kill: float | None  # mean predicted share of max HP lost per safe kill
    danger_rooms: int  # area rooms the careful player avoids (aggressive mobs it can't handle)


def predict(world, plan: AreaPlan, cls: str, level: int) -> Prediction:
    from xania_explorer.assess import combat as CB

    safe = [m for m in safe_targets(world, cls, level) if m in plan.mobs]
    pop = deadly = 0
    for m in CB.analyse(world).mobs:
        if m.mob.vnum not in plan.mobs or m.info.population <= 0:
            continue
        try:
            f = api.predict_fight(world, cls, level, m.mob.vnum)
        except (KeyError, ValueError):
            continue
        pop += m.info.population
        deadly += m.info.population * bool(f.deadly)
    shares = []
    for m in safe:
        with contextlib.suppress(KeyError, ValueError):
            shares.append(api.predict_fight(world, cls, level, m).hp_lost_share)
    danger = len(danger_rooms(world, level, cls) & set(plan.rooms))
    return Prediction(safe, deadly / pop if pop else 0.0, sum(shares) / len(shares) if shares else None, danger)


# -- one careful player -------------------------------------------------------------------------------------------

@dataclass
class AreaRun:
    cls: str
    label: str
    level: int
    bot: str = ""
    staged: bool = False
    stage_notes: list[str] = field(default_factory=list)
    predicted: Prediction | None = None
    visited: set[int] = field(default_factory=set)
    skipped: dict[int, str] = field(default_factory=dict)
    fights: list[FightResult] = field(default_factory=list)
    killers: list[str] = field(default_factory=list)
    gold_start: int | None = None
    gold_end: int | None = None
    looted: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)
    shop: str = ""  # where it traded
    sold: list[str] = field(default_factory=list)
    sold_gold: int = 0
    for_sale: int = 0
    seconds: float = 0.0
    error: str = ""
    phases: dict[str, int] = field(default_factory=dict)  # seconds per phase: explore, hunt, trade
    loops: list[str] = field(default_factory=list)  # walks the navigator gave up on as loops
    stopped: str = ""  # why its play ended (the hunt's reason, or "over its time cap")

    @property
    def xp(self) -> int:
        return sum(f.xp for f in self.fights)

    escapes: Counter = field(default_factory=Counter)  # escapes outside fights it chose (ambushes, while resting)
    chosen: Counter = field(default_factory=Counter)  # outcomes of fights it chose, to tell the two apart

    def count(self, outcome: str) -> int:
        """Fights by outcome; flees and deaths include escapes from fights it didn't choose."""
        return sum(1 for f in self.fights if f.outcome == outcome) + (self.escapes[outcome] if outcome != "won" else 0)

    @property
    def hp_per_kill(self) -> float | None:
        won = [f for f in self.fights if f.outcome == "won" and f.max_hp]
        return sum(max(0, f.hp_before - f.hp_after) / f.max_hp for f in won) / len(won) if won else None

    def summary(self, rooms: int) -> dict:
        p = self.predicted
        mins = self.seconds / 60 or 1
        return {"class": self.cls, "level": self.level, "band": self.label, "staged": self.staged,
                "rooms": f"{len(self.visited)}/{rooms}", "kills": self.count("won"), "deaths": self.count("died"),
                "flees": self.count("fled"), "xp": self.xp, "xp_per_minute": round(self.xp / mins, 1),
                "hp_per_kill": _pct(self.hp_per_kill), "hp_per_kill_predicted": _pct(p.hp_per_kill if p else None),
                "safe_targets_predicted": len(p.safe) if p else None,
                "deadly_share_predicted": _pct(p.deadly_share if p else None),
                "gold": None if None in (self.gold_start, self.gold_end) else self.gold_end - self.gold_start,
                "looted": len(self.looted), "sold": len(self.sold), "sold_gold": self.sold_gold,
                "shop": self.shop, "killers": self.killers, "error": self.error, "phases": self.phases,
                "loops": self.loops, "stopped": self.stopped, "escapes": dict(self.escapes)}


def _pct(x: float | None) -> float | None:
    return None if x is None else round(100 * x, 1)


class AreaPlayer(Leveller):
    """The careful levelling player, kept to one area: it hunts only mobs that spawn there, doesn't walk off to
    practise, and loots by hand (autoloot stays off: it crashes debug builds, xania#1)."""

    toggles = {"autogold": True, "autosac": False}
    practise_skills = False

    def __init__(self, *a, plan: AreaPlan, run: AreaRun, objects: Objects, **kw):
        super().__init__(*a, **kw)
        self.plan, self.run_, self.objects = plan, run, objects
        self.after_fight = self._after_fight

    async def rest(self, timeout: float = 900) -> None:
        """Rest where it is when that's a quiet room of the area: no aggressive mob spawns there (a far-off area
        makes the walk back from the temple eat most of the time and moves); otherwise recall as usual. Attacked
        while resting: escape. (Resting at 20% HP where trolls roam killed a level-15 mage.)"""
        from xania_sim.activities.fight import recover

        here = self.nav.here
        if here in set(self.plan.rooms) and here not in self.nav.avoid and here not in self.plan.aggressive_rooms:
            if await recover(self.bot, timeout=timeout, on_attack=self.nav.escape):
                return
        await super().rest(timeout)

    def spawns(self) -> dict[int, list[int]]:
        inside = set(self.plan.rooms)
        return {m: [r for r in rooms if r in inside] for m, rooms in super().spawns().items() if m in self.plan.mobs}

    async def _after_fight(self, r: FightResult, room: int | None) -> None:
        self.run_.fights.append(r)
        self.run_.chosen[r.outcome] += 1
        if r.outcome == "died":
            self.run_.killers.append(self.world.mobs[r.mob].short_descr if r.mob in self.world.mobs else str(r.mob))
        if r.outcome == "won":
            o = await loot(self.bot, self.world, sacrifice=True, objects=self.objects)
            self.run_.looted += o.data.get("taken", [])
            self.run_.refused.update(o.data.get("refused", {}))


async def _trade(run: AreaRun, bot: Bot, nav: Navigator, world, plan: AreaPlan) -> None:
    """Sell the loot: at a shop in the area if there is one, else the nearest. Nothing to sell: no trip (a shop
    outside a far-off area can be minutes away)."""
    objects = Objects(world)
    if not any(not objects.is_money(item) for item in run.looted):
        run.shop = "nothing to sell"
        return
    out = None
    for room in plan.shops:
        if (await nav.walk_to(room)).reached:
            stock = await T.list_stock(bot)
            if stock.ok:
                out = Outcome("trade", True, f"in the area (room {room})", stock.evidence,
                              {"room": room, **stock.data})
                break
    if out is None:
        out = await T.go_to_shop(nav, world)
    if not out.ok:
        run.shop = out.summary
        return
    run.shop = ("area shop " if out.data["room"] in plan.shops else "outside the area, room ") + str(out.data["room"])
    run.for_sale = len(out.data.get("stock", []))
    for item in dict.fromkeys(run.looted):
        if objects.is_money(item):
            continue
        s = await T.sell(bot, objects.keyword(item))
        if s.ok:
            run.sold.append(s.data["item"])
            run.sold_gold += s.data["gold"]


GRACE = 90  # seconds a bot may run over its play time (selling, quitting) before it's cut off


async def _play(run: AreaRun, bot: Bot, world, plan: AreaPlan, minutes: float, log: FightLog | None,
                beat=null_beat) -> None:
    """One careful player's session, cut off at ``minutes`` + GRACE whatever it's doing (a stuck wait or loop then
    costs at most that)."""
    t0 = time.monotonic()
    phases = Phases()
    nav = Navigator(bot, world, Fingerprints(world), level=run.level)
    nav.rest_timeout = minutes * 60 / 2  # no single wait for moves may take more than half the session
    who = f"{run.cls} {run.level}"
    try:
        await asyncio.wait_for(_session(run, bot, world, plan, minutes, log, nav, phases, beat, who),
                               timeout=minutes * 60 + GRACE)
    except asyncio.TimeoutError:
        run.stopped = f"over its time cap ({minutes:g} min + {GRACE} s)"
        beat(who, "cut off", phase=next(reversed(phases.seconds), "?"))
    except Exception as e:  # noqa: BLE001 - one bot failing mustn't stop the others
        run.error = f"{type(e).__name__}: {str(e)[:200]}"
        beat(who, "error", error=run.error)
    finally:
        run.seconds = time.monotonic() - t0
        run.phases = phases.rounded()
        run.loops = list(nav.loops)
        run.escapes = Counter(nav.escapes) - run.chosen  # a chosen fight that went badly escaped through nav too
        run.visited |= nav.visited & set(plan.rooms)


async def _session(run, bot, world, plan, minutes, log, nav, phases, beat, who) -> None:
    t0 = time.monotonic()
    nav.here = plan.entry
    await nav.locate()
    run.gold_start = bot.prompt.gold if bot.prompt else None
    nav.avoid = danger_rooms(world, run.level, run.cls)
    tactics = await Tactics.read(bot, run.cls, run.level, world=world)
    # made before exploring: it answers ambushes on the way (fights back when that's safe)
    player = AreaPlayer(bot, world, run.cls, nav, log, MobIndex(world), tactics=tactics, plan=plan, run=run,
                        objects=Objects(world))
    await set_toggle(bot, "autosac", False)  # corpses from ambush fights are looted too
    beat(who, "exploring", rooms=len(plan.rooms), avoid=len(nav.avoid & set(plan.rooms)))
    with phases("explore"):
        # at most half the session (a big area mustn't leave no time to hunt), checked between walks so a fight
        # on the way is never cut short
        cov = await nav.explore(plan.filename, max_steps=min(2000, 8 * len(plan.rooms)), seconds=minutes * 30)
        run.skipped = dict(cov.skipped)
    run.visited = nav.visited & set(plan.rooms)
    beat(who, "explored", reached=f"{len(run.visited)}/{len(plan.rooms)}", fights=len(run.fights),
         escapes=dict(nav.escapes), loops=len(nav.loops))
    left = minutes * 60 - (time.monotonic() - t0)
    if left > 20:
        with phases("hunt"):
            rep = await player.run(goal_level=HERO + 1, budget=left)
        run.stopped = rep.reason
    beat(who, "hunted", kills=run.count("won"), chosen=dict(run.chosen), escapes=dict(nav.escapes),
         stopped=run.stopped)
    with phases("trade"):
        await _trade(run, bot, nav, world, plan)
        await bot.do("score", expect=r"Level: \S*\d+")  # a fresh prompt for the gold
        run.gold_end = bot.prompt.gold if bot.prompt else None


# -- the trial ------------------------------------------------------------------------------------------------------

async def run_area(result, area: str, port: int = 9160, classes: list[str] | None = None,
                   levels: list[int] | None = None, minutes: float = 4, kit: str = "shop",
                   cut_down: bool = False, quick: bool = False, fail_fast: bool = False) -> list[Finding]:
    """``quick``: a preflight, one class at the first level for 1.5 minutes, to check everything works before a
    long run. ``fail_fast``: stop after a level where no bot won a fight (the rest would likely go the same way)."""
    from xania_explorer.source.cpp_tables import CLASSES

    from xania_sim.setup.stage import stage

    runs = Path("runs") / time.strftime("area-%Y%m%d-%H%M%S")
    world = W.load()
    a = world.area_named(area)
    if a is None:
        raise KeyError(f"no area named {area!r}")
    area_list = None
    if cut_down:
        area_list = cut_down_list(world, a.filename)
        world = cut_down_world(area_list, runs / "world")
    plan = plan_area(world, a.filename)
    bands = plan.levels if not levels else {f"level {lv}": lv for lv in levels}
    classes = classes or list(CLASSES)
    if quick:
        first = next(iter(bands))
        bands, classes, minutes = {first: bands[first]}, classes[:1], min(minutes, 1.5)
    beat = Heartbeat(runs, "area")
    beat("trial", "planned", area=plan.name, levels=", ".join(f"{k} {v}" for k, v in bands.items()),
         classes=", ".join(classes), minutes=minutes, quick=quick)
    result.measurements.update(area=plan.name, file=plan.filename, rooms=len(plan.rooms), entry=plan.entry,
                               advertised="-".join(map(str, plan.advertised)) if plan.advertised else "all",
                               median_mob_level=plan.median, levels=", ".join(f"{k} {v}" for k, v in bands.items()),
                               shops_in_area=len(plan.shops), world="cut-down" if cut_down else "full")
    repro = f"python -m xania_sim area '{plan.filename}'" + (" --cut-down" if cut_down else "")
    names = iter(bot_names("area", 500, mob_words(world)))
    found: list[Finding] = []
    all_runs: list[AreaRun] = []
    async with TestMud(port=port, area_list=area_list) as mud:
        d = await Director.create(mud)
        try:
            for label, level in bands.items():
                bots: list[tuple[AreaRun, Bot]] = []
                for cls in classes:  # staging goes through the one director connection: one at a time
                    run = AreaRun(cls, label, level)
                    bot = Bot(next(names), "areapass1", host=mud.host, port=mud.port, transcript_dir=runs)
                    run.bot = bot.name
                    run.predicted = predict(world, plan, cls, level)
                    try:
                        rep = await stage(mud, d, bot, level, kit, "natural", cls=cls, world=world)
                        run.staged = rep.ok
                        run.stage_notes = [f"{w.piece.name}: {w.reply}" for w in rep.failed][:4]
                        await d.goto(plan.entry)
                        await d.bring(bot.name)
                        await d.goto(TEMPLE)
                        bots.append((run, bot))
                    except Exception as e:  # noqa: BLE001
                        run.error = f"staging: {type(e).__name__}: {e}"
                        await bot.close()
                    all_runs.append(run)
                beat("trial", f"playing {label} ({level})", bots=len(bots))
                crashed = await _play_band(mud, [_play(run, bot, world, plan, minutes, None, beat)
                                                 for run, bot in bots])
                for run, bot in bots:
                    try:
                        await asyncio.wait_for(bot.quit(), timeout=20)
                    except Exception:  # noqa: BLE001
                        await bot.close()
                if crashed or not mud.alive():
                    beat("trial", "xania crashed", band=label)
                    found.append(crash_finding(mud.crash_report(), repro, f"{plan.name}, {label} ({level})",
                                               transcript_tails(runs)))
                    break
                band_runs = [r for r, _ in bots]
                verdict = band_verdict(band_runs)
                beat("trial", f"done {label} ({level})", verdict=verdict)
                if verdict.startswith("unproductive"):
                    result.outcomes.append(Outcome("area", False, f"{label} ({level}): {verdict}", [], {}))
                    lo, hi = plan.advertised or plan.span
                    if fail_fast and lo <= level <= hi:  # outside its range, an empty level is expected
                        result.measurements["stopped_early"] = f"after {label}: {verdict}"
                        break
        finally:
            with contextlib.suppress(Exception):
                await d.close()
        found += bug_line_findings(mud.log("xania"), repro)
    found += _findings(plan, all_runs, repro)
    for run in all_runs:
        s = run.summary(len(plan.rooms))
        result.measurements[f"{run.cls} {run.label}"] = s
        ok = run.staged and not run.error
        result.outcomes.append(Outcome("area", ok, _cell(s) if ok else (run.error or "staging failed"),
                                       run.stage_notes + [f"killed by {k}" for k in run.killers], s))
    table = side_by_side(plan, bands, classes, all_runs)
    print(table)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "report.md").write_text(table + "\n", encoding="utf-8")
    result.artifacts["report"] = str(runs / "report.md")
    result.artifacts["transcripts"] = str(runs)
    return found


CRASH_CHECK = 5  # seconds between checks that xania is still up while bots play


async def _play_band(mud, sessions: list) -> bool:
    """Play a level's sessions together; if xania dies meanwhile, stop them at once. True if it crashed."""
    tasks = [asyncio.ensure_future(s) for s in sessions]
    crashed = False
    while not all(t.done() for t in tasks):
        await asyncio.wait(tasks, timeout=CRASH_CHECK)
        if not all(t.done() for t in tasks) and not await asyncio.to_thread(mud.alive):
            crashed = True
            for t in tasks:
                t.cancel()
            break
    await asyncio.gather(*tasks, return_exceptions=True)
    return crashed


def band_verdict(runs: list[AreaRun]) -> str:
    """One line on a level's sessions: "ok: 5 kills", or "unproductive: ..." with what got in the way, read from
    what the bots did (no kill at all, and why: escapes by outcome, navigation loops, errors, cut-offs)."""
    kills = sum(r.count("won") for r in runs)
    if kills:
        return f"ok: {kills} kills"
    why = []
    esc = Counter()
    for r in runs:
        esc += r.escapes + Counter({k: v for k, v in r.chosen.items() if k != "won"})
    if esc:
        why.append("escapes " + ", ".join(f"{k} {v}" for k, v in esc.most_common()))
    loops = sum(len(r.loops) for r in runs)
    if loops:
        why.append(f"{loops} navigation loops")
    errors = [r.error for r in runs if r.error]
    if errors:
        why.append(f"errors: {errors[0][:80]}")
    cut = sum(1 for r in runs if r.stopped.startswith("over its time cap"))
    if cut:
        why.append(f"{cut} cut off")
    stops = Counter(r.stopped for r in runs if r.stopped and not r.stopped.startswith("over"))
    if stops:
        why.append("stopped: " + ", ".join(f"{k} ({v})" for k, v in stops.items()))
    if not any(r.predicted and r.predicted.safe for r in runs):
        why.append("no mob predicted safe at this level")
    return "unproductive: no kills" + ("; " + "; ".join(why) if why else "")


def _findings(plan: AreaPlan, runs: list[AreaRun], repro: str) -> list[Finding]:
    out = []
    lo, hi = plan.advertised if plan.advertised else plan.span
    for run in runs:
        if run.count("died") and lo <= run.level <= hi:
            out.append(Finding(
                "area", f"area:{plan.filename}:death:{run.cls}", f"{plan.name}: a careful level-{run.level} {run.cls} "
                f"died in its own level range", "medium",
                f"Fighting only mobs predicted safe, a level {run.level} {run.cls} ({run.label}) died "
                f"{run.count('died')}x, killed by {', '.join(run.killers) or 'unknown'}. Either the area is harder "
                f"than its range says or Explorer's prediction is off for these mobs.", repro,
                {"lines": run.killers, "context": f"{run.cls} {run.level}"}))
    reached = set().union(*(r.visited for r in runs)) if runs else set()
    stuck = Counter()
    for r in runs:
        for room, why in r.skipped.items():
            if why in UNREACHED and room not in reached:
                stuck[room] += 1
    if stuck and len([r for r in runs if r.staged]) >= 2:
        rooms = sorted(stuck)
        out.append(Finding(
            "map", f"area:{plan.filename}:unreached", f"{plan.name}: {len(rooms)} rooms the map says are reachable "
            "that no bot reached", "low",
            "Explorer plans a route to these rooms, but walking it failed for every bot that tried (a one-way or "
            "locked exit, or a map error): " + ", ".join(map(str, rooms[:30])), repro,
            {"lines": [f"room {v}: {stuck[v]} bots" for v in rooms[:30]], "context": plan.name}))
    return out


def _cell(s: dict) -> str:
    hp = "-" if s["hp_per_kill"] is None else f"{s['hp_per_kill']:g}%"
    return (f"{s['rooms']} rooms, {s['kills']}k {s['deaths']}d {s['flees']}f, {s['xp_per_minute']:g} xp/min, "
            f"hp/kill {hp}")


def side_by_side(plan: AreaPlan, bands: dict[str, int], classes: list[str], runs: list[AreaRun]) -> str:
    """Markdown: one table per measure, levels down, classes across, observed (predicted)."""
    by = {(r.label, r.cls): r for r in runs}
    rng = "-".join(map(str, plan.advertised)) if plan.advertised else "all levels"
    out = [f"# {plan.name} ({plan.filename})", "",
           f"Levels {rng}, combat mobs' median level {plan.median}; {len(plan.rooms)} rooms, entry room "
           f"{plan.entry}, {len(plan.shops)} shops in the area.", ""]

    def table(title: str, cell) -> None:
        out.extend([f"## {title}", "", "| level | " + " | ".join(classes) + " |",
                    "|---|" + "---|" * len(classes)])
        for label, lv in bands.items():
            cells = []
            for c in classes:
                r = by.get((label, c))
                cells.append("not run" if r is None else ("error: " + r.error[:40]) if r.error and not r.fights
                             else cell(r))
            out.append(f"| {label} ({lv}) | " + " | ".join(cells) + " |")
        out.append("")

    n = len(plan.rooms)
    table("Rooms reached", lambda r: f"{len(r.visited)}/{n} (avoids {r.predicted.danger_rooms})")
    table("Kills / deaths / flees", lambda r: f"{r.count('won')} / {r.count('died')} / {r.count('fled')}"
          f" ({len(r.predicted.safe)} safe mobs)")
    table("HP lost per kill, seen (predicted)",
          lambda r: f"{_fmt(_pct(r.hp_per_kill))} ({_fmt(_pct(r.predicted.hp_per_kill))})")
    table("XP per minute", lambda r: f"{r.xp / (r.seconds / 60 or 1):.0f}")
    table("Gold: change, sold", lambda r: f"{_fmt(None if None in (r.gold_start, r.gold_end) else r.gold_end - r.gold_start)}"
          f", {len(r.sold)} for {r.sold_gold}")
    deaths = [f"- {r.cls} {r.label} ({r.level}): killed by {', '.join(r.killers)}" for r in runs if r.killers]
    if deaths:
        out += ["## Deaths", ""] + deaths + [""]
    return "\n".join(out)


def _fmt(x) -> str:
    return "-" if x is None else f"{x:g}%" if isinstance(x, float) else str(x)
