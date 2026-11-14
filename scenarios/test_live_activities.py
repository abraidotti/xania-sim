"""The everyday activities on the real game: loot, gear, trade, group and death. The director sets each one up (a
level 20 knight, a drunk holding a sword, gold for the shops, a second bot to group with, a slaying)."""

import pytest
from xania_explorer.assess import mobs as M

from xania_sim.activities import death, gear, group, loot, trade
from xania_sim.activities.fight import fight
from xania_sim.activities.items import Objects
from xania_sim.activities.move import Navigator
from xania_sim.core import world as W
from xania_sim.core.director import Director
from xania_sim.core.names import bot_names, mob_words
from xania_sim.core.server import TestMud
from xania_sim.core.session import Bot, Profile
from xania_sim.record.rooms import Fingerprints

pytestmark = pytest.mark.live
MOB, SWORD, JACKET = 3064, 3021, 9523  # the drunk (level 2, doesn't flee); a short sword; a leather jacket (level 20)

OWN_PORT = 9102  # its own fresh MUD: not the session's 9100, which may already be up



def _mob_room(w) -> int:
    info = next(i for i in M.collect(w) if i.mob.vnum == MOB)
    return info.spawns[0].room_vnum


async def test_everyday_activities():
    w = W.load()
    objects = Objects(w)
    names = iter(bot_names("acts", 50, mob_words(w)))
    async with TestMud(port=OWN_PORT) as mud:
        d = await Director.create(mud)
        try:
            bot = Bot(next(names), "actspass1", host=mud.host, port=mud.port)
            await bot.enter(Profile(cls="knight"))
            nav = Navigator(bot, w, Fingerprints(w))
            room = _mob_room(w)
            await d.goto(room)
            await d.bring(bot.name)
            await d.advance(bot.name, 20)
            await d.restore(bot.name)
            await bot.do("score", expect=r"Level: \S*20\b", timeout=30)  # after all the level-up messages
            nav.here = room

            # loot: a drunk holding a sword dies, and its corpse is looted one item at a time
            await d.do(f"load mob {MOB}", expect=r"Ok\.")
            await d.do(f"load obj {SWORD}", expect=r"Ok\.")
            await d.do("give sword 1.drunk", expect=r"You give|aren't|can't")
            await d.goto(3001)  # out of the way, so the kill is the bot's
            r = await fight(bot, MOB, "drunk")
            assert r.outcome == "won", r
            got = await loot.loot(bot, w, "corpse", objects=objects)
            assert any("sword" in t for t in got.data["taken"]), got

            # gear: a better body armour goes on, the sword is wielded only if it's better than the school weapon
            await d.goto(room)
            await d.give_obj(bot.name, JACKET)
            worn = await gear.equip_best(bot, w, 20, objects)
            assert "a leather jacket" in worn.data["worn"], worn
            eq = await gear.equipment(bot)
            assert any("jacket" in item for _, item in eq), eq

            # trade: walk to the nearest shop, see its stock, buy the cheapest thing, value it, sell it back
            await d.set_char(bot.name, "gold", 2000)
            at = await trade.go_to_shop(nav, w)
            assert at.ok, at
            cheapest = min(at.data["stock"], key=lambda s: s.price)
            bought = await trade.buy(bot, objects.keyword(cheapest.name))
            assert bought.ok, bought
            # a shop only buys the item types it trades in (act_obj.cpp get_cost): "uninterested" is a real answer
            valued = await trade.value(bot, objects.keyword(cheapest.name))
            assert valued.ok or "uninterested" in valued.evidence[0], valued
            sold = await trade.sell(bot, objects.keyword(cheapest.name))
            assert sold.ok or "uninterested" in sold.evidence[0], sold

            # group: a second bot follows and joins; the leader splits gold
            mate = Bot(next(names), "actspass1", host=mud.host, port=mud.port)
            await mate.enter(Profile(cls="cleric"))
            await d.goto(nav.here)
            await d.peace()  # Midgaard's wanderers pick fights; a group forms out of combat
            await d.bring(mate.name)
            formed = await group.form(bot, [mate])
            assert formed.ok, formed
            leader, members = await group.members(bot)
            assert {m.name for m in members} == {bot.name, mate.name}
            assert (await group.split(bot, 100)).ok

            # death: slain, the bot wakes at the altar and fetches its things from its corpse
            died_in = nav.here
            await d.goto(died_in)
            s = await d.do(f"slay {bot.name}", expect=r"You slay|They aren't|Slay")
            assert "slay" in s.lower(), s
            back = await bot.listen(3)
            nav.here = death.ALTAR
            rec = await death.recover(bot, nav, w, died_in, objects)
            assert rec.ok and rec.data["taken"], rec
            eq = await gear.equipment(bot)
            assert any("jacket" in item for _, item in eq), (eq, back.text)
            await mate.quit()
            await bot.quit()
        finally:
            await d.close()
