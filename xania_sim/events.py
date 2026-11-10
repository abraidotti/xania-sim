"""Events: what happened to a bot, read from the game's own words, and kept so progress can be tracked and stories
told (and, later, so other tools can use it).

``parse(text)`` is pure: it turns MUD output into ``Event``s (kind + data), one per recognised line. The messages
come from Xania's source (file:line in ``RULES``), so a change there shows up as a failing test.

A ``Recorder`` watches a bot (``Bot.observers``): every reply and unprompted output is parsed, each event is stored
with the bot, the time, the room and area the navigator believes the bot is in, and what it was fighting; the
prompt is snapshotted when it changes (at most once every ``SNAPSHOT_EVERY`` seconds, and always on a level or a
death). Tables live in the shared lore database:

- ``bots``: one row per character (class, race, first and last seen);
- ``events``: (bot, at, kind, room, area, data JSON), never edited;
- ``snapshots``: (bot, at, level, hp, max_hp, mana, max_mana, moves, max_moves, xp, gold, room).

``Progress`` reads them back for the status table and the dispatch's progress pages.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from .parsers import Prompt

SNAPSHOT_EVERY = 60.0
CLOSE_CALL = 0.2  # HP share (alive) recorded as a close call, once until HP is back above REARM
REARM = 0.6


@dataclass(frozen=True)
class Event:
    kind: str
    data: dict = field(default_factory=dict)
    line: str = ""


def _int(s: str) -> int:
    return int(s.replace(",", ""))


# (kind, pattern, data from the match). Patterns are anchored to whole lines; ``who`` and ``text`` are verbatim.
RULES: list[tuple[str, re.Pattern, object]] = [
    # fight.cpp:739-740 (raw_kill)
    ("died", re.compile(r"^You have been KILLED!!"), lambda m: {}),
    ("kill", re.compile(r"^(?P<who>.+?) is DEAD!!$"), lambda m: {"victim": m["who"]}),
    # fight.cpp:1367 (group_gain)
    ("xp", re.compile(r"^You receive (?P<n>-?\d+) experience points\.$"), lambda m: {"amount": int(m["n"])}),
    # update.cpp:594 (advance_level via gain_exp); act_wiz.cpp:1461 (an immortal's advance)
    ("level", re.compile(r"^You raise a level!!"), lambda m: {}),
    # update.cpp:533/581
    ("gain", re.compile(r"^Your gain is: (?P<hp>\d+)/(?P<mhp>\d+) hp, (?P<m>\d+)/(?P<mm>\d+) m, (?P<mv>\d+)/(?P<mmv>\d+) "
                        r"mv (?P<p>\d+)/(?P<mp>\d+) prac\.$"),
     lambda m: {"hp": int(m["hp"]), "max_hp": int(m["mhp"]), "mana": int(m["m"]), "max_mana": int(m["mm"]),
                "moves": int(m["mv"]), "max_moves": int(m["mmv"]), "practices": int(m["p"])}),
    # Learning.cpp:22/25
    ("skill", re.compile(r"^You have become better at (?P<s>.+?)! \((?P<n>\d+)\)$"),
     lambda m: {"skill": m["s"], "percent": int(m["n"]), "from": "success"}),
    ("skill", re.compile(r"^You learn from your mistakes, and your (?P<s>.+?) skill improves\. \((?P<n>\d+)\)$"),
     lambda m: {"skill": m["s"], "percent": int(m["n"]), "from": "failure"}),
    # fight.cpp:2181 (do_flee)
    ("fled", re.compile(r"^You flee from combat!"), lambda m: {}),
    ("flee_failed", re.compile(r"^PANIC! You couldn't escape!"), lambda m: {}),
    # fight.cpp:1754, 1974 (do_bash, do_trip on the victim)
    ("knocked", re.compile(r"^(?P<who>.+?) (?:sends you sprawling with a powerful bash|trips you and you go down)!$"),
     lambda m: {"by": m["who"]}),
    # act_obj.cpp:578 (get from a container: loot), act_obj.cpp:1465 (sacrifice)
    ("gold", re.compile(r"^You get (?P<n>[\d,]+) gold coins? from (?:the )?corpse of (?P<who>.+?)\.$"),
     lambda m: {"amount": _int(m["n"]), "source": "corpse", "of": m["who"]}),
    ("gold", re.compile(r"^(?P<god>\w+) gives you (?P<n>[\d,]+) gold coins? for your sacrifice\.$"),
     lambda m: {"amount": _int(m["n"]), "source": "sacrifice", "god": m["god"]}),
    ("loot", re.compile(r"^You get (?P<item>.+?) from (?:the )?corpse of (?P<who>.+?)\.$"),
     lambda m: {"item": m["item"], "of": m["who"]}),
    # act_comm.cpp:136-137 (tell), 649 (gtell); do_say; channels.cpp:146+ (channel_command)
    ("tell", re.compile(r"^(?P<who>.+?) tells you '(?P<text>.*)'$"), lambda m: {"from": m["who"], "text": m["text"]}),
    ("say", re.compile(r"^(?P<who>.+?) says '(?P<text>.*)'$"), lambda m: {"from": m["who"], "text": m["text"]}),
    ("channel", re.compile(r"^(?P<who>.+?) (?P<ch>gossips|auctions|questions|answers|gratzes|alleges|philosophises|"
                           r"qwests|shouts|MUSIC:) '(?P<text>.*)'$"),
     lambda m: {"channel": m["ch"].rstrip(":").lower(), "from": m["who"], "text": m["text"]}),
    # act_comm.cpp:449-451 (follow), 632-634 (group)
    ("follow", re.compile(r"^(?P<who>.+?) now follows you\.$"), lambda m: {"who": m["who"], "dir": "in"}),
    ("follow", re.compile(r"^You now follow (?P<who>.+?)\.$"), lambda m: {"who": m["who"], "dir": "out"}),
    ("group", re.compile(r"^(?P<who>.+?) joins your group\.$"), lambda m: {"who": m["who"], "leader": "you"}),
    ("group", re.compile(r"^You join (?P<who>.+?)'s group\.$"), lambda m: {"who": "you", "leader": m["who"]}),
    # --- the bot's own skills and spells (M9 tactics). ``ability`` lines say whether a use worked; ``damage`` lines
    # (DamageMessages.cpp, from damage() via fight.cpp:699) say what an attack did. The recorder doesn't store these
    # two (QUIET): tactics.Tactics folds them into one ``tactic`` event per use, with the mana and lag it cost.
    # magic.cpp:637, 627 (do_cast)
    ("ability", re.compile(r"^You lost your concentration\.$"), lambda m: {"ok": False, "why": "concentration"}),
    ("ability", re.compile(r"^You don't have enough mana\.$"), lambda m: {"ok": False, "why": "mana"}),
    # fight.cpp:1755, 1769 (do_bash; 1942 is do_trip on oneself)
    ("ability", re.compile(r"^You slam into (?P<who>.+?), and send \w+ flying!$"),
     lambda m: {"name": "bash", "ok": True, "victim": m["who"]}),
    ("ability", re.compile(r"^You fall flat on your face!$"), lambda m: {"name": "bash", "ok": False, "fell": True}),
    # fight.cpp:1975 (do_trip)
    ("ability", re.compile(r"^You trip (?P<who>.+?) and .+? goes down!$"),
     lambda m: {"name": "trip", "ok": True, "victim": m["who"]}),
    # fight.cpp:1879 (do_dirt: told to the room, the kicker included)
    ("ability", re.compile(r"^(?P<who>.+?) is blinded by the dirt in \w+ eyes!$"),
     lambda m: {"name": "dirt kicking", "ok": True, "victim": m["who"]}),
    # fight.cpp:1583, 2466, 1576 (do_disarm / disarm)
    ("ability", re.compile(r"^You disarm (?P<who>.+?)!$"), lambda m: {"name": "disarm", "ok": True, "victim": m["who"]}),
    ("ability", re.compile(r"^You fail to disarm (?P<who>.+?)\.$"),
     lambda m: {"name": "disarm", "ok": False, "victim": m["who"]}),
    ("ability", re.compile(r"^\w+ weapon won't budge!$"), lambda m: {"name": "disarm", "ok": False}),
    # fight.cpp:1642, 1675 (do_berserk)
    ("ability", re.compile(r"^Your pulse races as you are consumed by rage!"), lambda m: {"name": "berserk", "ok": True}),
    ("ability", re.compile(r"^Your pulse speeds up, but nothing happens\.$"), lambda m: {"name": "berserk", "ok": False}),
    # fight.cpp:2234, 2229 (do_rescue)
    ("ability", re.compile(r"^You rescue (?P<who>.+?)!$"), lambda m: {"name": "rescue", "ok": True, "ally": m["who"]}),
    ("ability", re.compile(r"^You fail the rescue\.$"), lambda m: {"name": "rescue", "ok": False}),
    # fight.cpp:2318 (do_headbutt)
    ("ability", re.compile(r"^(?P<who>.+?) dodges your headbutt\. You feel disoriented\.$"),
     lambda m: {"name": "headbutt", "ok": False, "victim": m["who"]}),
    # DamageMessages.cpp:315 (immune), 323-325 (to_char: the amount is shown from level 20, make_damage_amount_label)
    ("damage", re.compile(r"^(?P<who>.+?) is unaffected by your (?P<attack>.+?)!$"),
     lambda m: {"attack": m["attack"], "victim": m["who"], "amount": 0, "immune": True}),
]

# DamageMessages.cpp:26-180 (dam_proportion_verbs_table), pluralised as ImpactVerbs::create does (DamageMessages.cpp:
# 255-273): what a hit "does" to its victim, from a miss up to ERADICATES
_IMPACT_VERBS = (
    "miss scrape touch brush taint scratch bruise hurt sicken entangle graze sting cool jolt gnaw nauseate stir "
    "dismiss hit scald chill zap ail smolder cut blister ice shock intoxicate torment pain injure burn frost stun "
    "contaminate palpitate wound torch freeze electrify boil corrupt torture split sizzle solidify electrocute combust "
    "defile agonize gash frazzle crackle deteriorate crush purge shred shatter crisp perforate befoul cleanse maul "
    "bake crystalize dissipate smash pacify decimate grill scorch splutter blacken purify eviscerate roast sear "
    "frizzle curse interrogate devastate cook splatter ruin redeem maim char enslave sanctify MUTILATE BRAND FREEZE "
    "ELECTRIFY DISSOLVE VITRIFY DISEMBOWEL MELT ELECTROCUTE PURGE DISMEMBER ENGULF CAUTERIZE DECOMPOSE CRUCIFY "
    "MASSACRE NUKE DISINTEGRATE CONDEMN MANGLE ATOMIZE DEMOLISH DEVASTATE OBLITERATE ANNIHILATE ERADICATE").split()


def _plural(verb: str) -> str:
    last = verb[-1]
    if last in "yY":
        return verb[:-1] + ("IES" if last.isupper() else "ies")
    if last in "hsxHSX":
        return verb + ("ES" if last.isupper() else "es")
    return verb + ("S" if last.isupper() else "s")


_IMPACTS = sorted({_plural(v) for v in _IMPACT_VERBS}, key=len, reverse=True) + ["does UNSPEAKABLE things to"]
# "Your kick hits the goblin's leg. (12)"; big hits are framed "***  MUTILATES  ***" / "<<<  ...  >>>"
# (make_prefix_suffix, DamageMessages.cpp:198-211; with colour off only the frame is left)
RULES.append(("damage", re.compile(r"^Your (?P<attack>[\w' ]+?) (?:\*\*\*  |<<<  )?(?P<impact>" + "|".join(_IMPACTS)
                                   + r")(?:  \*\*\*|  >>>)? (?P<who>.+?)'s (?P<part>[^.!]+)[.!](?: \((?P<n>\d+)\))?$"),
              lambda m: {"attack": m["attack"], "victim": m["who"], "amount": int(m["n"] or 0),
                         "missed": m["impact"] == "misses" and not m["n"]}))
# (a small hit can read "misses" yet carry an amount: the proportion is damage * 100 / max HP in integers plus a
# random 0..level/5, and 0 is the "miss" row: DamageMessages.cpp:183-195)
QUIET = {"ability", "damage"}  # parsed for tactics, folded into "tactic" events rather than stored line by line


# who's hitting the bot: "The small spider's slash splits your head." / "...'s trip misses your left shin." (the bot's
# own hits start with "Your"; dam_message in fight.cpp); also "<short> is here, fighting YOU!" in a room view
_ATTACK = re.compile(r"^(?!You)(?P<who>[^'\n]+?)'s [\w ]+? (?:you|your)\b.*$|^(?P<who2>.+?) is here, fighting YOU!$", re.M)


def attacker(text: str) -> str | None:
    """The last thing seen attacking the bot in ``text`` (lower-cased article), or None."""
    who = None
    for m in _ATTACK.finditer(text):
        who = m["who"] or m["who2"]
    if who is None:
        return None
    return re.sub(r"^(The|A|An) ", lambda m: m.group(1).lower() + " ", who.strip())


def parse(text: str) -> list[Event]:
    out = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith("<bot "):
            continue
        for kind, pattern, data in RULES:
            m = pattern.match(line)
            if m:
                out.append(Event(kind, data(m), line))
                break
    return out


SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    name TEXT PRIMARY KEY, cls TEXT NOT NULL DEFAULT '', race TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
    last_seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, bot TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL, room INTEGER, area TEXT,
    run_id TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS events_bot ON events (bot, at);
CREATE INDEX IF NOT EXISTS events_kind ON events (kind, at);
CREATE TABLE IF NOT EXISTS snapshots (
    bot TEXT NOT NULL, at REAL NOT NULL, level INTEGER NOT NULL, hp INTEGER, max_hp INTEGER, mana INTEGER,
    max_mana INTEGER, moves INTEGER, max_moves INTEGER, xp INTEGER, gold INTEGER, room INTEGER);
CREATE INDEX IF NOT EXISTS snapshots_bot ON snapshots (bot, at);
"""


