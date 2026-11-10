"""Practice: the table from do_practice, and what a class spends its sessions on."""

from xania_sim.activities.practice import ENOUGH, parse_table, plan

TABLE = ("axe                  1%  dagger              40%  flail                1%\n"
         "mace                 1%  polearm              1%  shield block         1%\n"
         "sword               40%  whip                 1%  parry                1%\n"
         "second attack       12%  recall              50%\n"
         "You have 5 practice sessions left.\n")


def test_the_practice_table_is_read():
    skills, sessions = parse_table(TABLE)
    assert sessions == 5 and skills["sword"] == 40 and skills["second attack"] == 12 and skills["recall"] == 50
    assert len(skills) == 11


def test_sessions_go_to_the_class_priorities_in_turn():
    skills, sessions = parse_table(TABLE)
    assert plan("knight", skills, sessions) == ["sword", "parry", "shield block", "second attack", "sword"]
    assert plan("knight", skills, 0) == []
    done = {s: ENOUGH for s in skills}
    assert plan("knight", done, 3) == []
