"""Crash tracing: under gdb, a xania crash leaves a backtrace, and the test MUD restarts with its players."""

import asyncio

import pytest

from xania_sim.core.server import BACKTRACE_MARKER
from xania_sim.core.session import Bot, Profile


async def test_a_crash_leaves_a_backtrace_and_the_mud_comes_back(mud, names):
    if "gdb" not in mud.run(f"ps -o comm= -p $(cat {mud.data_dir}/xania.pid)", check=False):
        pytest.skip("xania isn't running under gdb (XANIA_SIM_GDB=0 or gdb not installed)")
    name = names()
    bot = Bot(name, "simpass1", host=mud.host, port=mud.port)
    await bot.enter(Profile())
    await bot.quit()

    mud.run(f"pkill -SEGV -P $(cat {mud.data_dir}/xania.pid)")  # crash xania (gdb's child) on purpose
    for _ in range(40):
        if not mud.alive():
            break
        await asyncio.sleep(0.25)
    assert not mud.alive() and mud.crashed()
    report = mud.crash_report()
    assert "SIGSEGV" in report and BACKTRACE_MARKER in report and "#0 " in report

    await mud.restart()
    again = Bot(name, "simpass1", host=mud.host, port=mud.port)
    await again.enter()  # players survive the restart
    assert not again.created
    await again.quit()
