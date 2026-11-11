# Reading the results

## On the command line

Each trial ends with a summary. With `--json` it prints the full result on stdout instead (the summary goes to
stderr): the trial, the Xania commit, its measurements, its findings, and each activity's outcome in the game's
own words. The schema is in `xania_sim/record/result.py`.

The exit code says how it went:

| Code | Meaning |
|---|---|
| 0 | Nothing new turned up |
| 1 | A new finding turned up |
| 2 | The trial couldn't run (no build, the MUD wouldn't start, staging failed) |

So a trial works as a check in a script or CI:

```sh
python -m xania_sim --xania /path/to/xania churn --minutes 5 --json > result.json
```

`--no-record` keeps the run out of the database below (it still reads which findings are already known).

## Findings

Runs and findings are kept in `state/lore.db`, plain SQLite. A finding is one problem however often it recurs: a
crash is identified by its signal (or sanitizer error) and the first function in Xania's own source, and xania's
`[*****] BUG:` log lines become findings too. Each keeps every occurrence: when, which commit, the backtrace, the
bots' last transcript lines, and a way to reproduce it.

A finding is `new`, `known` or `ignored`. A known one (say, once it has an issue) is still listed but no longer
fails a run.

```sh
python -m xania_sim report                                     # latest runs and findings
python -m xania_sim findings do_get --status known --link URL  # mark one, and link its issue
```

## The dispatch

The dispatch is a small website of the findings (each with its own page and a stable link), the run log, and each
bot's progress beside Explorer's predictions. It's published at https://abraidotti.github.io/xania-sim/.

```sh
python -m xania_sim dispatch     # build it locally, in build/dispatch/
python -m xania_sim publish      # deploy it to GitHub Pages
```

## Bots' progress

Everything a bot reads is parsed into events (kills, deaths and their killer, XP, levels, skill gains, loot, gold)
and stored with the room and area it happened in.

```sh
python -m xania_sim status         # every bot's level, XP, gold, kills, deaths, flees, hours played
python -m xania_sim status Hilda   # one bot's areas and recent events too
```

## Files from a run

Each run has a folder in `runs/` with:

* a transcript per bot, everything sent (`>`) and received (`<`);
* `progress.jsonl`, a line per step as the trial went;
* xania's log, and a crash report for each crash;
* for the area trial, `report.md`.
