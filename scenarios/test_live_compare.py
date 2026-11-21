"""compare counts more than weapon dice and three armor values (mattgodbolt/xania#144, option B): a verdict that
includes damroll (weapons) and AC vs magic plus AC affects (armor), then a vague line for each other stat that
differs, and rings or amulets compared by their stat lines alone.

A feature check, not a known-bug check: it fails before the change (compare said "looks worse" about the pin and
couldn't compare the rings) and passes after it. The items and the expected lines are #144's examples."""

from xania_sim.core.director import Director
from xania_sim.core.session import Bot, Profile

# vnum: the keyword the director gives it by (two of them are "ring", so not the first keyword)
ITEMS = {16018: "pin", 1603: "elven", 3043: "protection", 6601: "pink"}


async def test_compare_counts_bonuses_and_names_the_stats_that_differ(mud, names):
    bot = Bot(names(), "simpass1", host=mud.host, port=mud.port)
    await bot.enter(Profile())
    d = await Director.create(mud)
    try:
        await d.goto(3001)
        await d.bring(bot.name)
        for vnum, keyword in ITEMS.items():
            if vnum != 16018:
                r = await d.give_obj(bot.name, vnum, keyword)
                assert "You give" in r, r
        # The pin last: "pin" is also a prefix of "pink", and the newest item in the inventory is found first.
        # It's invisible (extra flag invis), so a new character couldn't be given it, or see it to compare.
        r = await d.do("load obj 16018", expect=r"Ok\.|No object")
        assert "Ok." in r, r
        await d.do("set obj pin extra -invis", expect=r"extra flags")
        r = await d.do(f"give pin {bot.name}", expect=r"You give|can't")
        assert "You give" in r, r
        reply = r"looks (?:better|worse) than|look about the same|You can't compare|You do not have|would \w+ you"
        weapons = (await bot.do("compare pin elven", expect=reply)).text
        rings = (await bot.do("compare protection pink", expect=reply)).text
    finally:
        await d.close()
    await bot.quit()

    # a very sharp pin (4d5, +1 damroll, -3 str, -15 hp) against a long elven dagger (5d4): the damroll flips it
    weapons, rings = weapons[-600:], rings[-600:]  # the replies are at the end; before them, the setup's output
    assert "A very sharp pin looks better than a long elven dagger." in weapons, weapons
    assert "A very sharp pin would make you weaker." in weapons, weapons
    assert "A very sharp pin would make you less healthy." in weapons, weapons
    assert "hit more often" not in weapons and "miss more often" not in weapons, weapons  # both +2 hitroll
    # a ring of protection (-2 AC, -1 save vs spell) against a pink ice ring (+1 str, +4 hp): stat lines only
    assert "You can't compare" not in rings, rings
    lines = [ln.strip() for ln in rings.splitlines() if ln.strip().startswith("A ring of protection")]
    assert lines == ["A ring of protection would protect you better than a pink ice ring.",
                     "A ring of protection would guard you better against magic.",
                     "A ring of protection would make you weaker.",
                     "A ring of protection would make you less healthy."], rings