class Recorder:
    """Records what one bot sees. ``room()`` gives the navigator's belief; ``area_of(vnum)`` the area's name;
    ``target`` is set by whoever starts a fight (a deaths' killer is the last target)."""

    def __init__(self, db, bot, cls: str = "", race: str = "", level: int = 0, run_id: str = "", room=None,
                 area_of=None, clock=time.time):
        self.db, self.bot, self.run_id, self.clock = db, bot, run_id, clock
        self.room = room or (lambda: None)
        self.area_of = area_of or (lambda vnum: None)
        self.level = level
        self.target: str | None = None  # what the bot chose to fight (set by the Leveller)
        self.attacker: str | None = None  # what was last seen hitting it
        self._last_snap, self._last_prompt = 0.0, None
        self._close_call_armed = True
        db.executescript(SCHEMA)
        now = clock()
        with db:
            db.execute("INSERT INTO bots (name, cls, race, created, last_seen) VALUES (?, ?, ?, ?, ?) ON CONFLICT(name) "
                       "DO UPDATE SET last_seen = excluded.last_seen, cls = CASE WHEN excluded.cls != '' THEN "
                       "excluded.cls ELSE bots.cls END, race = CASE WHEN excluded.race != '' THEN excluded.race ELSE "
                       "bots.race END", (bot.name, cls, race, now, now))
        bot.observers.append(self.feed)

    def detach(self) -> None:
        if self.feed in self.bot.observers:
            self.bot.observers.remove(self.feed)

    def feed(self, text: str, prompt: Prompt | None) -> list[Event]:
        events = parse(text)
        hit_by = attacker(text)
        if hit_by:
            self.attacker = hit_by
        now, room = self.clock(), self.room()
        area = self.area_of(room) if room is not None else None
        rows = []
        for e in events:
            if e.kind in QUIET:
                continue
            data = dict(e.data)
            if e.kind == "level":
                self.level += 1
                data["level"] = self.level
            elif e.kind == "died" and (self.attacker or self.target):
                data["killer"] = self.attacker or self.target  # who was hitting it, not what it last chose to fight
                self.attacker = None
            elif e.kind == "kill" and self.target:
                data["target"] = self.target
            rows.append((self.bot.name, now, e.kind, room, area, self.run_id, json.dumps(data)))
        if prompt is not None and prompt.max_hp and not any(e.kind == "died" for e in events):
            if 0 < prompt.hp < CLOSE_CALL * prompt.max_hp and self._close_call_armed:
                self._close_call_armed = False
                foe = self.attacker or self.target
                data = {"hp": prompt.hp, "max_hp": prompt.max_hp} | ({"against": foe} if foe else {})
                rows.append((self.bot.name, now, "close_call", room, area, self.run_id, json.dumps(data)))
            elif prompt.hp >= REARM * prompt.max_hp:
                self._close_call_armed = True
        force = any(e.kind in ("level", "died") for e in events)
        with self.db:
            if rows:
                self.db.executemany("INSERT INTO events (bot, at, kind, room, area, run_id, data) VALUES (?, ?, ?, ?, ?, "
                                    "?, ?)", rows)
                self.db.execute("UPDATE bots SET last_seen = ? WHERE name = ?", (now, self.bot.name))
            if prompt is not None and (force or (prompt != self._last_prompt and now - self._last_snap >= SNAPSHOT_EVERY)):
                self._snapshot(prompt, now, room)
        return events

    def record(self, kind: str, data: dict) -> None:
        """Store an event the bot's behaviour derived itself (e.g. a ``tactic``: one skill or spell use, with what it
        cost and did), with the same bot, time, room and area as parsed ones."""
        now, room = self.clock(), self.room()
        area = self.area_of(room) if room is not None else None
        with self.db:
            self.db.execute("INSERT INTO events (bot, at, kind, room, area, run_id, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (self.bot.name, now, kind, room, area, self.run_id, json.dumps(data)))

    def snapshot(self) -> None:
        """Snapshot now (e.g. on login and logout)."""
        if self.bot.prompt is not None:
            with self.db:
                self._snapshot(self.bot.prompt, self.clock(), self.room())

    def _snapshot(self, p: Prompt, now: float, room: int | None) -> None:
        self.db.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (self.bot.name, now, self.level, p.hp, p.max_hp, p.mana, p.max_mana, p.moves, p.max_moves,
                         p.xp, p.gold, room))
        self._last_snap, self._last_prompt = now, p


