"""Shared lore: one SQLite database every bot reads and writes, so what one bot learns, all know.

- ``versions``: each scan of the game repo (commit, dirty).
- ``abilities``: each skill/spell's facts from the source and its current fingerprint; ``ability_changes`` keeps
  when each was first seen and every time its code changed.
- ``observations``: raw results of bots using abilities (never edited; summaries are computed from them, so a bad
  reading can't corrupt anything and an ability's history can be dropped).
- ``claims``: which bot is experimenting with which ability in which level band, with an expiry, so two bots
  don't duplicate work and a bot that disappears doesn't block others.

An ability's status is per level band (a level-5 cast says little about level 40), counting only observations
made on its current fingerprint: ``known`` once there are enough, ``testing`` while someone holds a claim,
``stale`` if there are observations but only on older code, else ``unknown``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from xania_sim.record.scan import AbilityFacts

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "state" / "lore.db"
BAND_WIDTH = 10
KNOWN_AFTER = 5  # observations in a band on the current code before an ability counts as known there
CLAIM_SECONDS = 15 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY, commit_hash TEXT NOT NULL, dirty INTEGER NOT NULL, scanned_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS abilities (
    name TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, facts TEXT NOT NULL, version_id INTEGER NOT NULL,
    present INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS ability_changes (
    name TEXT NOT NULL, fingerprint TEXT NOT NULL, version_id INTEGER NOT NULL, kind TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY, ability TEXT NOT NULL, fingerprint TEXT NOT NULL, bot TEXT NOT NULL,
    level INTEGER NOT NULL, target TEXT NOT NULL DEFAULT '', success INTEGER, damage REAL, mana REAL,
    detail TEXT NOT NULL DEFAULT '{}', at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS obs_ability ON observations (ability, fingerprint, level);
CREATE TABLE IF NOT EXISTS claims (
    ability TEXT NOT NULL, band INTEGER NOT NULL, bot TEXT NOT NULL, expires_at REAL NOT NULL,
    PRIMARY KEY (ability, band));
"""


