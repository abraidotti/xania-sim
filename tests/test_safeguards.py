"""Safeguards that keep a live trial from running long on a problem: the heartbeat, the navigator's loop guard,
the crash watcher, and the verdict on a level where nothing was killed."""

import asyncio
import json
from collections import Counter
from types import SimpleNamespace as NS

from xania_sim.activities.fight import FightResult
from xania_sim.activities.move import Navigator
from xania_sim.record.heartbeat import Heartbeat, Phases
from xania_sim.trials import area as A


def test_heartbeat_writes_lines(tmp_path, capsys):
    beat = Heartbeat(tmp_path / "run", "area")
    beat("knight 10", "explored", reached="3/5", escapes={"fled": 2})
    line = json.loads((tmp_path / "run" / "progress.jsonl").read_text().splitlines()[0])
    assert (line["who"], line["what"], line["reached"], line["escapes"]) == ("knight 10", "explored", "3/5", {"fled": 2})
    assert "area knight 10: explored (reached=3/5)" in capsys.readouterr().err


def test_phases_add_up():
    p = Phases()
    with p("explore"):
        pass
    with p("explore"):
        pass
    assert set(p.rounded()) == {"explore"}


def test_walk_gives_up_on_a_loop():
    """The Mud School loop: every step 'works' but the room isn't recognised, so the bot recalls and tries again.
    The walk must stop after a few rounds instead of spending all the bot's moves."""

    class Bot:
        name = "Loopy"

    nav = Navigator(Bot(), world=NS(rooms={}))
    nav.here = 1
    calls = Counter()

    def plan(goal, start=None):
        return [NS(command="up", from_vnum=nav.here, to_vnum=2, door=False, direction=4)]

    async def step(s):
        calls["step"] += 1
        nav.here = None  # arrived somewhere it can't tell apart
        return False

    async def locate():
        nav.recalls += 1  # can't tell: recall to the temple
        nav.here = 1
        return 1

    nav.plan, nav.step, nav.locate = plan, step, locate
    r = asyncio.run(nav.walk_to(3))
    assert not r.reached and r.reason.startswith("looping at room 1")
    assert calls["step"] <= 5 and nav.loops == [r.reason]


def test_play_band_stops_when_xania_dies(monkeypatch):
    monkeypatch.setattr(A, "CRASH_CHECK", 0.05)
    alive = iter([True, False])
    mud = NS(alive=lambda: next(alive, False))
    cancelled = []

    async def session():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    assert asyncio.run(A._play_band(mud, [session(), session()])) is True
    assert cancelled == [1, 1]


def _run(**kw):
    r = A.AreaRun("knight", "low", 10, staged=True, predicted=A.Prediction([2804], 0.1, 0.2, 0))
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_band_verdict_says_why_nothing_was_killed():
    won = FightResult(1, "won", 3, 100, 80, 100, 10, False, "")
    assert A.band_verdict([_run(fights=[won])]) == "ok: 1 kills"
    v = A.band_verdict([_run(escapes=Counter(fled=5), loops=["looping at room 3001"]),
                        _run(stopped="no safe targets at level 10")])
    assert v.startswith("unproductive: no kills") and "escapes fled 5" in v and "1 navigation loops" in v
    assert "stopped: no safe targets at level 10 (1)" in v
    none_safe = _run()
    none_safe.predicted = A.Prediction([], 0.0, None, 0)
    assert "no mob predicted safe" in A.band_verdict([none_safe])


def test_mobs_fighting_you_are_recognised():
    """room_look shows a mob attacking the bot as "<short> is here, fighting YOU!" (with tags like "(Hide) " in
    front). Not recognising it made every ambush look like one with no attackers, so the bot always fled."""
    from xania_sim.activities.fight import MobIndex

    world = NS(mobs={2800: NS(long_descr="A beast guards the den.\n", short_descr="the guardian beast", keywords="beast"),
                     2804: NS(long_descr="A giant troll is here.\n", short_descr="the giant troll", keywords="troll")},
               room_area={}, mob_area={})
    mi = MobIndex(world)
    view = "(Hide) the guardian beast is here, fighting YOU!\nThe giant troll is here, fighting YOU!"
    assert mi.in_view(view) == [2800, 2804]


def test_unrecorded_runs_still_know_known_findings(tmp_path, monkeypatch):
    """--no-record mustn't make a known crash look new (exit 1): statuses are read from the lore database,
    read-only. The JSON keeps each finding's summary, since the test MUD's logs are temporary."""
    import sqlite3

    from xania_sim.record import result as R
    from xania_sim.record import store
    from xania_sim.record.findings import Finding, Journal

    db = tmp_path / "lore.db"
    con = sqlite3.connect(db)
    j = Journal(con)
    run = j.start_run("churn", "t")
    known = Finding("crash", "crash:asan:Descriptor::is_switched", "uaf", "high", "Access: ... <- char_update")
    j.record(run, known)
    con.execute("UPDATE findings SET status = 'known'")
    con.commit()
    con.close()
    monkeypatch.setattr(store, "DEFAULT_PATH", db)
    res = R.TrialResult("area", "t")
    R.add_findings(res, [known, Finding("crash", "crash:new:x", "new one", "high", "chain")], record=False)
    assert [(f["status"], f["summary"]) for f in res.findings] == [("known", "Access: ... <- char_update"),
                                                                    ("new", "chain")]
    assert res.exit_code() == 1  # the new one still fails the run
    res.findings = res.findings[:1]
    assert res.exit_code() == 0  # the known one alone doesn't


def test_crowd_damage_uses_the_same_model_as_targets(monkeypatch):
    """Targets came from Explorer's caster model but the crowd check from the melee one, so a level-15 mage saw two
    trolls as 82 HP of risk (12 with its spells) and never attacked."""
    from xania_sim.players import careful as C

    seen = []

    def predict_fight(world, cls, level, vnum, caster=False):
        seen.append(caster)
        return NS(damage_taken=6.0 if caster else 41.0)

    monkeypatch.setattr(C.api, "predict_fight", predict_fight)
    lev = C.Leveller(NS(prompt=NS(hp=106)), world=None, cls="mage", nav=NS(on_ambush=None, level=15), mobs=NS())
    monkeypatch.setattr(C.Leveller, "caster", property(lambda self: True))
    assert lev.crowd_damage([2804, 2805], 15) == 12.0 and seen == [True, True]


def test_step_stands_up_from_resting():
    """A resting bot's moves are refused ("You're resting. Try standing up first!"): stand, then move."""

    class Bot:
        name = "Restful"
        prompt = NS(hp=10, max_hp=10)

        def __init__(self):
            self.sent = []

        async def do(self, command, expect=None, timeout=10):
            self.sent.append(command)
            if command == "south" and "stand" not in self.sent:
                return NS(text="You're resting. Try standing up first!")
            if command == "stand":
                return NS(text="You stand up.")
            return NS(text="Square\n\nA square.\n[Exits: north]\n")

    bot = Bot()
    nav = Navigator(bot, world=NS(rooms={}))
    nav.here = 1
    nav.recognise = lambda view, expected=None: expected
    ok = asyncio.run(nav.step(NS(command="south", door=False, from_vnum=1, to_vnum=2, direction=2)))
    assert ok and bot.sent == ["south", "stand", "south"]
