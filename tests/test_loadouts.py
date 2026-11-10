"""Skill loadouts and the get_skill_level port, against the C++ tables of the test MUD's checkout."""

import pytest

from xania_sim.setup import loadouts as L
from xania_sim.setup import stage as S


@pytest.fixture(scope="module")
def src():
    try:
        from xania_sim.core import world as W

        return W.load().src_dir
    except Exception as e:
        pytest.skip(f"no world data: {e}")


def test_skill_level_port(src):
    # skills.cpp get_skill_level: class level, cross-class 60, specials 30/60, race bonus 30, unattainable 0
    assert L.skill_level("knight", "bash", src_dir=src) == 5
    assert L.skill_level("mage", "bash", src_dir=src) == 60  # SkillRatingAttainable
    assert L.skill_level("knight", "fireball", src_dir=src) == 60  # rated for knights, at 60
    assert L.skill_level("mage", "backstab", src_dir=src) == 60  # SkillRatingSpecial without the assassin group
    assert L.skill_level("mage", "backstab", assassin=True, src_dir=src) == 30
    assert L.skill_level("mage", "backstab", "hobbit", src_dir=src) == 30  # race bonus
    assert L.skill_level("mage", "bash", "dragon", src_dir=src) == 30
    assert L.skill_level("knight", "high explosive", src_dir=src) == 0  # unattainable


def test_unattainable_skills_count_as_usable(src):
    assert L.usable("knight", "high explosive", 1, src_dir=src)
    e = L.entry("knight", "high explosive", 75, 40, src_dir=src)
    assert e.usable and "unattainable" in e.why
    assert not L.entry("knight", "fireball", 75, 40, src_dir=src).usable
    assert not L.entry("mage", "bash", 75, 40, src_dir=src).usable
    assert L.entry("mage", "bash", 75, 60, src_dir=src).why == "cross-class from level 60"


def test_named_loadouts(src):
    none = L.build_loadout("none", "knight", 40, src_dir=src)
    assert none.settings == {} and none.reset == 0
    passives = L.build_loadout("passives", "knight", 40, src_dir=src).settings
    assert {"parry", "dodge", "shield block", "second attack", "third attack", "enhanced damage"} <= set(passives)
    assert set(passives.values()) == {75}
    mage = L.build_loadout("passives", "mage", 25, src_dir=src).settings
    assert "dodge" in mage and "second attack" not in mage  # mage second attack: level 30
    cls = L.build_loadout("class", "knight", 40, src_dir=src)
    assert all(e.usable and e.level > 0 for e in cls.entries.values())
    assert "high explosive" not in cls.settings and "fireball" not in cls.settings
    assert "bash" in cls.settings and set(cls.expected_practice()) == set(cls.settings)
    assert "fireball" in L.build_loadout("class", "knight", 60, src_dir=src).settings


def test_natural_loadout(src):
    knight = L.build_loadout("natural", "knight", 40, src_dir=src).settings
    assert knight["sword"] > 75 and knight["parry"] > 50  # practised to 75, then used
    assert "dodge" not in knight  # not in the knight default package, and bots never gain skills
    assert all(0 < v <= 100 for v in knight.values())
    low = L.natural_skills("knight", 5, src_dir=src)
    assert low["sword"] < knight["sword"] and "second attack" not in low  # level 7


def test_custom_loadout(src, tmp_path):
    f = tmp_path / "duel.toml"
    f.write_text('base = "passives"\nbash = 90\nfireball = 75\n', encoding="utf-8")
    lo = L.build_loadout("custom", "knight", 40, path=f, src_dir=src)
    assert lo.settings["bash"] == 90 and lo.settings["parry"] == 75
    assert [e.skill for e in lo.unusable] == ["fireball"]
    assert "fireball" not in lo.expected_practice()
    f.write_text("bashh = 90\n", encoding="utf-8")
    with pytest.raises(KeyError):
        L.load_custom(f, "knight", 40, src_dir=src)
    f.write_text("bash = 900\n", encoding="utf-8")
    with pytest.raises(ValueError):
        L.load_custom(f, "knight", 40, src_dir=src)


EQUIPMENT = """You are using:
<used as light>     (Glowing) a war banner
<worn on finger>    (U) (Invis) a sapphire ring
<worn around neck>  (U) a long grey cloak
<worn around wrist> (Glowing) Wolf bracers
<worn around wrist> (Glowing) Wolf bracers
<wielded>           (Glowing) a silvery sword
"""


def test_parse_equipment_and_missing_pieces():
    from xania_sim.setup.kits import Piece

    eq = S.parse_equipment(EQUIPMENT)
    assert eq[0] == ("used as light", "(Glowing) a war banner") and len(eq) == 6
    want = [Piece("finger", 911, "a sapphire ring", "ring", 34)] * 2 + [Piece("wrist", 10113, "Wolf bracers", "wolf",
                                                                              33)] * 2
    missing = S.missing_pieces(want, eq)
    assert [p.slot for p in missing] == ["finger"]


def test_skill_mismatches_allow_learning_by_use():
    assert S.skill_mismatches({"parry": 75, "fast healing": 75}, {"parry": 75, "fast healing": 77}) == {}
    assert S.skill_mismatches({"parry": 75}, {"parry": 60}) == {"parry": (75, 60)}
    assert S.skill_mismatches({"parry": 75}, {}) == {"parry": (75, None)}
    assert S.skill_mismatches({}, {"recall": 40}) == {"recall": (None, 40)}
