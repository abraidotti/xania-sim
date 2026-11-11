# Trials

Each trial starts its own isolated test MUD from your Xania build, plays, and reports. All of them take `--json`
and `--no-record` (see [Reading the results](results.md)), and `--xania PATH` before the trial name points at
another checkout.

| Trial | What it does |
|---|---|
| `smoke` | One bot creates a character, looks around and quits: is the build playable? |
| `hunt --mob "angry cave beast" --minutes 10` | Bots fight one kind of mob over and over until xania crashes or time runs out |
| `churn --bots 6 --minutes 15` | Bots create characters and quit in a loop, for crashes that need a quit to coincide with something periodic |
| `lobby` | A careless player presses Enter at each login prompt, and checks xania survives each one |
| `skill "magic missile" --levels 20` | Each class that learns the skill practises it and uses it in fights |
| `area TrollsDen.are` | A careful player of each class plays the area at five levels around its range |
| `stage Hilda --level 40 --kit best` | Puts one bot on stage at a level, with a gear kit and skills, and reports what didn't go on |
| `level Hilda --goal 5 --test-mud` | One bot levels on its own for a while, survival first |

`hunt` and `churn` stop at the first crash, which is what they're for. The report, with its backtrace, is saved in
the run folder.

## skill

For each class that learns the skill (by Explorer's class tables), at the level it's learned and any `--levels`,
a bot is staged with shop gear and its class's skills. It practises the skill from 1%, then uses it at 75%: in
fights against a mob it's predicted to beat, with its tactics limited to that one skill, or on itself if the skill
isn't for fights.

It measures the success rate, the damage against Explorer's prediction (from level 20, where the game shows damage
amounts), mana, the gains from practice and from use, and the messages seen. It reports a class that can't use or
practise the skill at the level it's learned, and a use that gets no reply at all.

## area

The area is played at five levels: low-5, low, median, high and high+5. The range comes from the area's header, or
from its mobs' levels when it has none, and median is its combat mobs' median level. `--levels` picks others.

At each level, one careful player per class (shop gear, the skills a player would have by then; `--kit` and
`--classes` to change) starts at the area's entrance and plays for `--minutes`, all classes at once. Each explores
every room it can safely reach, fights the mobs that spawn there and are predicted safe, loots the corpses by hand
and sells what it found at a shop in the area, or the nearest one.

`report.md` in the run folder puts the levels and classes side by side, seen against predicted:

* rooms reached;
* kills, deaths and flees, against the number of mobs predicted safe;
* HP lost per kill;
* XP per minute;
* gold.

It reports a careful player who dies at a level inside the area's own range, and rooms the map says can be
reached that no bot reached. Each level also gets a one-line verdict: `ok: 5 kills`, or `unproductive: no kills;
stopped: safe targets only past dangerous rooms`.

Options:

* `--cut-down` loads only the area plus Limbo, Midgaard, Mud School, the helps and socials, through a temporary
  `area.lst`: for a new area that isn't linked in yet, or to keep other areas out of a test.
* `--quick` runs one class at the first level for 1.5 minutes, to check everything works before a long run.
* `--fail-fast` stops after a level inside the area's range where nobody managed a kill.

## How long trials keep to time

* Each bot's session ends at its time plus 90 seconds, whatever it's doing; a wait for moves never takes more than
  half of it.
* A walk that keeps going round in circles gives up, and says where ("looping at room 3001").
* xania is checked every few seconds while bots play. In the area trial, a crash already marked `known` restarts
  the MUD and the trial carries on.
* `runs/<run>/progress.jsonl` gets a line per step, so a long run can be followed with `tail`.
