"""A telnet connection to doorman, reduced to plain text.

Doorman negotiates TTYPE, NAWS and ECHO (``xania/src/doorman/TelnetProtocol.cpp``). We refuse every option, so
the MUD asks about colour in the lobby and we answer no; any ANSI that still arrives is stripped. The MUD ends
lines with "\\n\\r"; we normalise to "\\n". Everything received and sent is appended to a transcript file.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class Timeout(Exception):
    def __init__(self, patterns, text: str):
        tail = text[-300:].replace("\n", "\\n")
        super().__init__(f"timed out waiting for {[getattr(p, 'pattern', p) for p in patterns]}; last output: {tail!r}")
        self.text = text


class Closed(Exception):
    pass


def strip_telnet(data: bytes, pending: bytearray) -> tuple[str, list[bytes]]:
    """Split raw bytes into text and the replies our refusals need. ``pending`` holds an incomplete IAC sequence
    between calls (it is updated in place)."""
    buf = bytes(pending) + data
    pending.clear()
    out, replies, i = bytearray(), [], 0
    while i < len(buf):
        b = buf[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= len(buf):
            pending.extend(buf[i:])
            break
        cmd = buf[i + 1]
        if cmd == IAC:  # escaped 255
            out.append(IAC)
            i += 2
        elif cmd in (DO, DONT, WILL, WONT):
            if i + 2 >= len(buf):
                pending.extend(buf[i:])
                break
            opt = buf[i + 2]
            if cmd == DO:
                replies.append(bytes([IAC, WONT, opt]))
            elif cmd == WILL:
                replies.append(bytes([IAC, DONT, opt]))
            i += 3
        elif cmd == SB:
            end = buf.find(bytes([IAC, SE]), i + 2)
            if end < 0:
                pending.extend(buf[i:])
                break
            i = end + 2
        else:  # GA, NOP and other two-byte commands
            i += 2
    text = ANSI.sub("", out.decode("latin-1")).replace("\r", "")
    return text, replies


class Connection:
    """Text in, text out. ``expect`` waits for a pattern in the unread output and consumes up to its end."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, transcript: Path | None = None):
        self._reader, self._writer = reader, writer
        self._buffer = ""
        self._pending = bytearray()
        self._log = transcript.open("a", encoding="utf-8") if transcript else None
        self.closed = False

    @classmethod
    async def open(cls, host: str, port: int, transcript: Path | None = None, timeout: float = 10) -> Connection:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        if transcript:
            transcript.parent.mkdir(parents=True, exist_ok=True)
        return cls(reader, writer, transcript)

    def _note(self, direction: str, text: str) -> None:
        if self._log:
            stamp = time.strftime("%H:%M:%S")
            for line in text.split("\n"):
                self._log.write(f"{stamp} {direction} {line}\n")
            self._log.flush()

    async def _fill(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        data = await asyncio.wait_for(self._reader.read(4096), remaining)
        if not data:
            self.closed = True
            raise Closed("the MUD closed the connection")
        text, replies = strip_telnet(data, self._pending)
        for r in replies:
            self._writer.write(r)
        if text:
            self._note("<", text)
            self._buffer += text

    async def expect(self, *patterns: str | re.Pattern, timeout: float = 10) -> tuple[str, re.Match]:
        """Wait for the earliest match of any pattern. Returns (text before the match, the match) and consumes both."""
        regexes = [re.compile(p) if isinstance(p, str) else p for p in patterns]
        deadline = time.monotonic() + timeout
        while True:
            found = [m for r in regexes if (m := r.search(self._buffer))]
            if found:
                m = min(found, key=lambda m: m.start())
                before = self._buffer[:m.start()]
                self._buffer = self._buffer[m.end():]
                return before, m
            try:
                await self._fill(deadline)
            except asyncio.TimeoutError:
                raise Timeout(regexes, self._buffer) from None

    async def drain_output(self, quiet: float = 0.3) -> str:
        """Everything that arrives until the connection has been quiet for ``quiet`` seconds."""
        while True:
            try:
                await self._fill(time.monotonic() + quiet)
            except asyncio.TimeoutError:
                break
            except Closed:
                break
        text, self._buffer = self._buffer, ""
        return text

    async def send(self, line: str) -> None:
        if self.closed:
            raise Closed("the connection is closed")
        self._note(">", line)
        self._writer.write(line.encode("latin-1", "replace") + b"\n")
        await self._writer.drain()

    async def close(self) -> None:
        self.closed = True
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        if self._log:
            self._log.close()
            self._log = None
