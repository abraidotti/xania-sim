"""Putting a bot on stage: a character at any level with a chosen gear kit and skill loadout, on a test MUD.

``stage`` uses the director (director.py) for everything a mortal can't do: ``advance`` to the level (act_wiz.cpp
do_advance), ``set skill`` for the loadout (do_sset: silent, sets the learned % only), ``set char`` for practice,
train or gold (do_mset), ``load obj`` + ``give`` for each kit piece, ``restore``. The bot itself wears each piece
(``wear``/``wield``/``hold`` are all do_wear, InterpreterImpl.cpp:122, :228; act_obj.cpp wear_obj), and the
report says, with the game's own words, what didn't go on. ``equipment`` (act_info.cpp do_equipment: "<worn on
body>  a vest") and ``practice`` (PracticeTabulator.cpp) are read back to check the result.

Gear goes on one piece at a time: a given object goes to the front of the inventory (handler.cpp:331 obj_to_char)
and ``wear <keyword>`` takes the first match (Char.cpp:318 find_filtered_obj), so it's the piece just given even
when the bot carries others with the same keyword. ``find_filtered_obj`` only finds what the bot can see, so for a
kit with invisible pieces the bot first quaffs a potion of see invisible.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from xania_sim.setup import kits as K
from xania_sim.setup import loadouts as L
from xania_sim.core.director import Director, DirectorError
from xania_sim.core.session import Bot, Profile
from xania_sim.activities.gear import parse_equipment
from xania_sim.activities.practice import SESSIONS, parse_table

STAGE_ROOM = 3001  # VnumRooms.hpp MidgaardTemple: lit, peaceful
SEE_INVISIBLE = K.SEE_INVISIBLE  # a potion of detect invis, level 0
WEAR_REPLY = (r"You wear|You wield|You hold|You light|You use|You must be level|zapped|too heavy|two hands free|"
              r"hands are tied|You already wear|You can't wear|You do not have|You can't remove")
WEAR_FAILED = re.compile(r"(You must be level \d+[^\n]*|You are zapped[^\n]*|It is too heavy[^\n]*|"
                         r"You need two hands free[^\n]*|Your hands are tied[^\n]*|You already wear[^\n]*|"
                         r"You can't wear, wield, or hold that[^\n]*|You do not have that item[^\n]*|"
                         r"You can't remove[^\n]*)")
LEARNING_DRIFT = 5  # % a skill may gain by use between being set and being checked


@dataclass
class WearResult:
    piece: K.Piece
    ok: bool
    reply: str  # the game's words (the failure line, or what it said on success)


@dataclass
class StageReport:
    name: str
    cls: str
    race: str
    level: int
    kit: K.Kit
    loadout: L.Loadout
    level_seen: int | None = None
    worn: list[WearResult] = field(default_factory=list)
    equipment: list[tuple[str, str]] = field(default_factory=list)  # (location, item) from ``equipment``
    missing: list[K.Piece] = field(default_factory=list)  # kit pieces (school gear included) not in ``equipment``
    practice: dict[str, int] = field(default_factory=dict)  # what ``practice`` lists
    practice_sessions: int = 0
    skill_mismatches: dict[str, tuple[int | None, int | None]] = field(default_factory=dict)  # skill: (want, seen)
    notes: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[WearResult]:
        return [w for w in self.worn if not w.ok]

    @property
    def ok(self) -> bool:
        return self.level_seen == self.level and not self.failed and not self.missing and not self.skill_mismatches

    def text(self) -> str:
        t = self.kit.totals()
        out = [f"{self.name}: level {self.level_seen} {self.race} {self.cls}, kit {self.kit.name} "
               f"({len(self.worn) - len(self.failed)}/{len(self.worn)} pieces on), loadout {self.loadout.name} "
               f"({len(self.practice)} skills listed, {len(self.skill_mismatches)} mismatches)",
               f"  on paper: AC {t.ac:.1f}, hitroll {t.hitroll}, damroll {t.damroll}, weapon avg {t.weapon_average:g}"]
        out += [f"  FAILED {w.piece.slot} {w.piece.vnum} {w.piece.name}: {w.reply}" for w in self.failed]
        out += [f"  MISSING from equipment: {p.slot} {p.vnum} {p.name}" for p in self.missing]
        out += [f"  SKILL {s}: wanted {want}, practice shows {seen}" for s, (want, seen) in
                sorted(self.skill_mismatches.items())]
        unusable = self.loadout.unusable
        if unusable:
            out.append(f"  set but unusable at level {self.level} (not listed by practice): "
                       + ", ".join(f"{e.skill} ({e.why})" for e in unusable))
        out += [f"  note: {n}" for n in self.notes]
        out.append("  equipment: " + "; ".join(f"<{loc}> {item}" for loc, item in self.equipment))
        return "\n".join(out)


def resolve_kit(world, kit: K.Kit | str, level: int, cls: str, align: str, race: str) -> K.Kit:
    return kit if isinstance(kit, K.Kit) else K.build_kit(world, kit, level, cls, align, race)


def resolve_loadout(world, loadout: L.Loadout | str, level: int, cls: str, race: str) -> L.Loadout:
    if isinstance(loadout, L.Loadout):
        return loadout
    if loadout.endswith(".toml"):
        return L.load_custom(loadout, cls, level, race, world.src_dir)
    return L.build_loadout(loadout, cls, level, race, src_dir=world.src_dir)


async def stage(mud, director: Director, bot: Bot, level: int, kit: K.Kit | str, loadout: L.Loadout | str,
                align: str | None = None, *, cls: str | None = None, race: str | None = None, world=None,
                room: int | None = STAGE_ROOM, practice: int | None = None, train: int | None = None,
                gold: int | None = None) -> StageReport:
    """Create (if ``bot`` isn't connected yet) and set up ``bot``. ``kit`` and ``loadout`` are objects or names
    (a loadout name ending in .toml is a custom file); with names, give ``cls`` (and ``race``, default human).
    ``align`` is g/n/e (default the kit's). Returns what was applied and what failed; raises only if the MUD
    refuses the basics (login, advance)."""
    if world is None:
        from xania_sim.core import world as W

        world = W.load()
    cls = cls or (kit.cls if isinstance(kit, K.Kit) else loadout.cls if isinstance(loadout, L.Loadout) else None)
    if cls is None:
        raise ValueError("stage needs cls= when kit and loadout are given by name")
    race = race or (kit.race if isinstance(kit, K.Kit) else "human")
    align = (align or (kit.align if isinstance(kit, K.Kit) else "n"))[0].lower()
    kit = resolve_kit(world, kit, level, cls, align, race)
    loadout = resolve_loadout(world, loadout, level, cls, race)
    rep = StageReport(bot.name, cls, race, level, kit, loadout)
    if bot.conn is None or bot.conn.closed:
        await bot.enter(Profile(race=race, cls=cls, alignment=align))
    name = bot.name
    if room is not None:
        await director.goto(room)
    await director.bring(name)
    r = await director.advance(name, level)
    if not re.search(r"Raising a player|Lowering a player|already", r):
        raise DirectorError(f"advance {name} {level}: {r.strip()}")
    await _apply_loadout(director, name, loadout, rep)
    for field_, value in (("practice", practice), ("train", train), ("gold", gold)):
        if value is not None:
            await director.set_char(name, field_, value)
    await _apply_kit(director, bot, kit, rep)
    await director.restore(name)
    await _check(bot, kit, loadout, rep)
    if hasattr(mud, "alive") and not mud.alive():
        rep.notes.append("xania is no longer running")
    return rep


async def _apply_loadout(director: Director, name: str, loadout: L.Loadout, rep: StageReport) -> None:
    await director.set_skill(name, "all", loadout.reset)
    for skill, value in loadout.settings.items():
        if value == loadout.reset:
            continue
        r = await director.set_skill(name, skill, value)
        if "No such skill" in r:
            rep.notes.append(f"set skill {skill}: {r.strip()}")


async def _apply_kit(director: Director, bot: Bot, kit: K.Kit, rep: StageReport) -> None:
    name = bot.name
    if any(p.invisible for p in kit.pieces):
        await director.give_obj(name, SEE_INVISIBLE, "potion")
        r = await bot.do("quaff potion", expect=r"You quaff[^\n]*|You do not have[^\n]*|too powerful[^\n]*")
        m = re.search(r"You quaff[^\n]*|You do not have[^\n]*|[^\n]*too powerful[^\n]*", r.text)
        rep.notes.append(f"invisible pieces: the bot quaffed a potion of see invisible ({m.group(0) if m else '?'})")
    for piece in kit.remove:
        r = await bot.do(f"remove {piece.keyword}", expect=r"You stop using|You do not have|can't")
        m = re.search(r"You stop using[^\n]*|You do not have[^\n]*|[^\n]*can't[^\n]*", r.text)
        rep.notes.append(f"took off {piece.name} first ({m.group(0) if m else r.text.strip()[:80]})")
        await _drop_replaced(bot, kit, r.text)
    for piece in kit.pieces:
        try:
            given = await director.give_obj(name, piece.vnum, piece.keyword)
        except DirectorError as e:
            rep.worn.append(WearResult(piece, False, f"director: {e}"))
            continue
        if "You give" not in given:
            rep.worn.append(WearResult(piece, False, f"give: {_first_line(given.split('Ok.', 1)[-1])}"))
            continue
        r = await bot.do(f"{piece.command} {piece.keyword}", expect=WEAR_REPLY)
        failed = WEAR_FAILED.search(r.text)
        if failed or not r.complete:
            rep.worn.append(WearResult(piece, False, failed.group(1).strip() if failed else
                                       f"no reply to '{piece.command} {piece.keyword}': {r.text.strip()[:120]}"))
        else:
            m = re.search(r"You (?:wear|wield|hold|light|use)[^\n]*", r.text)
            rep.worn.append(WearResult(piece, True, m.group(0) if m else _first_line(r.text)))
            await _drop_replaced(bot, kit, r.text)


async def _drop_replaced(bot: Bot, kit: K.Kit, reply: str) -> None:
    """Drop the school gear a piece replaced (remove_obj: "You stop using $p."), so it doesn't weigh the bot down."""
    for m in re.finditer(r"You stop using (.+?)\.\s*$", reply, re.M):
        old = next((p for p in kit.outfit if m.group(1).lower().endswith(p.name.lower())), None)
        if old is not None:
            await bot.do(f"drop {old.keyword}", expect=r"You drop|You do not have|can't let go")


async def _check(bot: Bot, kit: K.Kit, loadout: L.Loadout, rep: StageReport) -> None:
    r = await bot.do("score", expect=r"Level: \S*\d+")
    m = re.search(r"Level: \S*?(\d+)", r.text)
    rep.level_seen = int(m.group(1)) if m else None
    r = await bot.do("equipment", expect=r"You are using:")
    rep.equipment = parse_equipment(r.text)
    rep.missing = missing_pieces(kit.worn, rep.equipment)
    r = await bot.do("practice", expect=SESSIONS)
    rep.practice, rep.practice_sessions = parse_table(r.text)
    rep.skill_mismatches = skill_mismatches(loadout.expected_practice(), rep.practice)


def missing_pieces(pieces: list[K.Piece], equipment: list[tuple[str, str]]) -> list[K.Piece]:
    """The pieces whose short description isn't among the worn items (each worn line counts once; the game may
    prefix "(Glowing) " and the like)."""
    worn = Counter(item.lower() for _, item in equipment)
    out = []
    for p in pieces:
        hit = next((w for w in worn if worn[w] > 0 and w.endswith(p.name.lower())), None)
        if hit is None:
            out.append(p)
        else:
            worn[hit] -= 1
    return out


def skill_mismatches(want: dict[str, int], seen: dict[str, int], drift: int = LEARNING_DRIFT
                     ) -> dict[str, tuple[int | None, int | None]]:
    """Skills where ``practice`` differs from the loadout: missing, a different %, or listed but not wanted. A skill
    up to ``drift`` above what was set isn't a mismatch: use improves skills in the meantime (fast healing and
    meditation on every regen tick, update.cpp)."""
    out = {s: (v, seen.get(s)) for s, v in want.items()
           if seen.get(s) is None or not v <= seen[s] <= v + drift}
    out |= {s: (None, v) for s, v in seen.items() if s not in want}
    return out


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


# -- CLI ------------------------------------------------------------------------------------------------------------
def add_command(sub) -> None:
    s = sub.add_parser("stage", help="stage a bot on a test MUD at a level with a gear kit and skill loadout")
    s.add_argument("name")
    s.add_argument("--level", type=int, default=40)
    s.add_argument("--class", dest="cls", default="knight")
    s.add_argument("--race", default="human")
    s.add_argument("--align", default="n", choices=["g", "n", "e"])
    s.add_argument("--kit", default="best", choices=K.KITS)
    s.add_argument("--loadout", default="class", help=f"{', '.join(L.LOADOUTS[:-1])} or a custom .toml file")
    s.add_argument("--port", type=int, default=9140)
    s.add_argument("--existing", action="store_true", help="use a test MUD already running on --port")
    s.add_argument("--password", default="stagepass1")


async def stage_cmd(args) -> StageReport:
    from xania_sim.core import world as W
    from xania_sim.core.server import TestMud

    world = W.load()
    mud = TestMud(port=args.port)
    if not args.existing:
        await mud.start()
    try:
        kit = K.build_kit(world, args.kit, args.level, args.cls, args.align, args.race)
        print(kit.describe())
        loadout = resolve_loadout(world, args.loadout, args.level, args.cls, args.race)
        print(f"{loadout.name} loadout: {len(loadout.entries)} skills, {len(loadout.unusable)} unusable at "
              f"level {args.level}")
        director = await Director.create(mud)
        try:
            bot = Bot(args.name, args.password, host=mud.host, port=mud.port, transcript_dir=Path("runs") / "stage")
            rep = await stage(mud, director, bot, args.level, kit, loadout, args.align, world=world)
            print(rep.text())
            await bot.quit()
        finally:
            await director.close()
        return rep
    finally:
        if not args.existing:
            await mud.stop()