def band_of(level: int) -> int:
    return max(0, (level - 1) // BAND_WIDTH)


def band_label(band: int) -> str:
    return f"{band * BAND_WIDTH + 1}-{(band + 1) * BAND_WIDTH}"


@dataclass
class ScanDiff:
    version_id: int
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Summary:
    ability: str
    band: int
    status: str
    observations: int
    success_rate: float | None
    mean_damage: float | None
    claimed_by: str | None = None


class Lore:
    def __init__(self, path: Path | str = DEFAULT_PATH, clock=time.time):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.clock = clock

    def close(self) -> None:
        self.db.close()

    # -- the source ------------------------------------------------------------------------------
    def apply_scan(self, commit: str, dirty: bool, facts: dict[str, AbilityFacts]) -> ScanDiff:
        now = self.clock()
        with self.db:
            vid = self.db.execute("INSERT INTO versions (commit_hash, dirty, scanned_at) VALUES (?, ?, ?)",
                                  (commit, int(dirty), now)).lastrowid
            known = dict(self.db.execute("SELECT name, fingerprint FROM abilities WHERE present = 1"))
            diff = ScanDiff(vid)
            for name, f in facts.items():
                old = known.get(name)
                if old == f.fingerprint:
                    continue
                kind = "new" if old is None and not self._ever(name) else "changed"
                (diff.new if kind == "new" else diff.changed).append(name)
                self.db.execute("INSERT INTO abilities (name, fingerprint, facts, version_id, present) "
                                "VALUES (?, ?, ?, ?, 1) ON CONFLICT(name) DO UPDATE SET fingerprint = excluded."
                                "fingerprint, facts = excluded.facts, version_id = excluded.version_id, present = 1",
                                (name, f.fingerprint, json.dumps(asdict(f)), vid))
                self.db.execute("INSERT INTO ability_changes VALUES (?, ?, ?, ?, ?)",
                                (name, f.fingerprint, vid, kind, now))
                self.db.execute("DELETE FROM claims WHERE ability = ?", (name,))
            for name in sorted(set(known) - set(facts)):
                diff.removed.append(name)
                self.db.execute("UPDATE abilities SET present = 0 WHERE name = ?", (name,))
                self.db.execute("INSERT INTO ability_changes VALUES (?, ?, ?, 'removed', ?)",
                                (name, known[name], vid, now))
        return diff

    def _ever(self, name: str) -> bool:
        return self.db.execute("SELECT 1 FROM abilities WHERE name = ?", (name,)).fetchone() is not None

    def facts(self, name: str) -> AbilityFacts | None:
        row = self.db.execute("SELECT facts FROM abilities WHERE name = ? AND present = 1", (name,)).fetchone()
        return AbilityFacts(**json.loads(row[0])) if row else None

    def abilities(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT name FROM abilities WHERE present = 1 ORDER BY name")]

    def fingerprint(self, name: str) -> str | None:
        row = self.db.execute("SELECT fingerprint FROM abilities WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    # -- what bots saw ------------------------------------------------------------------------------
    def observe(self, ability: str, bot: str, level: int, *, success: bool | None = None, damage: float | None = None,
                mana: float | None = None, target: str = "", **detail) -> None:
        fp = self.fingerprint(ability)
        if fp is None:
            raise KeyError(f"{ability!r} isn't in the scanned source")
        with self.db:
            self.db.execute("INSERT INTO observations (ability, fingerprint, bot, level, target, success, damage, "
                            "mana, detail, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (ability, fp, bot, level, target, None if success is None else int(success), damage, mana,
                             json.dumps(detail), self.clock()))

    def summary(self, ability: str, level: int) -> Summary:
        band = band_of(level)
        lo, hi = band * BAND_WIDTH + 1, (band + 1) * BAND_WIDTH
        fp = self.fingerprint(ability)
        n, ok, tries, dmg = self.db.execute(
            "SELECT COUNT(*), SUM(success), COUNT(success), AVG(damage) FROM observations "
            "WHERE ability = ? AND fingerprint = ? AND level BETWEEN ? AND ?", (ability, fp, lo, hi)).fetchone()
        older = self.db.execute("SELECT COUNT(*) FROM observations WHERE ability = ? AND fingerprint != ? "
                                "AND level BETWEEN ? AND ?", (ability, fp, lo, hi)).fetchone()[0]
        claim = self.db.execute("SELECT bot FROM claims WHERE ability = ? AND band = ? AND expires_at > ?",
                                (ability, band, self.clock())).fetchone()
        if n >= KNOWN_AFTER:
            status = "known"
        elif claim:
            status = "testing"
        elif older and not n:
            status = "stale"
        else:
            status = "unknown"
        return Summary(ability, band, status, n, ok / tries if tries else None, dmg, claim[0] if claim else None)

    # -- sharing the work ---------------------------------------------------------------------------
    def claim(self, ability: str, level: int, bot: str, seconds: float = CLAIM_SECONDS) -> bool:
        """Claim the right to experiment with ``ability`` in ``level``'s band. True if this bot now holds it (also
        when it already did: the claim is renewed)."""
        band, now = band_of(level), self.clock()
        with self.db:
            cur = self.db.execute(
                "INSERT INTO claims VALUES (?, ?, ?, ?) ON CONFLICT(ability, band) DO UPDATE SET bot = excluded.bot, "
                "expires_at = excluded.expires_at WHERE claims.expires_at <= ? OR claims.bot = excluded.bot",
                (ability, band, bot, now + seconds, now))
        return cur.rowcount == 1

    def release(self, ability: str, level: int, bot: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM claims WHERE ability = ? AND band = ? AND bot = ?",
                            (ability, band_of(level), bot))

    def to_learn(self, names, level: int) -> list[str]:
        """Of ``names``, those not yet known in this level's band and not being tested by someone else."""
        return [n for n in names if self.fingerprint(n) and self.summary(n, level).status in ("unknown", "stale")]
