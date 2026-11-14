"""A watchdog outside the event loop: it notices a trial that has stopped moving, says where, and ends it if it's
truly stuck.

A trial's own caps (``asyncio.wait_for``) run on the event loop, so they can't fire if the loop itself stops
running: something blocking it, or a wait that never wakes. This thread can. Every few seconds it asks the loop to
run a no-op and watches telnet traffic (``core.telnet.last_io``):

* **Loop blocked** for ``blocked_after`` seconds: every thread's Python stack (``faulthandler``, which works while
  the loop is stuck) goes to ``stall.txt`` in the run folder, with a line on stderr and in ``progress.jsonl``.
* **Still blocked** at ``exit_after``: the process exits with code 2 ("couldn't run"). The test MUD is left
  behind, and the next ``TestMud.start()`` on that port stops it (core.server).
* **No traffic** for ``quiet_after`` seconds while the loop runs: every asyncio task's stack goes to
  ``stall.txt``, to show what is waiting on what. Only a record: long quiet spells can be legitimate.

Each record is written once per episode, then again only after the loop or the traffic has moved on.

Only command-line trials are watched: ``python -m xania_sim <trial>`` arms it (``arm``) and the trial's Heartbeat,
created on the running loop, hands it that loop and the run folder (``adopt``). Tests aren't watched.
Settings (environment variables): ``XANIA_SIM_WATCHDOG=0`` turns it off; ``XANIA_SIM_STALL_EXIT`` (seconds, ``0``
for never) changes ``exit_after``.
"""

from __future__ import annotations

import asyncio
import faulthandler
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

from xania_sim.core import telnet

BLOCKED_AFTER = 120.0
EXIT_AFTER = float(os.environ.get("XANIA_SIM_STALL_EXIT", 600))
QUIET_AFTER = 300.0
EVERY = 5.0
EXIT_CODE = 2  # record.result: "couldn't run"


class Watchdog:
    def __init__(self, loop: asyncio.AbstractEventLoop | None, folder: Path, trial: str = "", *,
                 blocked_after: float = BLOCKED_AFTER, exit_after: float | None = EXIT_AFTER,
                 quiet_after: float | None = QUIET_AFTER, every: float = EVERY, exit=os._exit):
        self.loop, self.folder, self.trial = loop, Path(folder), trial
        self.blocked_after, self.quiet_after, self.every = blocked_after, quiet_after, every
        self.exit_after = exit_after or None
        self._exit = exit
        self.answered = time.monotonic()  # when the loop last ran our no-op
        self.records: list[str] = []  # "blocked" / "quiet" / "exit", in order (for tests and the report)
        self._stop = threading.Event()
        self._blocked_noted = self._quiet_noted = False
        self._thread = threading.Thread(target=self._run, name="xania-sim-watchdog", daemon=True)

    @property
    def path(self) -> Path:
        return self.folder / "stall.txt"

    def start(self) -> Watchdog:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    # -- the thread ---------------------------------------------------------------------------------------------
    def _answer(self) -> None:
        self.answered = time.monotonic()

    def watch(self, loop: asyncio.AbstractEventLoop, folder: Path, trial: str = "") -> None:
        """Watch ``loop`` from now on, writing to ``folder`` (a new trial's run, or a new asyncio.run)."""
        self.answered = time.monotonic()
        self._blocked_noted = self._quiet_noted = False
        self.loop, self.folder, self.trial = loop, Path(folder), trial

    def _run(self) -> None:
        while not self._stop.wait(self.every):
            if self.loop is None or self.loop.is_closed():
                continue  # between trials (or before the first): nothing to watch
            try:
                self.loop.call_soon_threadsafe(self._answer)
            except RuntimeError:  # the loop closed between the check and the call
                return
            self.check(time.monotonic())

    def check(self, now: float) -> None:
        """One look at the loop and the traffic (the thread calls it every ``every`` seconds)."""
        blocked = now - self.answered
        if blocked < self.every * 2:
            self._blocked_noted = False
        elif blocked >= self.blocked_after and not self._blocked_noted:
            self._blocked_noted = True
            self._record("blocked", f"the event loop hasn't run for {blocked:.0f} s; every thread's stack:",
                         threads=True)
        if self.exit_after and blocked >= self.exit_after:
            self._record("exit", f"the event loop is still blocked after {blocked:.0f} s: exiting with code "
                                 f"{EXIT_CODE} (the next TestMud.start() on this port stops the server left behind)")
            self._stop.set()
            self._exit(EXIT_CODE)
            return
        quiet = now - telnet.last_io
        if self.quiet_after is None or blocked >= self.every * 2:
            return
        if quiet < self.quiet_after:
            self._quiet_noted = False
        elif not self._quiet_noted:
            self._quiet_noted = True
            # ask the loop for its tasks' stacks: it's running, so it can answer
            self.loop.call_soon_threadsafe(self._record, "quiet",
                                           f"no MUD traffic for {quiet:.0f} s; every asyncio task's stack:", False,
                                           True)

    # -- writing ------------------------------------------------------------------------------------------------
    def _record(self, kind: str, headline: str, threads: bool = False, tasks: bool = False) -> None:
        self.records.append(kind)
        stamp = time.strftime("%H:%M:%S")
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(f"==== {stamp} {kind}: {headline}\n")
                f.flush()
                if threads:
                    faulthandler.dump_traceback(file=f, all_threads=True)
                if tasks:
                    f.write(_task_stacks(self.loop))
                f.write("\n")
            with (self.folder / "progress.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at": stamp, "who": "watchdog", "what": kind, "see": str(self.path)}) + "\n")
        except OSError:
            pass  # the watchdog must never be what stops a trial
        print(f"[watchdog] {self.trial} {kind}: {headline.rstrip(':;')} (see {self.path})", file=sys.stderr,
              flush=True)


def _task_stacks(loop: asyncio.AbstractEventLoop) -> str:
    buf = io.StringIO()
    for task in asyncio.all_tasks(loop):
        buf.write(f"-- {task!r}\n")
        task.print_stack(file=buf)
    return buf.getvalue()


_armed: Watchdog | None = None


def arm() -> Watchdog | None:
    """Start the process's watchdog (``python -m xania_sim``); it watches nothing until a trial ``adopt``s it."""
    global _armed
    if _armed is None and os.environ.get("XANIA_SIM_WATCHDOG", "1") != "0":
        _armed = Watchdog(None, Path("runs")).start()
    return _armed


def disarm() -> None:
    global _armed
    if _armed is not None:
        _armed.stop()
        _armed = None


def adopt(folder: Path, trial: str = "") -> None:
    """Point an armed watchdog at the running loop and this run's folder (a no-op if unarmed or outside a loop)."""
    if _armed is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _armed.watch(loop, folder, trial)
