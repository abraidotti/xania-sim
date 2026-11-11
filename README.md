# Xania Sim

Xania Sim tests [Xania](https://github.com/mattgodbolt/xania) by playing it. Its bots connect over telnet, like
any MUD client, and do what players do: create characters, explore, fight, loot, shop, practise, level, quit. They
play carefully, carelessly or awkwardly, and they report what went wrong: a crash comes back with a source-level
backtrace, and an odd result comes back with the game's own words as evidence.

- **Finding bugs** that only show up in play: a crash on autoloot, a quit landing on a tick, an empty reply at a
  login prompt.
- **Testing features**: a skill, a spell or an area, at the levels it's meant for, with each class.
- **Measuring the game** against [Xania Explorer](https://github.com/abraidotti/xania-explorer)'s predictions:
  Explorer works out from the area files and C++ what should happen; the bots see what does happen.

Every run uses its own isolated test MUD, started from your Xania build with a throwaway data directory, so it
never touches a game people are playing on. (Xania is the game; `xania` is its server process.)

## Setup

1. A Xania checkout built with `make`. By default Xania Sim looks for it at `~/xania`; set `XANIA_SIM_XANIA`, or
   pass `--xania PATH` before a command, to use another.
2. Python 3.12 or later, with Xania Sim and Xania Explorer installed:

```
python -m venv .venv
source .venv/bin/activate            # .venv\Scripts\activate on Windows
python -m pip install -e ".[dev,explorer]"
```

(With Explorer checked out next to this repo, `python -m pip install -e ".[dev]" -e ../xania-explorer` uses that
copy instead.)

## Quick start

```
python -m xania_sim smoke                 # start a test MUD, create a bot, look around, quit
python -m pytest -m "not live"            # unit tests, against a fake MUD: no Xania needed
python -m pytest                          # unit tests and the live regression scenarios (a few minutes)
```

Every bot writes a transcript of everything sent and received to `runs/<run>/<Name>.log`.

## Trials

Each trial starts a test MUD, plays, and reports. All of them take `--json` and `--no-record` (below).

| Trial | What it does |
|---|---|
| `smoke` | One bot creates a character, looks around and quits: is the build playable? |
| `hunt --mob "angry cave beast" --minutes 10` | Bots fight one kind of mob over and over until xania crashes or time runs out |
| `churn --bots 6 --minutes 15` | Bots create characters and quit in a loop, for crashes that need a quit to coincide with something periodic |
| `lobby` | A careless player presses Enter at each login prompt, and checks xania survives each one |
| `skill "magic missile" --levels 20` | Each class that learns the skill practises it and uses it in fights, at the level it's learned (and any `--levels`) |
| `area TrollsDen.are` | A careful player of each class plays the area at five levels around its range |
| `stage Hilda --level 40 --kit best` | Puts one bot on stage at a level, with a gear kit and skills, and reports what didn't go on |
| `level Hilda --goal 5 --test-mud` | One bot levels on its own for a while, survival first |

**`skill`** measures the success rate, damage against Explorer's prediction (from level 20, where the game shows
damage amounts), mana, the practice and use gains, and the messages seen. It reports a class that can't use or
practise the skill at the level it's learned, and a use that gets no reply at all.

**`area`** plays at low-5, low, median, high and high+5. The range comes from the area's header (or its mobs'
levels, without one), and median is its combat mobs' median level. At each level the classes play at once, each
for `--minutes`: they explore every room they can safely reach, fight the mobs that spawn there and are predicted
safe, loot the corpses and sell what they found. `report.md` in the run folder puts levels and classes side by
side, seen against predicted: rooms reached, kills, deaths and flees, HP lost per kill, XP per minute, gold. It
reports a careful player dying inside the area's own range, and rooms the map says can be reached that no bot
reached. `--cut-down` loads only that area plus Limbo, Midgaard, Mud School, the helps and socials (for a new area
not linked in yet). `--quick` runs one class at one level for 1.5 minutes, as a check before a long run, and
`--fail-fast` stops after a level inside the range where nobody managed a kill.

## Reading the results

**On the command line.** Each trial ends with a summary. With `--json` it prints the full result on stdout instead:
the trial, the Xania commit, measurements, findings, and each activity's outcome in the game's own words. The exit
code is **0** if nothing new turned up, **1** if a new finding did, and **2** if the trial couldn't run (no build,
the MUD wouldn't start). That makes a trial usable as a check in a script or CI:

```
python -m xania_sim --xania /path/to/xania churn --minutes 5 --json > result.json
```

