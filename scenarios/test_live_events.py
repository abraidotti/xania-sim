"""The event bus's immortal commands (abraidotti/xania#16): `events` lists every listener, and `deaths` counts deaths
per area from nothing but the CharKilled event. Needs a xania with the bus (feature/16-events)."""

import re

from xania_sim.core.director import Director

ARENA = 3108  # Midgaard's park path: no resets, no room flags
BEGGAR = 3065


async def test_events_lists_the_mob_program_listeners_and_the_death_counter(mud):
    d = await Director.create(mud)
    try:
        r = await d.do("events", expect=r"Events dropped for being nested too deeply: \d+\.")
    finally:
        await d.close()
    for event, listener in [("ActSeen", "mob programs: act"), ("CharArrived", "mob programs: entry, greet"),
                            ("CharKilled", "mob programs: death"), ("CharKilled", "death counter"),
                            ("CharSpoke", "mob programs: speech"), ("CombatRound", "mob programs: hitprcnt, fight"),
                            ("GoldGiven", "mob programs: bribe"), ("MobUpdate", "mob programs: random"),
                            ("ObjectGiven", "mob programs: give")]:
        assert re.search(rf"^{event} +0  {re.escape(listener)}$", r, re.M), r
    assert "Events dropped for being nested too deeply: 0." in r, r


async def test_deaths_counts_a_slain_mob_in_its_area(mud):
    d = await Director.create(mud)
    try:
        before = await d.do("deaths", expect=r"Nobody has died|Players +Mobs")
        m = re.search(r"^Midgaard +(\d+) +(\d+)$", before, re.M)
        players, mobs = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        await d.goto(ARENA)
        assert "Ok." in await d.do(f"load mob {BEGGAR}", expect=r"Ok\.|No mob")
        await d.do("slay beggar", expect=r"You slay")
        after = await d.do("deaths", expect=r"Players +Mobs")
        await d.do("purge", expect=None)
    finally:
        await d.close()
    assert re.search(rf"^Midgaard +{players} +{mobs + 1}$", after, re.M), after
