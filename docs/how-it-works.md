# How it works

## The test MUD

Each run starts its own doorman and xania on their own port, from your build, with an empty data directory. If gdb
is installed, xania runs under it: gdb only watches, but when xania dies the report has the signal, xania's last
log lines and a backtrace. Builds with AddressSanitizer report their own stacks.

A run that dies before it can stop its server (killed, or ended by the watchdog) leaves that server behind: on
Windows its `wsl.exe`, start script, doorman and xania all outlive the Python process. Left alone, it holds the
port, and the next run's doorman can't bind it. Each MUD's processes record the Python process they belong to
(`XANIA_SIM_OWNER`), and the next run on the same port stops any whose owner has gone. A MUD another run is still
using is never touched, and a port held by anything else is still refused. If the doorman can't bind its port
anyway, the run stops with that error, rather than carrying on behind someone else's doorman.

## The watchdog

A trial's time limits run on its event loop, so they can't fire if the loop itself stops. A thread outside the
loop watches it during command-line trials (area, lobby, skill, weapon). If the loop doesn't run for 2 minutes,
every thread's stack goes to `stall.txt` in the run folder; if it's still stuck at 10 minutes, the trial exits
with code 2 (couldn't run). When the loop runs but no MUD traffic has passed for 5 minutes, the asyncio tasks'
stacks go there too, as a record only. `XANIA_SIM_STALL_EXIT` sets the exit time in seconds (`0`: never), and
`XANIA_SIM_WATCHDOG=0` turns the watchdog off.

## Setting the stage

An immortal "director" bot sets up what a test needs, so it can start where a real player would be. It advances a
character to a level, sets its skills, gives it a kit of gear and moves it into place. The kit is one of: the best
in the world for that level, what shops sell, what lies around, or nothing. The bot puts the gear on itself, and
the report says, in the game's words, what didn't fit. From there, the bots only play.

## Playing like a client

A bot sends one command, then waits for its prompt. It sets the prompt to a fixed format when it logs in, so the
end of each reply is unambiguous and it never trips the input-flood kick. Everything it reads is parsed into
events (kills, deaths and their killer, XP, levels, skill gains, loot, gold) using Xania's own messages.

## Getting around

Routes come from [Xania Explorer](https://github.com/abraidotti/xania-explorer)'s map. As a bot arrives it
recognises the room by its name, the first line of its description and its exits, plus where the move should have
led (about 600 room names are shared). It routes around locked doors and rooms its level can't enter, rests when
tired, recalls when lost, and gives up on a walk that goes round in circles.

## Fighting carefully

The careful player only starts fights predicted to cost well under half its HP: by Explorer's model at first, then
by what bots have seen once there's enough. It skips rooms where others could join in, and fights back when
something attacks it if that fight is safe; otherwise it flees, and never goes back to that room.

Mages and clerics are judged with their spells and buffs. In fights they buff first, open with their best move,
pick the spell that does most damage to that mob (fire against trolls), and keep enough mana to escape.

## Shared lore

The bots share what they know: what the source says about each skill and spell, and what they've seen when using
them. Each ability is fingerprinted from its skill-table entry and spell function, so a rebalanced spell shows up
as changed and its observations start over.

## Running on Windows

If Xania runs under WSL, Xania Sim finds it there: `XANIA_SIM_DISTRO` names the distro, and the test MUD keeps a
`wsl.exe` session attached while it runs, since WSL stops a distro that has none.