**Findings.** Runs and findings are kept in `state/lore.db` (SQLite). A finding is one problem however often it
recurs: a crash is identified by its signal (or sanitizer error) and the first function in Xania's own source, and
xania's `[*****] BUG:` log lines become findings too. Each keeps every occurrence: when, which commit, the
backtrace, the bots' last transcript lines, and a way to reproduce it. A finding marked `known` (say, once it has
an issue) still shows up but no longer fails a run.

```
python -m xania_sim report                                     # latest runs and findings
python -m xania_sim findings do_get --status known --link URL  # mark one, and link its issue
python -m xania_sim status                                     # each bot's level, xp, kills, deaths, time played
```

**The dispatch** is a small website of the findings (one page each, with a stable link), the run log, and each
bot's progress beside Explorer's predictions: https://abraidotti.github.io/xania-sim/. `dispatch` builds it
locally and `publish` deploys it.

**While a trial runs**, `runs/<run>/progress.jsonl` gets a line per step (also printed to stderr), so a long run can
be followed with `tail`, and one that died shows where it stopped. xania's log and any crash reports are saved in
the same folder.

## Writing a scenario

Scenarios are pytest tests in `scenarios/`. The fixtures start one test MUD per session and make bots on it:

```python
from xania_sim import Profile

async def test_mage_starts_with_magic_missile(new_bot):
    bot = await new_bot(Profile(race="elf", cls="mage"))
    r = await bot.do("spells")
    assert r.has("magic missile")
```

`bot.do(command)` sends one command and returns its reply: `text`, `lines`, `has(regex)` and the `prompt` that
ended it (HP, mana, moves, XP, gold, wait state). Because the MUD also prompts on its own (ticks, combat rounds,
other players), pass `expect=` when it matters: `bot.do("north", expect=r"^\[Exits:|Alas")` reads past stray
prompts until the real reply arrives.

## How it works

**The test MUD.** Each run starts its own doorman and xania on their own port, with an empty data directory, from
your build. If gdb is installed, xania runs under it: gdb only watches, but when xania dies the report has the
signal, xania's last log lines and a backtrace. Builds with AddressSanitizer report their own stacks. `hunt` and
`churn` stop at the first crash, since that's what they're for; the area trial restarts the MUD after a crash
already marked `known` and carries on.

**Setting the stage.** An immortal "director" bot sets up what a test needs, so it can start where a real player
would be: it advances a character to a level, sets its skills, gives it a kit of gear (the best in the world, what
shops sell, what lies around, or nothing) and moves it into place. The bot then puts the gear on itself, and the
report says, in the game's words, what didn't fit. From there, the bots only play.

**Playing like a client.** A bot sends one command, then waits for its prompt, which it sets on login to a fixed
format, so the end of each reply is unambiguous and it never trips the input-flood kick. Everything it reads is
parsed into events (kills, deaths and their killer, XP, levels, skill gains, loot, gold) using Xania's own
messages.

**Getting around.** Routes come from Explorer's map. As a bot arrives it recognises the room by its name, the first
line of its description and its exits, plus where the move should have led (about 600 room names are shared). It
routes around locked doors and rooms its level can't enter, rests when tired, recalls when lost, and gives up on a
walk that goes round in circles.

**Fighting carefully.** The careful player only starts fights predicted to cost well under half its HP: by
Explorer's model at first, then by what bots have seen once there's enough. It skips rooms where others could join
in, fights back when something attacks it and the fight is safe (otherwise it flees), and never goes back to a room
it fled from. Mages and clerics are judged with their spells and buffs; in fights they buff first, open with their
best move and keep enough mana to escape.

**Shared lore.** The bots share what they know: what the source says about each skill and spell, and what they've
seen when using them. Each ability is fingerprinted from its skill-table entry and spell function, so a rebalanced
spell shows up as changed and its observations start over.

## What's next

Players in groups, in the area trial; more careless and awkward players (driving every command with odd
arguments); balance trials across classes and races; and comparing two commits of Xania side by side, so a change
can be checked against the version before it.

Character bots that play beside you and write their own legends are a separate project built on Xania Sim:
[Xania Companions](https://github.com/abraidotti/xania-companions).

## Platform notes

Xania Sim needs Python 3.12+ and a machine that can build and run Xania; the bots only need to reach the MUD over
telnet. If Xania runs under WSL on Windows, Xania Sim finds it there: `XANIA_SIM_DISTRO` names the distro, and the
test MUD keeps a `wsl.exe` session attached while it runs (WSL stops a distro that has none).
