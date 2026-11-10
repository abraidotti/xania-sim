"""Gear kits against the real world data the test MUD loads (skipped when the checkout can't be read)."""

import pytest

from xania_sim.setup import kits as K


@pytest.fixture(scope="module")
def kits():
    try:
        from xania_sim.core import world as W

        return K.kits_for(W.load())
    except Exception as e:  # no WSL checkout or no Explorer: nothing to test against
        pytest.skip(f"no world data: {e}")


CLASSES = ["mage", "cleric", "knight", "barbarian"]


@pytest.mark.parametrize("cls", CLASSES)
@pytest.mark.parametrize("name", K.KITS)
def test_every_piece_can_go_on(kits, cls, name):
    for align in "gne":
        kit = kits.build(name, 40, cls, align)
        assert all(p.level <= 40 for p in kit.worn), kit.describe()
        for p in kit.pieces:
            assert K.ANTI[align] not in kits.by_vnum[p.vnum].alignment_flags, (p, align)
            assert K.natural_slot(kits.by_vnum[p.vnum]) == p.slot or p.slot == "wield", p
        slots = [p.slot for p in kit.worn]
        for slot, n in K.E.SLOTS.items():
            assert slots.count(slot) <= n, (slot, kit.describe())
        assert slots.count("wield") == 1


def test_bare_is_the_school_outfit(kits):
    kit = kits.build("bare", 40, "knight")
    assert kit.pieces == []
    assert {p.vnum for p in kit.base} == {3716, 3703, 3704, 3702}
    assert kits.build("bare", 40, "mage").weapon.vnum == 3701


def test_kits_rank_as_named(kits):
    t = {n: k.totals() for n, k in kits.all(40, "knight").items()}
    assert t["best-ac"].ac >= t["best"].ac >= t["shop"].ac > t["floor"].ac > t["bare"].ac
    assert t["best-hitdam"].hitroll + t["best-hitdam"].damroll >= t["best"].hitroll + t["best"].damroll
    assert t["best"].weapon_average >= t["shop"].weapon_average > t["floor"].weapon_average


def test_best_ac_is_bounded_by_explorers_kit(kits):
    """Explorer's ac kit ignores alignment, wear order and uniqueness, so it's the ceiling."""
    explorer = kits.analysis.ac_kits[40].total("ac")
    ours = sum(p.ac for p in kits.build("best-ac", 40, "knight").pieces if p.slot != "wield")
    assert 0.8 * explorer <= ours <= explorer


def test_shop_kit_is_all_shop_stock(kits):
    kit = kits.build("shop", 40, "mage")
    assert kit.pieces and all("shop" in p.sources for p in kit.pieces)


def test_weapon_follows_the_class_profile(kits):
    assert kits.build("best", 40, "knight").weapon.weapon_type in {"sword", "dagger", "mace"}
    assert kits.build("best", 40, "barbarian").weapon.weapon_type == "axe"
    assert kits.build("best", 40, "mage").weapon.weapon_type == "dagger"
    limit = K.wield_limit("mage", "human", kits.src_dir)
    assert limit == 14  # human str 14: str_app[14].wield
    assert kits.by_vnum[kits.build("best", 40, "mage").weapon.vnum].obj.weight <= limit


def test_unique_items_are_not_doubled(kits):
    for name in ("best", "best-ac", "best-hitdam"):
        kit = kits.build(name, 60, "knight")
        for p in kit.pieces:
            if K.unique(kits.by_vnum[p.vnum]):
                assert sum(q.vnum == p.vnum for q in kit.pieces) == 1, (name, p)


def test_two_handed_weapon_drops_the_shield(kits):
    kit = kits.build("best", 60, "knight", two_handed=True)
    if not kit.weapon.two_handed:
        pytest.skip("no two-hander for knights at 60")
    assert not any(p.slot == "shield" for p in kit.worn)
    assert [p.vnum for p in kit.remove] == [3704]
    large = kits.build("best", 60, "knight", race="minotaur", two_handed=True)
    assert any(p.slot == "shield" for p in large.worn)  # Large: both


def test_take_two_of_slot(monkeypatch):
    a, b = object(), object()
    monkeypatch.setattr(K, "unique", lambda i: i is a)  # a is unique, b isn't
    assert K._take([a, b], 2) == [a, b]
    assert K._take([b, a], 2) == [b, b]
    assert K._take([a], 2) == [a]
    assert K._take([b], 1) == [b]


def test_totals_table_lists_each_kit(kits):
    table = K.totals_table(kits.all(40, "cleric"))
    assert all(n in table for n in K.KITS)
