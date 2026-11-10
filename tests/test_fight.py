"""Recognising mobs in a room view, and the shared fight log that overrides predictions."""

from types import SimpleNamespace as NS

import pytest

from xania_sim.activities.fight import BAD_RATE, MIN_FIGHTS, FightLog, FightResult, MobIndex
from xania_sim.record.store import Lore


def _mob(vnum, keywords, short, long):
    return NS(vnum=vnum, keywords=keywords, short_descr=short, long_descr=long)


@pytest.fixture
def world():
    town, forest = object(), object()
    mobs = {1: _mob(1, "duckling duck", "the duckling", "A |yfluffy|w duckling waddles about.\n"),
            2: _mob(2, "soldier toy", "a toy soldier", "A toy soldier marches back and forth.\nIt looks wooden.\n"),
            3: _mob(3, "bear ugly", "the ugly bear", "An ugly bear growls at you.\n"),
            4: _mob(4, "bear ugly", "the ugly bear", "An ugly bear growls at you.\n")}
    return NS(mobs=mobs, room_area={10: town, 20: forest}, mob_area={1: town, 2: town, 3: town, 4: forest})


def test_mobs_are_recognised_after_the_exits_line(world):
    idx = MobIndex(world)
    view = ("Toy Shop\n\n  Shelves of toys.\n\n[Exits: north]\nA wooden sword lies here.\n"
            "A fluffy duckling waddles about.\n(White Aura) A toy soldier marches back and forth.\nIt looks wooden.\n"
            "The ugly bear is fighting YOU!\n")
    assert idx.in_view(view, here=10) == [1, 2, 3]  # the bear: the one from this room's area
    assert idx.keyword(2) == "soldier"
    assert idx.in_view("Nowhere\n\n[Exits: none]\n") == []


def fought(outcome, before=20, after=10, xp=100):
    return FightResult(7, outcome, 3, before, after, 20, xp, False, "")


def test_fight_log_summarises_per_level_band(tmp_path):
    lore = Lore(tmp_path / "lore.db")
    log = FightLog(lore.db)
    assert log.seen(7, 3) == (0, 0.0, 0.0)
    for o in ("won", "won", "fled", "died", "absent"):
        log.record("Brena", "knight", 3, fought(o))
    n, bad, lost = log.seen(7, 5)
    assert n == 4 and bad == 0.5 and lost == pytest.approx(0.5)  # "absent" isn't a fight
    assert log.seen(7, 15) == (0, 0.0, 0.0)  # another band
    assert n >= MIN_FIGHTS and bad >= BAD_RATE  # this mob would now be off the list at levels 1-10
    lore.close()


def test_one_death_is_counted_per_band(tmp_path):
    lore = Lore(tmp_path / "lore.db")
    log = FightLog(lore.db)
    log.record("Wren", "mage", 2, FightResult(7, "died", 9, 20, -9, 20, 0, False, ""))
    assert log.deaths(7, 1) == 1 and log.deaths(7, 15) == 0 and log.deaths(8, 2) == 0
    lore.close()