@dataclass
class BotStatus:
    name: str
    cls: str
    race: str
    created: float
    last_seen: float
    level: int
    xp: int
    gold: int
    kills: int
    deaths: int
    flees: int
    levels_gained: int
    hours: float  # time with a snapshot or event, roughly time online


class Progress:
    """Read-only views of the event log."""

    def __init__(self, db):
        self.db = db
        db.executescript(SCHEMA)

    def bots(self) -> list[BotStatus]:
        out = []
        for name, cls, race, created, last_seen in self.db.execute(
                "SELECT name, cls, race, created, last_seen FROM bots ORDER BY last_seen DESC"):
            snap = self.db.execute("SELECT level, xp, gold FROM snapshots WHERE bot = ? ORDER BY at DESC LIMIT 1",
                                   (name,)).fetchone() or (0, 0, 0)
            counts = dict(self.db.execute("SELECT kind, COUNT(*) FROM events WHERE bot = ? GROUP BY kind", (name,)))
            out.append(BotStatus(name, cls, race, created, last_seen, snap[0], snap[1], snap[2], counts.get("kill", 0),
                                 counts.get("died", 0), counts.get("fled", 0), counts.get("level", 0),
                                 self.online_hours(name)))
        return out

    def online_hours(self, bot: str, gap: float = 600) -> float:
        """Time covered by the bot's records, counting gaps under ``gap`` seconds as online."""
        times = [r[0] for r in self.db.execute(
            "SELECT at FROM snapshots WHERE bot = ? UNION ALL SELECT at FROM events WHERE bot = ? ORDER BY 1",
            (bot, bot))]
        return sum(b - a for a, b in zip(times, times[1:]) if b - a < gap) / 3600

    def levels(self, bot: str) -> list[tuple[float, int]]:
        """(time, level) at each snapshot where the level changed, for the level-over-time chart."""
        out: list[tuple[float, int]] = []
        for at, level in self.db.execute("SELECT at, level FROM snapshots WHERE bot = ? ORDER BY at", (bot,)):
            if not out or out[-1][1] != level:
                out.append((at, level))
        return out

    def events(self, bot: str | None = None, kinds: tuple[str, ...] = (), limit: int = 200) -> list[dict]:
        q, args = "SELECT bot, at, kind, room, area, data FROM events WHERE 1", []
        if bot:
            q, args = q + " AND bot = ?", args + [bot]
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        rows = self.db.execute(q + " ORDER BY at DESC, id DESC LIMIT ?", args + [limit]).fetchall()
        return [{"bot": b, "at": at, "kind": k, "room": r, "area": a, "data": json.loads(d)} for b, at, k, r, a, d in rows]

    def timeline(self, bot: str | None = None, since: float = 0, until: float = float("inf")) -> list[dict]:
        """Every event in a period, oldest first (for the chronicle)."""
        q = "SELECT bot, at, kind, room, area, data FROM events WHERE at >= ? AND at < ?"
        args: list = [since, until if until != float("inf") else 1e18]
        if bot:
            q, args = q + " AND bot = ?", args + [bot]
        rows = self.db.execute(q + " ORDER BY at, id", args).fetchall()
        return [{"bot": b, "at": at, "kind": k, "room": r, "area": a, "data": json.loads(d)} for b, at, k, r, a, d in rows]

    def by_area(self, bot: str | None = None) -> list[tuple[str, int, int, int]]:
        """(area, kills, deaths, xp) per area, most kills first."""
        q = ("SELECT COALESCE(area, '?'), SUM(kind = 'kill'), SUM(kind = 'died'), "
             "SUM(CASE WHEN kind = 'xp' THEN json_extract(data, '$.amount') ELSE 0 END) FROM events "
             "WHERE kind IN ('kill', 'died', 'xp')" + (" AND bot = ?" if bot else "") + " GROUP BY 1 ORDER BY 2 DESC")
        return [tuple(r) for r in self.db.execute(q, (bot,) if bot else ())]
