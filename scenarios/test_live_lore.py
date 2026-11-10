"""The scanned source agrees with the running game: each class's own spells and skills lists match the scan."""

import pytest

from xania_sim.core.repo import GameRepo
from xania_sim.record.scan import crosscheck, parse_ability_list, scan_source
from xania_sim.core.session import Profile


@pytest.fixture(scope="module")
def repo():
    return GameRepo()


@pytest.fixture(scope="module")
def facts(repo):
    return scan_source(repo.src_dir)


def test_the_build_is_current(repo):
    check = repo.build_check()
    assert check.built and not check.stale, check.reason
    assert len(repo.version().commit) == 40


@pytest.mark.parametrize("cls", ["mage", "cleric", "knight", "barbarian"])
async def test_class_lists_match_the_source(new_bot, facts, cls):
    bot = await new_bot(Profile(cls=cls))
    spells = parse_ability_list((await bot.do("spells", expect=r"^L ?\d+:|You know no spells")).text)
    skills = parse_ability_list((await bot.do("skills", expect=r"^L ?\d+:|You know no skills")).text)
    assert skills, "every class starts with some skills"
    if cls == "mage":
        assert spells["magic missile"][0] == 1 and len(spells) >= 3
    assert crosscheck(spells | skills, facts, cls) == []
    assert all(facts[n].is_spell for n in spells) and not any(facts[n].is_spell for n in skills)
