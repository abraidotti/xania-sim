"""Trade: finding a shop, reading its stock, buying, selling and asking what something is worth.

Shops are the area files' #SHOPS keepers (Explorer: ``shops`` per area file) in the rooms where they spawn.
``list`` (act_obj.cpp do_list, 1970-2015) prints "[Lv Price] Item" then "[{:2} {:5}] {short}." per item, or "You
can't buy anything here." / "You can't do that here." (no shopkeeper). ``buy`` (1829-1950): "You buy $p." (after
"You haggle the price down to N coins." sometimes), or "$n tells you 'I don't sell that -- try 'list''.", "... 'You
can't afford to buy $p'.", "... 'You can't use $p yet'.", "You can't carry that many items.", "You can't carry that
much weight.". ``sell`` (2018-2065): "You sell $p for N gold piece(s)." or "... 'You don't have that item'.", "$n
looks uninterested in $p.", "... 'I'm afraid I don't have enough gold to buy $p.", "You can't let go of it.".
``value`` (2080-2107): "$n tells you 'I'll give you N gold coins for $p'.".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from xania_sim.record.result import Outcome

ROW = re.compile(r"^\[\s*(?P<level>\d+)\s+(?P<price>\d+)\]\s+(?P<name>.+?)\.?\s*$", re.M)
LIST_REPLY = re.compile(r"\[Lv Price\] Item|You can't buy anything here|You can't do that here|Pets for sale|"
                        r"out of pets", re.M)
BOUGHT = re.compile(r"^You buy (?P<what>.+?)\.$", re.M)
HAGGLE = re.compile(r"You haggle the price down to (\d+) coins")
BUY_FAILED = re.compile(r"I don't sell that|You can't afford to buy|You can't use .+? yet|"
                        r"You can't carry that many items|You can't carry that much weight|forbids you from possessing|"
                        r"You can't do that here|Buy what", re.M)
SOLD = re.compile(r"^You sell (?P<what>.+?) for (?P<gold>\d+) gold pieces?\.$", re.M)
SELL_FAILED = re.compile(r"You don't have that item|looks uninterested in|don't have enough gold to buy|"
                         r"You can't let go of it|doesn't see what you are offering|You can't do that here|Sell what",
                         re.M)
VALUE = re.compile(r"I'll give you (?P<gold>\d+) gold coins for (?P<what>.+?)'\.")


@dataclass(frozen=True)
class Stock:
    level: int
    price: int
    name: str


def parse_stock(text: str) -> list[Stock]:
    return [Stock(int(m["level"]), int(m["price"]), m["name"]) for m in ROW.finditer(text)]


def shop_rooms(world) -> dict[int, int]:
    """{room vnum: shopkeeper vnum} for every shop, where its keeper spawns."""
    from xania_explorer.assess import mobs as M

    keepers = {s.keeper for f in world.files for s in f.shops}
    out = {}
    for info in M.collect(world):
        if info.mob.vnum in keepers:
            for sp in info.spawns:
                out[sp.room_vnum] = info.mob.vnum
    return out


async def go_to_shop(nav, world, sells=None, tries: int = 4) -> Outcome:
    """Walk to the nearest shop (optionally one whose stock ``sells(Stock)`` likes; checked on arrival)."""
    from xania_explorer import api

    if nav.here is None:
        await nav.locate()
    dist = api.distances(world, nav.here) if nav.here is not None else {}
    rooms = sorted((r for r in shop_rooms(world) if r in dist), key=lambda r: dist[r])
    for room in rooms[:tries]:
        walk = await nav.walk_to(room)
        if not walk.reached:
            continue
        stock = await list_stock(nav.bot)
        if stock.ok and (sells is None or any(sells(s) for s in stock.data["stock"])):
            return Outcome("trade", True, f"at the shop in room {room}", stock.evidence, {"room": room, **stock.data})
    return Outcome("trade", False, "no shop reachable" if not rooms else "no suitable shop reached", [],
                   {"tried": rooms[:tries]})


async def list_stock(bot) -> Outcome:
    r = await bot.do("list", expect=LIST_REPLY)
    stock = parse_stock(r.text)
    if stock:
        return Outcome("trade", True, f"{len(stock)} items for sale", [s.name for s in stock[:5]],
                       {"stock": stock})
    m = LIST_REPLY.search(r.text)
    return Outcome("trade", False, "nothing for sale here", [m.group(0) if m else r.text.strip()[:200]],
                   {"stock": []})


async def buy(bot, keyword: str) -> Outcome:
    r = await bot.do(f"buy {keyword}", expect=re.compile(BOUGHT.pattern + "|" + BUY_FAILED.pattern, re.M))
    m = BOUGHT.search(r.text)
    if m:
        h = HAGGLE.search(r.text)
        return Outcome("trade", True, f"bought {m['what']}", [m.group(0)],
                       {"item": m["what"], "haggled_to": int(h.group(1)) if h else None})
    f = BUY_FAILED.search(r.text)
    return Outcome("trade", False, f"couldn't buy {keyword}", [f.group(0) if f else r.text.strip()[:200]],
                   {"keyword": keyword})


async def sell(bot, keyword: str) -> Outcome:
    r = await bot.do(f"sell {keyword}", expect=re.compile(SOLD.pattern + "|" + SELL_FAILED.pattern, re.M))
    m = SOLD.search(r.text)
    if m:
        return Outcome("trade", True, f"sold {m['what']} for {m['gold']}", [m.group(0)],
                       {"item": m["what"], "gold": int(m["gold"])})
    f = SELL_FAILED.search(r.text)
    return Outcome("trade", False, f"couldn't sell {keyword}", [f.group(0) if f else r.text.strip()[:200]],
                   {"keyword": keyword})


async def value(bot, keyword: str) -> Outcome:
    r = await bot.do(f"value {keyword}", expect=re.compile(VALUE.pattern + "|" + SELL_FAILED.pattern, re.M))
    m = VALUE.search(r.text)
    if m:
        return Outcome("trade", True, f"{m['what']} is worth {m['gold']}", [m.group(0)], {"gold": int(m["gold"])})
    f = SELL_FAILED.search(r.text)
    return Outcome("trade", False, f"no value for {keyword}", [f.group(0) if f else r.text.strip()[:200]], {})
