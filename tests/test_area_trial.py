"""The area trial's planning and report (no MUD needed)."""

import pytest

from xania_sim.activities.fight import FightResult
from xania_sim.core.server import area_list_script
from xania_sim.trials import area as A


def test_levels_around_the_range():
    assert A.levels_for((5, 35), 5, (2, 8)) == {"low-5": 1, "low": 5, "high": 35, "high+5": 40}  # median 5 = low
    assert A.levels_for((10, 15), 12, (3, 11)) == {"low-5": 5, "low": 10, "median": 12, "high": 15, "high+5": 20}
    # clamped to 1: low wins over low-5; nothing above hero (91)
    assert A.levels_for(None, 15, (1, 30)) == {"low": 1, "median": 15, "high": 30, "high+5": 35}
    assert A.levels_for((88, 91), 90, (88, 91)) == {"low-5": 83, "low": 88, "median": 90, "high": 91}


def test_area_list_script():
    assert area_list_script(None) == ""
    s = area_list_script(["Help.are", "Midgaard.are"])
    assert "printf 'Help.are\\nMidgaard.are\\n$\\n'" in s and 'AREA="$D/area/"' in s
    with pytest.raises(ValueError):
        area_list_script(["x; rm -rf /"])


class _File:
    def __init__(self, name, rooms):
        self.filename, self.rooms = name, rooms


class _World:
    load_order = ["Help.are", "Limbo.are", "Shire.are", "Midgaard.are", "Moria.are", "Socials.are", "MudSchool.are"]
    files = [_File(n, [] if n in ("Help.are", "Socials.are") else [1]) for n in load_order]


def test_cut_down_list_keeps_load_order():
    assert A.cut_down_list(_World(), "Moria.are") == ["Help.are", "Limbo.are", "Midgaard.are", "Moria.are",
                                                      "Socials.are", "MudSchool.are"]


def _plan():
    return A.AreaPlan("Troll Den", "TrollsDen.are", (10, 15), 12, (3, 11), {"low": 10, "high": 15},
                      [2801, 2802, 2803], 2801, {2800}, [])


def _run(cls, label, level, *outcomes):
    r = A.AreaRun(cls, label, level, staged=True, predicted=A.Prediction([2800], 0.1, 0.2, 0))
    r.fights = [FightResult(2800, o, 3, 100, 70, 100, 10, False, "") for o in outcomes]
    r.visited, r.seconds = {2801, 2802}, 120
    return r


def test_findings_death_in_range_and_unreached_rooms():
    runs = [_run("knight", "low", 10, "won", "died"), _run("mage", "high", 15, "won")]
    runs[0].killers = ["the troll"]
    for r in runs:
        r.skipped = {2803: "no route"}
    found = {f.signature: f for f in A._findings(_plan(), runs, "repro")}
    assert set(found) == {"area:TrollsDen.are:death:knight", "area:TrollsDen.are:unreached"}
    assert "the troll" in found["area:TrollsDen.are:death:knight"].summary
    runs[1].visited.add(2803)  # one bot got there: not a map problem
    assert "area:TrollsDen.are:unreached" not in {f.signature for f in A._findings(_plan(), runs, "repro")}


def test_side_by_side():
    runs = [_run("knight", "low", 10, "won", "won", "fled")]
    md = A.side_by_side(_plan(), {"low": 10, "high": 15}, ["knight", "mage"], runs)
    assert "| low (10) | 2 / 0 / 1 (1 safe mobs) | not run |" in md
    assert "| low (10) | 30% (20%) | not run |" in md  # seen 30 HP of 100 per kill, predicted 20%
    s = runs[0].summary(3)
    assert (s["kills"], s["flees"], s["xp_per_minute"], s["rooms"]) == (2, 1, 15.0, "2/3")


def test_restricted_rooms_hide_their_exits():
    """Mud School's inner rooms are NewbiesOnly (flag R): above level 5 the game leaves exits into them out of
    [Exits: ...] and won't let a character in (Char::can_see(const Room &)); a bot must neither get lost at the
    entrance nor plan a route through them (it once looped recall -> up -> look until it ran out of moves)."""
    from types import SimpleNamespace as NS

    from xania_sim.activities.move import Navigator
    from xania_sim.record.rooms import Fingerprints, RoomView, visible_to

    R = 1 << 17  # NewbiesOnly
    ex = lambda to: NS(cosmetic=False, to_vnum=to, is_door=False, key=0)  # noqa: E731
    rooms = {1: NS(name="Temple", description="A temple.", room_flags=0, sector=0, exits={4: ex(2)}),
             2: NS(name="Entrance", description="An entrance.", room_flags=0, sector=0, exits={0: ex(3), 5: ex(1)}),
             3: NS(name="School", description="A school.", room_flags=R, sector=0, exits={2: ex(2)})}
    world = NS(rooms=rooms)
    fp = Fingerprints(world)
    assert fp.matches(2, RoomView(("Entrance", "An entrance."), frozenset({"down"})))  # north hidden: still known
    assert visible_to(rooms[3], 5) and not visible_to(rooms[3], 6) and visible_to(rooms[3], None)
    nav = Navigator(None, world, fp, level=20)
    nav.here = 1
    assert nav.plan(3) is None
    nav.level = 3
    assert [s.to_vnum for s in nav.plan(3)] == [2, 3]


def test_escapes_outside_chosen_fights_count_as_flees():
    from collections import Counter

    r = _run("knight", "low", 10, "won", "fled")
    r.chosen = Counter({"won": 1, "fled": 1})
    r.escapes = Counter({"fled": 3}) - r.chosen  # nav saw 3 escapes: 1 from the chosen fight, 2 ambushes
    assert (r.count("won"), r.count("fled")) == (1, 3)


def test_ambush_fights_back_only_when_safe(monkeypatch):
    """Aggressive mobs attack on arrival. A careful player fights back when everything attacking is predicted to
    cost well under its HP, else escapes (it once fled 5 times from trolls predicted to cost a quarter of its HP)."""
    import asyncio
    from types import SimpleNamespace as NS

    from xania_sim.players import careful as C

    calls = []

    class Nav:
        here, level, visited, avoid, on_ambush = 2801, 10, set(), set(), None

        async def escape(self):
            calls.append("escape")
            return "fled"

    class Mobs:
        def in_view(self, text, here):
            return [2804, 2805][: text.count("fighting YOU")]

        def keyword(self, v):
            return "troll"

    async def fake_fight(bot, mob, keyword, **kw):
        calls.append(f"fight {mob}")
        return FightResult(mob, "won", 3, 100, 90, 114, 50, False, "")

    monkeypatch.setattr(C, "fight", fake_fight)
    bot = NS(prompt=NS(hp=114), name="Bot")
    lev = C.Leveller(bot, world=None, cls="knight", nav=Nav(), mobs=Mobs())
    assert lev.nav.on_ambush == lev.ambushed
    view = "Troll's Bedroom\n[Exits: north]\nThe giant troll is here, fighting YOU!\nThe large female troll is here, fighting YOU!"
    monkeypatch.setattr(lev, "crowd_damage", lambda mobs, level: 28.0)  # 25% of 114 HP: fight both
    asyncio.run(lev.ambushed(view))
    assert calls == ["fight 2804", "fight 2805"]
    calls.clear()
    monkeypatch.setattr(lev, "crowd_damage", lambda mobs, level: 80.0)  # 70%: get out
    asyncio.run(lev.ambushed(view))
    assert calls == ["escape"]
