# Xania Sim

Bots that play [Xania](https://github.com/mattgodbolt/xania) over telnet to test it. Xania Sim simulates
players, good and bad: anything a real person could do through a MUD client.

- **Finding bugs**: bots play the real game and do what players do, so they run into crashes and oddities that
  nobody would find by reading code. A crash comes back as a report with a source-level backtrace.
- **Testing features**: scripted scenarios that check new skills, spells, areas and systems on the real game.
- **Measuring the game**: bots level, fight and survive, and their results sit beside Xania Explorer's predictions.

An immortal "director" only sets the stage (levels, skills, gear) so a test can start where a real player would be;
from there the bots just play. Bots never touch your real game: tests run on their own isolated MUD.

Companion bots (characters who play beside you and write their own legends) used to live here and are now their
own project, [Xania Companions](https://github.com/abraidotti/xania-companions), built on Xania Sim.

It works alongside [Xania Explorer](../xania-explorer): Explorer reads the area files and C++ tables and predicts
what should happen; the bots check what does happen. xania-sim imports Explorer's API (`xania_explorer.api`) for the
map, names and predictions. Explorer never imports xania-sim.

## Setup

1. Xania built from source: a checkout (by default `~/xania`) with `make` run in it. See Xania's README.
2. Install this package and Explorer from the sibling checkout:

```
python -m venv .venv
source .venv/bin/activate         # or .venv\Scripts\activate on Windows
python -m pip install -e ".[dev]" -e ../xania-explorer    # with Explorer checked out next to this repo
python -m pip install -e ".[dev,explorer]"                # or: Explorer straight from GitHub
```

Settings (environment variables): `XANIA_SIM_XANIA`, the Xania checkout (default `$HOME/xania`).

## Try it

```
python -m xania_sim smoke                 # starts a test MUD on port 9100, creates a bot, looks, quits
python -m xania_sim smoke --port 9000 --existing   # the same against a MUD you already have running
python -m pytest                          # unit tests + live scenarios (1-3 min; the MUD takes ~12 s to boot)
python -m pytest -m "not live"            # unit tests only (a fake MUD, no Xania needed)
python -m pytest -m "not slow"            # skip scenarios that wait for regen ticks
```

Every bot writes a full transcript (`<` received, `>` sent) to `runs/<timestamp>/<Name>.log`.

## Finding bugs

The test MUD runs xania under gdb when gdb is installed where Xania runs, so if xania crashes, the
report shows the signal, the last lines xania logged, and a backtrace with source files and line numbers. gdb only
watches: no breakpoints, signals pass straight through. `XANIA_SIM_GDB=0` turns it off.

- **During tests**: a crash fails the test that caused it, with the backtrace, and the MUD restarts (players kept)
  so the rest of the run carries on.
- **On purpose**: `hunt` has bots repeat one risky activity until xania crashes or time runs out, then saves the
  report:

```
python -m xania_sim hunt --mob "angry cave beast" --bots 4 --minutes 10
# 4 fights in 75 s ... XANIA CRASHED. Report with backtrace: runs/hunt-.../xania-crash.txt
```

What the first hunt found: any character with autoloot (on for new players) who kills a mob with something on it
crashes debug builds of xania. Autoloot runs `get all corpse` (`loot_and_sacrifice_corpse`, src/fight.cpp:565),
and `do_get` (src/act_obj.cpp:707) passes `arg1[3]` where `arg1` is the 3-character `"all"`: C-era code reading a
string terminator that a `std::string_view` doesn't have. Typing `get all` in an empty room does the same (line
648). Release builds don't crash, but read the next character instead, so the guard against looting undroppable
items with `get all` never applies. Filed as [abraidotti/xania#1](https://github.com/abraidotti/xania/issues/1);
bots turn autoloot off until it's fixed, and `scenarios/test_live_bugs.py` keeps checking it (it fails, telling you
to retire it, once the crash is gone).

`python -m xania_sim churn --bots 6 --minutes 15` has bots create characters and quit in a loop until xania
crashes: it reproduces crashes that need a quit to coincide with something periodic (it found
[abraidotti/xania#2](https://github.com/abraidotti/xania/issues/2), a use-after-free when a quit lands on a tick).

## Reading and sharing what runs found: the dispatch

Hunts and live test sessions record themselves (runs and findings) in the lore database. A finding is one problem
however often it's seen: a crash is identified by its signal and the first function in Xania's own source, so the
`get all` and autoloot triggers above are one finding with two kinds of occurrence. xania's own `[*****] BUG:` log
lines become findings too. Each keeps every occurrence (when, which game commit, backtrace, the bots' last
transcript lines), a way to reproduce it, a status and an optional issue link.

```
python -m xania_sim report                          # latest runs and findings in the terminal
python -m xania_sim findings do_get --link https://github.com/abraidotti/xania/issues/1
python -m xania_sim findings do_get --status known  # new | known | ignored (new ones lead the headlines)
python -m xania_sim dispatch                        # build the website locally (build/dispatch/index.html)
python -m xania_sim publish                         # push it to the dispatch branch and deploy to GitHub Pages
```

The published dispatch is at **https://abraidotti.github.io/xania-sim/**: headlines, a card per finding (each
with its own page, a stable link to share), and the run log. `publish` commits the site to the `dispatch` branch
(never to `main`) and runs the [Dispatch workflow](.github/workflows/dispatch.yml), which deploys it. Only test
MUD runs are recorded, and home directories in backtraces are shortened to `~/`.

## Shared lore

```
python -m xania_sim lore                  # scan the game repo's skills/spells into the shared lore; show what changed
python -m xania_sim lore fireball --level 25   # one ability's facts, fingerprint and status for a level band
```

The lore is shared by all bots: what the source says about each skill and spell, plus what bots observe when they
use them. Each ability is fingerprinted (its skill-table entry and spell function, comments and spacing ignored),
so a new or rebalanced spell shows up as new or changed and only its observations start over. Bots claim an
ability and level band before experimenting, so the work isn't duplicated.

## Writing a scenario

Scenarios are pytest tests in `scenarios/`. The fixtures start one test MUD per session and make bots on it:

```python
from xania_sim import Profile

async def test_mage_starts_with_magic_missile(new_bot):
    bot = await new_bot(Profile(race="elf", cls="mage"))
    r = await bot.do("spells")
    assert r.has("magic missile")
```

- `bot.do(command)` sends one command and returns a `Response` with `text`, `lines`, `has(regex)` and the
  `prompt` that ended it (hp, mana, moves with maxima, xp, gold and wait state). Long output is paged through.
- The rule behind it: one command at a time, then wait for the prompt. Bots set the prompt `<bot ...>` when they
  log in, so the end of each command's output is unambiguous. Sending faster would trip Xania's input-flood kick.
  The MUD also prompts on its own (regen ticks, combat rounds, other players), so pass `expect=` (a regex the
  reply must contain) when it matters: `bot.do("north", expect=r"^\[Exits:|Alas")` reads past stray prompts
  until the real reply arrives, and returns `complete=False` if it never does.
- Getting around: `Navigator(bot, world, avoid=danger_rooms(world, level))` gives `locate()`, `walk_to(vnum)` and
  `explore(area)`. Routes come from Explorer's map; rooms are recognised as the bot arrives (name, first line of
  description, exits, plus where the move should have led, since about 600 room names are shared); locked doors and
  other refusals are routed around; tiredness means a rest; a lost bot recalls to the temple.
- Names: the MUD refuses names that match a mob keyword. The `names` fixture (and `xania_sim.names.bot_names`)
  hands out acceptable ones.

## Levelling bots

```
python -m xania_sim level Hilda --goal 5                     # a knight that levels on its own, survival first
python -m xania_sim level Hilda --test-mud --port 9100       # the same on an isolated test MUD
```

Bots log in with the password `botpass1` unless you pass `--password`; a new name creates the character.

Levelling is survival first: a bot only starts fights predicted (by Explorer, then by the shared fight log once bots
have seen enough) to cost well under half its HP, skips rooms where others could join in, flees early (standing up
first if it's been tripped or bashed), rests at the temple, and never goes back to a room it fled from or died in.
If something attacks it while it rests it stops and escapes (praying to recall if it has no moves left to run),
and one death to a mob takes that mob off the list for its level band. At the start and after every level it
walks to the nearest guildmaster and spends its practice sessions on its class's priorities (`activities/practice.py`).

## Progress: what each bot has done

```
python -m xania_sim status                # every bot: level, xp, gold, kills, deaths, flees, hours played
python -m xania_sim status lvlknia        # one bot's areas and recent events too
```

Everything a bot reads is parsed into events (kills, deaths with their killer, xp, levels, skill gains, loot, gold,
tells, says, channels, follows, groups; the messages are Xania's own, with file:line in `record/events.py`) and stored
with the room and area it happened in, next to prompt snapshots (HP, mana, moves, xp, gold) taken as they change.
The dispatch gets a bots table and a page per bot: level over time, where it fought, what it did lately, and its
fights beside Explorer's predictions (`fight.compare`). The tables (`bots`, `events`, `snapshots`, `fights`) are
plain SQLite in `state/lore.db`, for other tools to read.

## How it fits together

Xania Sim is in layers. Activities are what a player does; players choose activities; trials combine them into
tests. Other tools (Xania Builder, Xania Companions) build on the activities.

| Layer | Module | What it does |
|---|---|---|
| core | `telnet.py` | Connection to doorman: refuses telnet options, strips ANSI, `expect(pattern)` / `send(line)`, transcript |
| core | `session.py` | `Bot`: the lobby (create or log in, as in `nanny`, xania/src/MudImpl.cpp), then `do(command)` |
| core | `parsers.py` | Pure parsing (the bot prompt, lines); tested against captured output |
| core | `server.py` | `TestMud`: its own doorman + xania on its own port with a throwaway data dir (`/tmp/xania-sim/<port>`) |
| core | `director.py` | An immortal on the test MUD that sets the stage: levels, skills, objects, moving players |
| core | `names.py` | Names the MUD accepts (`validate_player_name`) |
| core | `world.py` | Explorer's world, loaded from the checkout the test MUD runs |
| core | `repo.py` | `GameRepo`: the game's git repo (`XANIA_SIM_XANIA`): commit, dirty state, is the build older than the source |
| activities | `move.py` | `Navigator`: where am I, safe routes (`danger_rooms` from aggressive spawns + `predict_fight`), walking, exploring |
| activities | `fight.py` | Recognising mobs in a room, `safe_targets`, `fight()` round by round, `recover()`, the shared `FightLog`, `compare` |
| activities | `practice.py` | Practice: reads the practice table, picks skills by class priority, finds a practice mob |
| activities | `tactics.py` | Skills and spells in fights: buffs first, an opener, the best action each round, a mana reserve kept for escape |
| players | `careful.py` | `Leveller`: the survival-first player: go where the safe fights are, fight one at a time, rest, flee |
| players | `run.py` | The `level` command: one careful player for a while, recorded in the event log |
| record | `events.py` | `parse(text)` into events, `Recorder` (watches a bot, stores events + snapshots), `Progress` (reads them) |
| record | `findings.py` | Runs and findings (crashes with gdb or AddressSanitizer stacks), deduplicated by signature |
| record | `rooms.py` | Parsing room views (`room_look`, act_info.cpp) and recognising rooms by fingerprint |
| record | `scan.py` | Skills/spells from the repo's source (levels via Explorer; entry text, target, position read here) + fingerprints |
| record | `store.py` | `Lore`: shared SQLite (`state/lore.db`): abilities and their changes, raw observations, claims, status per band |
| trials | `churn.py`, `hunt.py` | Stress: bots create and quit, or fight one mob, until xania crashes |
| report | `dispatch.py` | The dispatch website: findings, runs, bots, predicted vs seen |

| setup | `stage.py` | Puts a bot on stage: level, skills, a gear kit to wear; reports what went on in the game's own words |
| setup | `kits.py` | Gear kits per level, class, alignment and race: `best`, `best-ac`, `best-hitdam`, `shop`, `floor`, `bare` |
| setup | `loadouts.py` | Skill loadouts: `none`, `passives`, `class`, `natural`, or a TOML file; which skills a class can use when |

`scenarios/` holds the live regression suite.

## Roadmap

Scope: Xania Sim simulates players, good and bad, through the client. Testing code directly (unit tests, fuzzing
functions, checking files) belongs to Xania Builder, which will use Sim's scenarios to check its own changes.

- **M1 (done)**: telnet, lobby, prompt, isolated test MUD, smoke scenarios (now the regression suite).
- **M2 (done)**: navigation with Explorer's map (`locate`, `walk_to`, `explore(area)`), danger-aware.
- **M3a (done)**: the game repo (version, build check), skill/spell scan with fingerprints, shared lore (SQLite).
- **M3 (done)**: survival and levelling: pick safe fights with `api.predict_fight`, rest, flee, recall.
- **M4 (done)**: event log (SQLite), `xania_sim status`, progress pages on the dispatch beside Explorer's predictions.
- **M5-M8**: companionship, personas, legends and the roster runner, now [Xania Companions](https://github.com/abraidotti/xania-companions).
- **Next**: the director and staging (bots at any level with chosen gear and skills), combat tactics; careless,
  abusive and exploiting players (fuzzing commands, flooding, exploit hunts); balance trials; area testing;
  `--json` output and exit codes so Builder can run Sim in a pipeline.

## Platform notes

Xania Sim only needs Python 3.12+ and a machine that can build and run Xania; bots talk to the MUD over telnet,
so they can run anywhere that reaches it. If Xania runs under WSL on Windows, the sim finds it there:
`XANIA_SIM_DISTRO` names the distro, and the test MUD keeps a `wsl.exe` session attached
while it runs (WSL stops a distro that has none).
