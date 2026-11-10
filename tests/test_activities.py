"""Loot, gear, trade, group and death: parsing the game's replies (formats from the C++ cited in each module) and
the decisions made from them."""

from xania_sim.activities import death, gear, group, items, loot, trade


def test_list_items_with_counts_and_flags():
    text = "the corpse of a goblin contains:\n( 2) a dagger\n(Glowing) (Humming) a blue potion\n23 gold coins\n\n"
    assert items.parse_list(text, r" contains:$") == [("a dagger", 2), ("a blue potion", 1), ("23 gold coins", 1)]
    assert items.parse_list("the corpse of a rat contains:\n     Nothing.\n", r" contains:$") == []


def test_loot_replies():
    assert loot.GOT.search("You get a dagger from the corpse of a goblin.")["what"] == "a dagger"
    for refusal in ("You can't take that.", "dagger: you can't carry that much weight.",
                    "|rCorpse looting is not permitted.|w", "I see nothing like that in the corpse.",
                    "Etaine forbids you from possessing more than one a sapphire ring."):
        assert loot.GET_FAILED.search(refusal), refusal


def test_wear_replies():
    assert gear.WORN.search("You wear a leather jacket on your body.")["what"] == "a leather jacket"
    assert gear.WORN.search("You wield a long sword.")["what"] == "a long sword"
    assert gear.WORN.search("You light a torch and hold it.")["what"] == "a torch"
    for refusal in ("You must be level 45 to use this object.", "It is too heavy for you to wield.",
                    "You are zapped by a holy symbol and drop it."):
        assert gear.REFUSED.search(refusal), refusal
    eq = "You are using:\n<worn on body>         a leather jacket\n<wielded>             a long sword\n"
    assert gear.parse_equipment(eq) == [("worn on body", "a leather jacket"), ("wielded", "a long sword")]


class FakeObjects:
    """name -> (vnum, value, level, slots)"""

    def __init__(self, table):
        self.t = table

    def best_vnum(self, name, level=None):
        return self.t[name][0] if name in self.t else None

    def value(self, vnum):
        return next(v for n, (vn, v, lv, s) in self.t.items() if vn == vnum)

    def level(self, vnum):
        return next(lv for n, (vn, v, lv, s) in self.t.items() if vn == vnum)

    def slots(self, vnum):
        return next(s for n, (vn, v, lv, s) in self.t.items() if vn == vnum)


def test_upgrades_compare_slot_by_slot():
    objs = FakeObjects({"a leather jacket": (1, 5.0, 10, ["body"]), "a plate mail": (2, 20.0, 15, ["body"]),
                        "a bone ring": (3, 2.0, 1, ["finger"]), "a gold ring": (4, 1.0, 1, ["finger"]),
                        "a demon blade": (5, 40.0, 60, ["wield"]), "a rock": (6, 0.0, 1, [])})
    worn = [("worn on body", "a leather jacket"), ("worn on finger", "a bone ring")]
    carried = [("a plate mail", 1), ("a gold ring", 1), ("a demon blade", 1), ("a rock", 1)]
    # plate beats the jacket; the gold ring fits the free finger; the blade is too high level; the rock isn't gear
    assert gear.upgrades(objs, 20, carried, worn) == ["a plate mail", "a gold ring"]
    assert gear.upgrades(objs, 20, [("a leather jacket", 1)], [("worn on body", "a plate mail")]) == []


def test_shop_stock_and_replies():
    text = "[Lv Price] Item\n[ 1    12] a torch.\n[10  1250] a long sword.\n"
    assert trade.parse_stock(text) == [trade.Stock(1, 12, "a torch"), trade.Stock(10, 1250, "a long sword")]
    assert trade.BOUGHT.search("You buy a torch.")["what"] == "a torch"
    assert trade.SOLD.search("You sell a dagger for 7 gold pieces.")["gold"] == "7"
    assert trade.VALUE.search("The shopkeeper tells you 'I'll give you 30 gold coins for a dagger'.")["gold"] == "30"
    assert trade.BUY_FAILED.search("The baker tells you 'You can't afford to buy a loaf of bread'.")


def test_group_listing():
    text = ("Hilda's group:\n[ 12 Kni] Hilda              120/ 140 hp   20/  20 mana  100/ 100 mv  4500 xp\n"
            "[ 10 Cle] Bram                80/  90 hp   60/ 110 mana   90/ 100 mv  3100 xp\n")
    leader, members = group.parse_group(text)
    assert leader == "Hilda" and [m.name for m in members] == ["Hilda", "Bram"]
    assert members[1] == group.Member("Bram", 10, "Cle", 80, 90, 60, 110)
    assert group.JOINED.search("Bram joins your group.")["who"] == "Bram"
    assert group.SPLIT.search("You split 40 gold coins.  Your share is 20 gold coins.")["share"] == "20"


def test_death():
    assert death.died("The goblin's slash DEMOLISHES you!\n|RYou have been KILLED!!|w")
    assert not death.died("You have been killed by boredom")
    assert death.corpse_contents("corpse of Hilda contains:\na long sword\n( 3) a bread\n") == [
        ("a long sword", 1), ("a bread", 3)]
