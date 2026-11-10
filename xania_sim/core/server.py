"""An isolated test MUD: its own doorman + xania on their own port, with a throwaway data directory.

It uses the binaries and areas of a built Xania checkout (``make`` in it first) but never its player files, so a
run starts from an empty world of players and can't disturb a MUD you're playing on. Doorman and xania find each
other through a socket named after the port (``/tmp/.xania-<port>-<user>``), so instances on different ports
don't clash.

On Windows the MUD runs in WSL. WSL stops a distro when no ``wsl.exe`` session is attached, so the start script
stays in the foreground (``wait``) for as long as the server should live.

Settings (environment variables):
- ``XANIA_SIM_DISTRO``: WSL distro (default ``Ubuntu-26.04``; ignored when running on Linux)
- ``XANIA_SIM_XANIA``: the Xania checkout, as a Linux path (default ``$HOME/xania``)
- ``XANIA_SIM_GDB``: ``1`` (default) runs xania under gdb when gdb is installed, so a crash leaves a backtrace in
  its log (``crash_report``); ``0`` runs it plain. gdb only watches: no breakpoints, signals passed through.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
import sys
import time

from xania_sim.core.telnet import Closed, Connection, Timeout

DISTRO = os.environ.get("XANIA_SIM_DISTRO", "Ubuntu-26.04")
XANIA = os.environ.get("XANIA_SIM_XANIA", "$HOME/xania")
GDB = os.environ.get("XANIA_SIM_GDB", "1")  # 0 = run xania without gdb
ON_WINDOWS = sys.platform == "win32"
BACKTRACE_MARKER = "=== xania-sim: backtrace ==="


def set_checkout(linux_path: str) -> None:
    """Point the test MUD, the game repo and the world at another Xania checkout (``--xania``), before using them."""
    global XANIA
    XANIA = linux_path

_START = """
set -e
X={xania}
D={data}
test -x "$X/install/bin/xania" || {{ echo "NO_BUILD $X/install/bin/xania"; exit 3; }}
{wipe}mkdir -p "$D/player" "$D/gods" "$D/system" "$D/log"
test -f "$D/log/xania.log" && mv "$D/log/xania.log" "$D/log/xania-$(date +%s).log"
AREA="$X/area/"
{area_list}export MUD_PORT={port} MUD_AREA_DIR="$AREA" MUD_HTML_DIR="$X/html/" MUD_DATA_DIR="$D/"
cd "$X"
"$X/install/bin/doorman" > "$D/log/doorman.log" 2>&1 & echo $! > "$D/doorman.pid"
sleep 0.5
if [ "{gdb}" != 0 ] && command -v gdb > /dev/null; then
    # Under gdb: if xania crashes, the backtrace lands in its log. Signals xania handles itself pass straight through.
    gdb -q -batch -nx -ex "set pagination off" -ex "set confirm off" \\
        -ex "handle SIGPIPE SIGTERM SIGINT SIGHUP SIGUSR1 SIGUSR2 SIGALRM SIGCHLD nostop noprint pass" \\
        -ex run -ex "echo \\n{marker}\\n" -ex "bt" -ex "echo \\n=== locals ===\\n" -ex "bt full 8" \\
        --args "$X/install/bin/xania" > "$D/log/xania.log" 2>&1 & echo $! > "$D/xania.pid"
else
    "$X/install/bin/xania" > "$D/log/xania.log" 2>&1 & echo $! > "$D/xania.pid"
fi
echo STARTED
wait
"""


def _bash() -> list[str]:
    """bash reading its script from stdin (wsl.exe would otherwise expand $VARS in arguments itself)."""
    return ["wsl.exe", "-d", DISTRO, "--", "bash", "-s"] if ON_WINDOWS else ["bash", "-s"]


def run_bash(script: str, check: bool = True) -> str:
    """Run a bash script where the MUD runs (WSL on Windows) and return its output."""
    r = subprocess.run(_bash(), input=script.encode(), capture_output=True, timeout=60)  # bytes: keep \n as is
    out = r.stdout.decode("utf-8", "replace")
    if check and r.returncode:
        raise RuntimeError(f"command failed ({r.returncode}): {script}\n{out}{r.stderr.decode('utf-8', 'replace')}")
    return out


def checkout_path() -> str:
    """The test MUD's Xania checkout as a path this Python can read (a \\\\wsl.localhost\\... path on Windows), so
    Explorer analyses exactly the areas the MUD loads."""
    if ON_WINDOWS:
        return run_bash(f'wslpath -w "{XANIA}"').strip()
    return run_bash(f'echo "{XANIA}"').strip()


def area_list_script(names: list[str] | None) -> str:
    """Bash that makes ``$D/area/``: every file of the checkout's area dir symlinked, and an area.lst of ``names``."""
    if not names:
        return ""
    for n in names:
        if not re.fullmatch(r"[\w.-]+", n):
            raise ValueError(f"not an area file name: {n!r}")
    listing = "\\n".join(names + ["$"])
    return (f'rm -rf "$D/area"; mkdir -p "$D/area"; ln -s "$X/area/"* "$D/area/"; rm "$D/area/area.lst"\n'
            f"printf '{listing}\\n' > \"$D/area/area.lst\"\nAREA=\"$D/area/\"\n")


