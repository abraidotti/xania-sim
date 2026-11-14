"""The watchdog (record/watchdog.py): a blocked event loop gets a stack dump and then an exit; a quiet but running
one gets its tasks' stacks; a healthy one gets nothing. And the two teardown fixes found with it."""

import asyncio
import time

import pytest

from xania_sim.core import server as S
from xania_sim.core import telnet
from xania_sim.record import watchdog as W


def _dog(tmp_path, **kw):
    exits = []
    kw = {"blocked_after": 0.3, "exit_after": None, "quiet_after": None, "every": 0.05} | kw
    dog = W.Watchdog(asyncio.get_running_loop(), tmp_path, "test", exit=exits.append, **kw)
    return dog, exits


def _wait_for_records(dog, n: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while len(dog.records) < n and time.monotonic() < deadline:
        time.sleep(0.02)


def blocking_the_loop(seconds: float) -> None:
    time.sleep(seconds)  # a stand-in for whatever froze the skill trial: sync code on the loop


async def test_a_blocked_loop_gets_every_threads_stack_then_an_exit(tmp_path):
    dog, exits = _dog(tmp_path, exit_after=0.8)
    dog.start()
    try:
        blocking_the_loop(1.3)
        _wait_for_records(dog, 2)
    finally:
        dog.stop()
    assert dog.records[:2] == ["blocked", "exit"]
    assert exits == [W.EXIT_CODE]
    text = dog.path.read_text(encoding="utf-8")
    assert "blocked: the event loop hasn't run" in text
    assert "blocking_the_loop" in text  # the frame the loop is stuck in, from faulthandler
    assert '"what": "blocked"' in (tmp_path / "progress.jsonl").read_text(encoding="utf-8")


async def test_blocked_is_recorded_once_per_episode(tmp_path):
    dog, _ = _dog(tmp_path)
    dog.start()
    try:
        blocking_the_loop(0.6)
        await asyncio.sleep(0.3)  # the loop runs again: re-armed
        blocking_the_loop(0.6)
        await asyncio.sleep(0.2)
    finally:
        dog.stop()
    assert dog.records == ["blocked", "blocked"]


async def test_quiet_traffic_on_a_running_loop_gets_the_tasks_stacks(tmp_path, monkeypatch):
    monkeypatch.setattr(telnet, "last_io", time.monotonic() - 100)
    dog, exits = _dog(tmp_path, quiet_after=1.0)
    dog.start()
    try:
        await asyncio.sleep(0.5)
    finally:
        dog.stop()
    assert dog.records == ["quiet"] and not exits
    text = dog.path.read_text(encoding="utf-8")
    assert "no MUD traffic" in text
    assert "test_quiet_traffic_on_a_running_loop" in text  # this test's own task, mid-await


async def test_a_healthy_loop_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(telnet, "last_io", time.monotonic())
    dog, exits = _dog(tmp_path, exit_after=0.5, quiet_after=5)
    dog.start()
    try:
        for _ in range(10):
            await asyncio.sleep(0.05)
    finally:
        dog.stop()
    assert dog.records == [] and not exits and not dog.path.exists()


async def test_adopt_does_nothing_unless_armed(tmp_path):
    W.disarm()
    W.adopt(tmp_path, "x")  # tests aren't watched: no thread, no error
    assert W._armed is None


async def test_close_gives_up_on_a_peer_that_never_confirms(monkeypatch):
    class StuckWriter:
        def close(self):
            pass

        async def wait_closed(self):
            await asyncio.Event().wait()  # never

    monkeypatch.setattr(telnet, "CLOSE_TIMEOUT", 0.2)
    conn = telnet.Connection(None, StuckWriter())
    t0 = time.monotonic()
    await asyncio.wait_for(conn.close(), 2)
    assert conn.closed and time.monotonic() - t0 < 1


class _Mud(S.TestMud):
    """A TestMud whose WSL side is scripted: what leftovers it finds, and whether the port answers."""

    def __init__(self, leftovers: list[tuple[str, str]], answers: list[bool]):
        super().__init__(port=9999)
        self.leftovers, self.answering, self.scripts = leftovers, answers, []

    def run(self, script: str, check: bool = True) -> str:
        self.scripts.append(script)
        return ""

    def _leftover_pids(self):
        return self.leftovers

    async def _answers(self):
        return self.answering.pop(0) if len(self.answering) > 1 else self.answering[0]


async def test_leftovers_on_the_port_are_stopped():
    mud = _Mud([("965", "doorman"), ("977", "xania")], [True, False])
    assert await mud._reap_leftovers(timeout=2) == [("965", "doorman"), ("977", "xania")]
    kill = mud.scripts[-1]
    assert "kill -TERM $p" in kill and "for p in 965 977;" in kill and "kill -KILL" in kill


async def test_nothing_is_stopped_when_nothing_was_left():
    mud = _Mud([], [True])
    assert await mud._reap_leftovers(timeout=2) == []
    assert mud.scripts == []


async def test_start_still_refuses_a_port_someone_else_holds():
    mud = _Mud([], [True])
    with pytest.raises(RuntimeError, match="already listening on port 9999"):
        await mud.start()


def test_the_leftover_finder_matches_the_port_and_reports_parent_and_owner():
    script = S._LEFTOVERS.format(port=9100)
    assert "grep -qx 'MUD_PORT=9100'" in script and "XANIA_SIM_OWNER=" in script
    assert '"bash -s "*) parent=script' in script and "parent=gdb" in script and "parent=other" in script


def test_a_dead_owners_mud_is_a_leftover_with_its_start_script():
    rows = [("712", "doorman", "707", "script", "4242"), ("714", "gdb", "707", "script", "4242"),
            ("724", "xania", "714", "gdb", "4242")]
    assert S.leftovers(rows, alive=lambda pid: False) == [("712", "doorman"), ("707", "bash"), ("714", "gdb")]


def test_a_live_owners_mud_is_left_alone():
    rows = [("712", "doorman", "707", "script", "4242"), ("724", "xania", "714", "gdb", "4242")]
    assert S.leftovers(rows, alive=lambda pid: True) == []


def test_processes_cut_off_from_their_script_are_leftovers_whoever_owns_them():
    rows = [("965", "doorman", "1", "other", "-"), ("966", "doorman", "1", "other", "4242")]
    assert S.leftovers(rows, alive=lambda pid: True) == [("965", "doorman"), ("966", "doorman")]


def test_a_mud_with_no_recorded_owner_under_its_script_is_left_alone():
    assert S.leftovers([("712", "doorman", "707", "script", "-")], alive=lambda pid: False) == []


def test_owner_alive_tells_a_running_process_from_a_finished_one():
    import os
    import subprocess
    import sys

    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    assert S.owner_alive(os.getpid())
    assert not S.owner_alive(int(done.stdout))
