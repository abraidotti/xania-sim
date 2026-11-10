"""Event parsing against lines captured from real transcripts, and the recorder/progress views on an in-memory db."""

import sqlite3

from xania_sim.record.events import Progress, Recorder, parse
from xania_sim.core.parsers import Prompt

# from runs/level-101536 (a level-1 knight against a small spider in the sewers), plus messages from the source
CAPTURED = """
The small spider trips you and you go down!
The small spider's trip eviscerates your head.
You have become better at sword! (45)
<bot 7/20 100/100 85/100 1028 0 0>
The small spider is DEAD!!
You receive 286 experience points.
Someone hits the ground ... DEAD.
You get 54 gold coins from corpse of the small spider.
Etaine gives you 2 gold coins for your sacrifice.
You get a rusty dagger from corpse of the small spider.
You raise a level!!
Your gain is: 11/31 hp, 3/103 m, 4/104 mv 4/9 prac.
You learn from your mistakes, and your parry skill improves. (3)
Hassan tells you 'need a hand?'
Hassan says 'over here'
Hassan gossips 'anyone seen the dragon?'
You flee from combat!  You lose 10 exps.
PANIC! You couldn't escape!
Hassan now follows you.
Hassan joins your group.
You have been KILLED!!
"""


def test_captured_lines_become_events():
    ev = parse(CAPTURED)
    assert [e.kind for e in ev] == ["knocked", "skill", "kill", "xp", "gold", "gold", "loot", "level", "gain", "skill",
                                    "tell", "say", "channel", "fled", "flee_failed", "follow", "group", "died"]
    by = {e.kind: e.data for e in ev}
    assert by["knocked"] == {"by": "The small spider"}
    assert by["kill"] == {"victim": "The small spider"}
    assert by["xp"] == {"amount": 286}
    assert [e.data for e in ev if e.kind == "gold"] == [
        {"amount": 54, "source": "corpse", "of": "the small spider"},
        {"amount": 2, "source": "sacrifice", "god": "Etaine"}]
    assert by["loot"] == {"item": "a rusty dagger", "of": "the small spider"}
    assert by["gain"]["max_hp"] == 31 and by["gain"]["practices"] == 4
    assert by["skill"] == {"skill": "parry", "percent": 3, "from": "failure"}
    assert by["channel"] == {"channel": "gossips", "from": "Hassan", "text": "anyone seen the dragon?"}
    assert by["tell"]["text"] == "need a hand?"


def test_ordinary_lines_are_not_events():
    assert parse("The Sewer Store Room\n[Exits: north]\nThe small spider is here, fighting YOU!\n") == []


class FakeBot:
    def __init__(self):
        self.name, self.prompt, self.observers = "Lvlknia", None, []


def _prompt(hp=20, xp=1000, gold=0):
    return Prompt(hp, 20, 100, 100, 85, 100, xp, gold, 0)


def test_recorder_stores_events_with_context_and_snapshots():
    db, clock = sqlite3.connect(":memory:"), [1000.0]
    bot = FakeBot()
    rec = Recorder(db, bot, cls="knight", race="human", level=1, run_id="r1", room=lambda: 3150,
                   area_of=lambda v: "Sewers", clock=lambda: clock[0])
    rec.target = "the small spider"
    assert bot.observers == [rec.feed]
    rec.feed("The small spider is DEAD!!\nYou receive 286 experience points.\n", _prompt(xp=1286))
    clock[0] += 10
    rec.feed("Your slash hits.\n", _prompt(hp=18))  # no events, and too soon for another snapshot
    clock[0] += 10
    rec.feed("You raise a level!!\n", _prompt(xp=2000))  # a level always snapshots
    clock[0] += 100
    rec.feed("You have been KILLED!!\n", _prompt(hp=1))
    p = Progress(db)
    [s] = p.bots()
    assert (s.name, s.cls, s.level, s.kills, s.deaths, s.levels_gained) == ("Lvlknia", "knight", 2, 1, 1, 1)
    assert p.levels("Lvlknia") == [(1000.0, 1), (1020.0, 2)]
    died = p.events("Lvlknia", ("died",))[0]
    assert died["data"] == {"killer": "the small spider"} and died["area"] == "Sewers" and died["room"] == 3150
    assert p.by_area() == [("Sewers", 1, 1, 286)]
    rec.detach()
    assert bot.observers == []


def test_the_dispatch_gets_bot_pages(tmp_path):
    from xania_sim.report import dispatch as D
    from xania_sim.activities.fight import Comparison
    from xania_sim.record.findings import Journal

    db, clock = sqlite3.connect(":memory:"), [1000.0]
    rec = Recorder(db, FakeBot(), cls="knight", race="human", level=1, room=lambda: 3150, area_of=lambda v: "Sewers",
                   clock=lambda: clock[0])
    rec.target = "the <small> spider"
    rec.feed("The small spider is DEAD!!\n", _prompt())
    clock[0] += 3000
    rec.feed("You raise a level!!\n", _prompt())
    cmp = [Comparison(3101, "the small spider", "knight", 1, 4, 0.75, 0.42, 280.0, 0.3, 300.0, False)]
    site = D.build(Journal(db), tmp_path / "site", Progress(db), cmp)
    page = (site / "bots" / "lvlknia.html").read_text(encoding="utf-8")
    assert "<svg class='chart'" in page and "reached level 2" in page and "Sewers" in page
    assert "predicted vs seen" in page and "42%" in page
    assert "Lvlknia" in (site / "index.html").read_text(encoding="utf-8")


def test_the_killer_is_whoever_was_hitting_not_the_last_target():
    from xania_sim.record.events import attacker

    assert attacker("Your pierce hits the gutter rat.\nThe small spider's slash splits your head.\n") == "the small spider"
    assert attacker("The small spider's trip misses your left shin.\n") == "the small spider"
    assert attacker("The small spider is here, fighting YOU!\n") == "the small spider"
    assert attacker("Hassan tells you 'it's your turn'\nYour slash hits the rat's head.\n") is None
    db = sqlite3.connect(":memory:")
    rec = Recorder(db, FakeBot(), level=1)
    rec.target = "a gutter rat"  # what it last chose to fight...
    rec.feed("The small spider's slash splits your head.\n", _prompt(hp=2))  # ...isn't what's killing it
    rec.feed("You have been KILLED!!\n", _prompt(hp=1))
    died = Progress(db).events("Lvlknia", ("died",))[0]
    assert died["data"]["killer"] == "the small spider"
    close = Progress(db).events("Lvlknia", ("close_call",))[0]
    assert close["data"]["against"] == "the small spider" and close["data"]["hp"] == 2