class TestMud:
    """``async with TestMud(port=9100) as mud:`` then connect bots to ``mud.host``/``mud.port``."""

    __test__ = False  # not a pytest test class

    def __init__(self, port: int = 9100, host: str = "127.0.0.1", area_list: list[str] | None = None):
        """``area_list``: load only these area files, in this order (a cut-down world), instead of the checkout's
        area.lst. Every file stays available (symlinked) for anything an area refers to."""
        self.port, self.host = port, host
        self.area_list = area_list
        self.data_dir = f"/tmp/xania-sim/{port}"  # Linux path
        self._proc: subprocess.Popen | None = None

    def run(self, script: str, check: bool = True) -> str:
        return run_bash(script, check)

    async def start(self, timeout: float = 60, fresh: bool = True) -> None:
        """Start the MUD. ``fresh`` empties the data dir first (no players); a restart keeps it."""
        if await self._answers():
            raise RuntimeError(f"something is already listening on port {self.port}")
        script = _START.format(xania=XANIA, data=shlex.quote(self.data_dir), port=self.port,
                               wipe='rm -rf "$D"\n' if fresh else "", gdb=GDB, marker=BACKTRACE_MARKER,
                               area_list=area_list_script(self.area_list))
        self._proc = subprocess.Popen(_bash(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT)
        self._proc.stdin.write(script.encode())
        self._proc.stdin.close()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                out = self._proc.stdout.read().decode("utf-8", "replace") if self._proc.stdout else ""
                raise RuntimeError(f"the test MUD failed to start:\n{out}\n{self.log('xania')[-2000:]}")
            if await self._answers():
                return
            await asyncio.sleep(0.5)
        await self.stop()
        raise RuntimeError(f"the test MUD didn't answer on port {self.port} within {timeout:g}s")

    async def _answers(self) -> bool:
        """True once xania is up behind doorman (the lobby's name prompt arrives)."""
        try:
            conn = await Connection.open(self.host, self.port, timeout=2)
        except (OSError, asyncio.TimeoutError):
            return False
        try:
            await conn.expect("State thy name", timeout=3)
            return True
        except (Timeout, Closed):
            return False
        finally:
            await conn.close()

    def alive(self) -> bool:
        """Is the xania process still running? (Doorman stays up when xania crashes.)"""
        d = shlex.quote(self.data_dir)
        return self.run(f"test -f {d}/xania.pid && kill -0 $(cat {d}/xania.pid) 2>/dev/null && echo yes",
                        check=False).strip() == "yes"

    def crash_report(self, lines: int = 15) -> str:
        """What stopped xania: under gdb, the signal, the last log lines before it and the backtrace; otherwise the
        end of its log (an assertion message, for example)."""
        log = self.log("xania").rstrip()
        all_lines = log.splitlines()
        asan = next((i for i, ln in enumerate(all_lines) if "ERROR: AddressSanitizer" in ln), None)
        if asan is not None:  # a sanitizer build: its report has the stacks (gdb sees only the exit)
            end = next((i for i in range(asan, len(all_lines)) if "SUMMARY: AddressSanitizer" in all_lines[i]),
                       min(len(all_lines) - 1, asan + 160))
            return "\n".join(all_lines[max(0, asan - 8):end + 1])
        sig = next((i for i, ln in enumerate(all_lines) if ln.startswith(("Program received signal",
                                                                           "Thread", "Program terminated"))
                    and "signal" in ln), None)
        if sig is not None and BACKTRACE_MARKER in log:
            return "\n".join(all_lines[max(0, sig - 8):sig + 140])
        return "\n".join(all_lines[-lines:])

    def crashed(self) -> bool:
        """Did xania stop on a signal (a crash), as opposed to shutting down normally?"""
        log = self.log("xania")
        return ("received signal" in log or "Assertion" in log or "terminated with signal" in log
                or "ERROR: AddressSanitizer" in log)

    async def restart(self) -> None:
        """Stop what's left (doorman) and start again, keeping players and logs."""
        await self.stop()
        await self.start(fresh=False)

    def log(self, which: str = "xania") -> str:
        return self.run(f"cat {shlex.quote(self.data_dir)}/log/{which}.log 2>/dev/null || true", check=False)

    async def stop(self) -> None:
        d = shlex.quote(self.data_dir)
        # xania first (under gdb it's gdb's child), then gdb or plain xania, then doorman
        self.run(f"test -f {d}/xania.pid && pkill -TERM -P $(cat {d}/xania.pid) 2>/dev/null; sleep 0.5; "
                 f"for p in xania doorman; do test -f {d}/$p.pid && kill $(cat {d}/$p.pid) 2>/dev/null; done; true",
                 check=False)
        if self._proc:
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    async def __aenter__(self) -> TestMud:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()
