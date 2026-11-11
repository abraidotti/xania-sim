# Writing a scenario

Scenarios are pytest tests in `scenarios/`, and they make up the live regression suite. The fixtures start one
test MUD per session and make bots on it:

```python
from xania_sim import Profile

async def test_mage_starts_with_magic_missile(new_bot):
    bot = await new_bot(Profile(race="elf", cls="mage"))
    r = await bot.do("spells")
    assert r.has("magic missile")
```

`bot.do(command)` sends one command and returns its reply: `text`, `lines`, `has(regex)`, and the `prompt` that
ended it (HP, mana, moves, XP, gold, wait state). Long output is paged through.

The MUD also prompts on its own (ticks, combat rounds, other players), so pass `expect=` when it matters: a regex
the reply must contain. `bot.do("north", expect=r"^\[Exits:|Alas")` reads past stray prompts until the real reply
arrives, and returns `complete=False` if it never does.

To move around, `Navigator(bot, world)` gives `locate()`, `walk_to(vnum)` and `explore(area)`, using Explorer's map.

The MUD refuses names that match a mob keyword; the `names` fixture hands out ones it accepts.

```sh
python -m pytest -m "not live"    # unit tests only, against a fake MUD
python -m pytest                  # everything, live scenarios included
python -m pytest -m "not slow"    # skip scenarios that wait for regen ticks
```
