<a id="readme-top"></a>

```text
  /    \    /    \
  \     \  /     /
   \     \/     /
    \          /  A N I A Sim
    /    /\    \
   /    /  \    \
  /    /    \    \
  \   /      \   /
```

[![License][license-shield]][license-url]
[![Python][python-shield]][python-url]

<br />
<div align="center">
  <h3 align="center">Xania Sim</h3>

  <p align="center">
    Bots that test Xania by playing it, the way players do.
  </p>

  <h2 align="center">
    <a href="https://abraidotti.github.io/xania-sim/">abraidotti.github.io/xania-sim »</a>
  </h2>

  <p align="center">
    The dispatch: what the bots have found, each finding on its own page, the run log and the bots' progress.
    <br />
    <br />
    <a href="docs/trials.md">The trials</a>
    &middot;
    <a href="docs/how-it-works.md">How it works</a>
    &middot;
    <a href="https://github.com/abraidotti/xania-explorer">Xania Explorer</a>
  </p>
</div>

<details>
  <summary>Table of Contents</summary>
  <ol>
    <li>
      <a href="#about-the-project">About The Project</a>
      <ul>
        <li><a href="#built-with">Built With</a></li>
      </ul>
    </li>
    <li>
      <a href="#getting-started">Getting Started</a>
      <ul>
        <li><a href="#prerequisites">Prerequisites</a></li>
        <li><a href="#installation">Installation</a></li>
      </ul>
    </li>
    <li><a href="#usage">Usage</a></li>
    <li><a href="#roadmap">Roadmap</a></li>
    <li><a href="#license">License</a></li>
    <li><a href="#acknowledgments">Acknowledgments</a></li>
  </ol>
</details>

## About The Project

Xania Sim tests [Xania](https://github.com/mattgodbolt/xania) by playing it. Its bots connect over telnet, like any
MUD client, and do what players do: create characters, explore, fight, loot, shop, practise, level and quit,
carefully or carelessly. They report what goes wrong: a crash with a source-level backtrace, or an odd result with
the game's own words as evidence.

* **Finding bugs** that only show up in play.
* **Testing features**: a skill, a spell or an area, at the levels it's meant for, with each class.
* **Measuring the game** against [Xania Explorer](https://github.com/abraidotti/xania-explorer)'s predictions:
  Explorer works out from the area files and C++ what should happen, and the bots see what does.

Every run uses its own isolated test MUD, started from your build with a throwaway data directory, so it never
touches a game people are playing on.

### Built With

* Python 3.12 with asyncio, talking telnet to doorman
* [Xania Explorer](https://github.com/abraidotti/xania-explorer) for the map and the predictions
* gdb and AddressSanitizer for crash reports
* SQLite for findings and the bots' history; pytest for the scenarios

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Getting Started

### Prerequisites

* Python 3.12 or later
* A Xania checkout built with `make`, by default at `~/xania` (set `XANIA_SIM_XANIA` for another)
* gdb, optionally, for backtraces when xania crashes

### Installation

1. Clone this repo:
   ```sh
   git clone https://github.com/abraidotti/xania-sim.git
   cd xania-sim
   ```
2. Install it, with Xania Explorer:
   ```sh
   python -m venv .venv
   source .venv/bin/activate          # .venv\Scripts\activate on Windows
   python -m pip install -e ".[dev,explorer]"
   ```
3. Check it works: a bot creates a character on a test MUD, looks around and quits.
   ```sh
   python -m xania_sim smoke
   ```

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Usage

Run a trial:

```sh
python -m xania_sim area TrollsDen.are          # careful players of each class around the area's level range
python -m xania_sim skill "magic missile"       # each class that learns it practises it and uses it in fights
python -m xania_sim lobby                       # an empty reply at each login prompt
python -m xania_sim churn --minutes 15          # characters created and quit in a loop
```

See what they found:

```sh
python -m xania_sim report                      # the latest runs and findings
```

With `--json`, a trial prints its full result and exits 0 (nothing new), 1 (a new finding) or 2 (couldn't run),
so it can run as a check in a script.

More:

* [Trials](docs/trials.md): every trial and its options
* [Reading the results](docs/results.md): JSON and exit codes, findings, the dispatch, run folders
* [Writing a scenario](docs/scenarios.md): pytest tests with bots
* [How it works](docs/how-it-works.md): the test MUD, the director, getting around, fighting carefully

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Roadmap

- [x] An isolated test MUD, with crash reports from gdb and AddressSanitizer
- [x] Bots that get around with Explorer's map, fight safely and level
- [x] A director that stages bots at any level, with gear and skills
- [x] Trials for crashes, skills and spells, areas and the login flow
- [x] Results as JSON with exit codes, findings, the dispatch
- [ ] Players in groups, in the area trial
- [ ] More careless and awkward players: every command with odd arguments
- [ ] Balance trials across classes and races
- [ ] Two commits of Xania side by side, to check a change against the version before it

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## License

Distributed under the MIT License. See [`LICENSE`](LICENSE).

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Acknowledgments

* [Xania](https://github.com/mattgodbolt/xania) and its developers
* [Xania Companions](https://github.com/abraidotti/xania-companions), character bots that play beside you, built
  on Xania Sim
* [Best-README-Template](https://github.com/othneildrew/Best-README-Template), which this README follows

<p align="right">(<a href="#readme-top">back to top</a>)</p>

[license-shield]: https://img.shields.io/github/license/abraidotti/xania-sim?style=for-the-badge
[license-url]: LICENSE
[python-shield]: https://img.shields.io/badge/python-3.12%2B-blue?style=for-the-badge
[python-url]: https://www.python.org/
