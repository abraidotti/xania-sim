"""Fixtures for live scenarios: one isolated test MUD per pytest session, and a factory for bots on it.

Every bot writes its transcript to runs/<timestamp>/<Name>.log. Skip live scenarios with ``-m "not live"``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import pytest_asyncio

from xania_sim.core import world as W
from xania_sim.record.findings import Finding, bug_line_findings, crash_finding, transcript_tails
from xania_sim.core.names import bot_names, mob_words
from xania_sim.core.server import TestMud
from xania_sim.core.session import Bot, Profile

RUNS = Path(__file__).resolve().parents[1] / "runs" / time.strftime("%Y%m%d-%H%M%S")


def pytest_collection_modifyitems(items):
    for item in items:
        if "scenarios" in item.path.parts:
            item.add_marker(pytest.mark.live)


@pytest_asyncio.fixture(scope="session")
async def mud():
    m = TestMud(port=9100)
    try:
        await m.start()
    except RuntimeError as e:
        if "NO_BUILD" in str(e):
            pytest.skip(f"Xania isn't built for the test MUD: {e}")
        raise
    yield m
    FOUND.extend(bug_line_findings(m.run(f"cat {m.data_dir}/log/xania*.log 2>/dev/null", check=False),
                                   "python -m pytest scenarios"))
    await m.stop()
    _record_session()


CRASHES: list[str] = []
FOUND: list[Finding] = []  # findings this session, recorded for the dispatch when the MUD stops
OUTCOMES: dict[str, int] = {}


def pytest_runtest_logreport(report):
    if report.when == "call" or report.outcome != "passed":
        OUTCOMES[report.outcome] = OUTCOMES.get(report.outcome, 0) + 1


def _record_session() -> None:
    """The session as a run in the lore database, with its findings (XANIA_SIM_RECORD=0 to skip)."""
    if os.environ.get("XANIA_SIM_RECORD", "1") == "0":
        return
    from xania_sim.record.findings import Journal
    from xania_sim.core.repo import GameRepo
    from xania_sim.record.store import Lore

    v = GameRepo().version()
    lore = Lore()
    journal = Journal(lore.db)
    run_id = journal.start_run("tests", "Live scenarios", v.commit, v.dirty)
    for f in FOUND:
        journal.record(run_id, f, v.commit)
    journal.finish_run(run_id, **OUTCOMES, findings=len(FOUND))
    lore.close()


@pytest.fixture
def found():
    """Tests append Findings here to put them in the dispatch."""
    return FOUND


@pytest_asyncio.fixture(autouse=True)
async def crash_watch(request):
    """After each live test: if xania died, fail that test with the backtrace, record the crash as a finding, and
    restart the MUD (keeping players) so the remaining tests still run."""
    yield
    if "mud" not in request.fixturenames:
        return
    m = request.getfixturevalue("mud")
    if not m.alive():
        report = m.crash_report()
        CRASHES.append(f"{request.node.nodeid}:\n{report}")
        FOUND.append(crash_finding(report, f'python -m pytest "{request.node.nodeid}"',
                                   f"During the live scenario {request.node.name}.", transcript_tails(RUNS)))
        await m.restart()
        pytest.fail(f"xania crashed during this test:\n{report}", pytrace=False)


def pytest_terminal_summary(terminalreporter):
    for c in CRASHES:
        terminalreporter.write_line(f"XANIA CRASH in {c}", red=True)


@pytest.fixture(scope="session")
def world():
    return W.load()


@pytest.fixture(scope="session")
def names(world):
    """An endless supply of unused, acceptable bot names."""
    pool = iter(bot_names("simbot", 500, mob_words(world)))
    return lambda: next(pool)


@pytest_asyncio.fixture
async def new_bot(mud, names):
    """``bot = await new_bot(Profile(...))``: a freshly created character, quit at the end of the test."""
    bots = []

    async def make(profile: Profile = Profile(), name: str | None = None) -> Bot:
        bot = Bot(name or names(), "simpass1", host=mud.host, port=mud.port, transcript_dir=RUNS)
        await bot.enter(profile)
        bots.append(bot)
        return bot

    yield make
    for b in bots:
        await b.quit()
