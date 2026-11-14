"""A test MUD left running by a run that died (killed, or ended by the watchdog) doesn't hold its port against the
next run: TestMud.start() stops it first (core/server.py ``_reap_leftovers``). One another run is still using is
never touched."""

import subprocess
import sys

import pytest

from xania_sim.core.server import TestMud

PORT = 9101  # not the session's 9100


def _finished_pid() -> int:
    """The pid of a Python process that has already exited: the owner of a dead run's MUD."""
    r = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    return int(r.stdout)


async def test_a_new_test_mud_replaces_what_a_dead_run_left_behind():
    left = TestMud(port=PORT)
    left.owner = _finished_pid()  # as if the run that started it had been killed: its MUD stays up
    await left.start()
    try:
        assert {n for _, n in left._leftover_pids()} >= {"doorman", "bash"}
        fresh = TestMud(port=PORT)
        await fresh.start()  # before: "already listening", or a doorman that couldn't bind, talking to the old one
        try:
            assert fresh.alive() and await fresh._answers()
            assert fresh._leftover_pids() == []
            assert "Unable to bind" not in fresh.log("doorman")
        finally:
            await fresh.stop()
        assert not await fresh._answers()
    finally:
        await left.stop()  # nothing should be left by now; still, never leave a server behind


async def test_a_test_mud_still_in_use_is_left_alone():
    busy = TestMud(port=PORT)
    await busy.start()
    try:
        assert busy._leftover_pids() == []
        with pytest.raises(RuntimeError, match=f"already listening on port {PORT}"):
            await TestMud(port=PORT).start()
        assert busy.alive() and await busy._answers()
    finally:
        await busy.stop()
