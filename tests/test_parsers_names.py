import pytest

from xania_sim.core import parsers as P
from xania_sim.core.names import bot_names, name_problem


def test_prompt_format_round_trips():
    m = P.PROMPT.search("Ok - prompt set.\n<bot 20/20 100/100 98/100 1000 0 4>\n")
    p = P.parse_prompt(m)
    assert (p.hp, p.max_hp, p.mana, p.moves, p.max_moves, p.xp, p.gold, p.wait) == (20, 20, 100, 98, 100, 1000, 0, 4)
    assert p.hp_share == 1.0
    assert P.PROMPT.search("<bot -3/20 0/100 0/100 0 0 0>\n").group(1) == "-3"


def test_prompt_format_uses_only_known_tokens():
    # xania/src/PromptFormat.cpp: h H m M v V X g c n
    assert set(P.PROMPT_FORMAT.split("%")[1:]) <= {t + rest for t in "hHmMvVXgcn" for rest in ("", "/", " ", ">")}


def test_lines_drop_blank_padding():
    assert P.lines("\nA room\n  \nExits: north\n") == ["A room", "Exits: north"]


@pytest.mark.parametrize("name,problem", [
    ("Al", None), ("A", "must be 2-12 letters"), ("Abcdefghijklm", "must be 2-12 letters"), ("Bot1", "letters only"),
    ("Illi", "only i and l"), ("Self", "reserved word"), ("Death", None),
])
def test_name_rules(name, problem):
    assert name_problem(name) == problem


def test_names_that_prefix_a_mob_keyword_are_refused():
    assert name_problem("Smokey", {"smokey", "joe"}) == "matches the mob keyword 'smokey'"
    assert name_problem("Smo", {"smokey"}) == "matches the mob keyword 'smokey'"
    assert name_problem("Smokeys", {"smokey"}) is None


def test_bot_names_are_distinct_and_skip_clashes():
    names = bot_names("tst", 30, {"tstb"})
    assert len(set(names)) == 30 and "Tstb" not in names
    assert names[:3] == ["Tsta", "Tstc", "Tstd"] and names[25] == "Tstba"
