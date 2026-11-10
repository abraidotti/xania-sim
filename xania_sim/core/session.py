"""One bot = one character on one connection.

``enter`` walks the lobby (``nanny`` in xania/src/MudImpl.cpp): it creates the character if the name is new and a
``Profile`` was given, or logs in if it exists. Once playing, the bot sets a machine-readable prompt and from then
on follows one rule: send one command, then wait for the next prompt. Each ``do`` returns that command's output
and the prompt that ended it. (Output the MUD sends unprompted, e.g. combat rounds or someone talking, can land
in the next response; behaviours treat responses as "what happened since the last command".)
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from xania_sim.core import parsers as P
from xania_sim.core.telnet import Closed, Connection, Timeout


class LoginError(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    """Choices at character creation. Names as the MUD lists them (see xania_explorer.api.pc_races/classes)."""

    race: str = "human"
    cls: str = "knight"
    sex: str = "o"  # m, f or o
    alignment: str = "n"  # g, n or e


@dataclass
class Response:
    command: str
    text: str
    prompt: P.Prompt
    complete: bool = True  # False if an expected reply never arrived

    @property
    def lines(self) -> list[str]:
        return P.lines(self.text)

    def has(self, pattern: str) -> bool:
        return re.search(pattern, self.text, re.I | re.M) is not None


# Lobby prompts, in the order a new character meets them. Each maps to what to answer (a Profile/password field).
_LOBBY = [
    ("name", r"(State thy name and enter the realm of Xania:|Name:|OK, try again:) ?$"),
    ("confirm_new", r"Did I hear that right - '.*' \(Y/N\)\? ?$"),
    ("password", r"(Think of a password for \w+:|Password:|Retype password:|Please retype password:) ?$"),
    ("race", r"What is your race \(help for more information\)\? ?$"),
    ("sex", r"Are you male, female or other \(M/F/O\)\? ?$"),
    ("cls", r"(What is your class \(help for more information\)\?|What IS your class\?) ?$"),
    ("alignment", r"Which alignment \(G/N/E\)\? ?$"),
    ("customize", r"Customize \(Y/N\)\? ?\n?$"),
    ("ansi", r"Does your terminal support ANSI colour.*\? ?$"),
    ("break_connect", r"Do you wish to connect anyway \(Y/N\)\? ?$"),
    ("continue", r"\[Hit [Rr]eturn to continue\]\n?$"),
]
_FAILURES = (r"(Illegal name, try another|Wrong password|You are denied access|The game is wizlocked|"
             r"The game is newlocked|Your site has been banned|Your character could not be created|"
             r"That is not a valid race|That's not a class|That's not a valid alignment|"
             r"Password must be at least|New password not acceptable)")


AUTOLOOT_REPLY = r"Autolooting removed|Automatic corpse looting set"


class Bot:
    """``autoloot``: bots turn autoloot off by default (None leaves it alone). With it on, killing a mob that carries
    anything crashes debug builds of xania: autoloot runs ``get all corpse``, and do_get reads ``arg1[3]`` past the
    end of "all" (src/act_obj.cpp:707)."""

    def __init__(self, name: str, password: str, host: str = "127.0.0.1", port: int = 9000,
                 transcript_dir: Path | None = None, autoloot: bool | None = False):
        self.autoloot = autoloot
        self.name, self.password, self.host, self.port = name.capitalize(), password, host, port
        self.transcript = transcript_dir / f"{self.name}.log" if transcript_dir else None
        self.conn: Connection | None = None
        self.prompt: P.Prompt | None = None
        self.created = False
        # callables (text, prompt) told about every chunk of output read while playing, e.g. events.Recorder.feed
        self.observers: list = []

    # -- lobby -----------------------------------------------------------------------------
    async def enter(self, profile: Profile | None = None, timeout: float = 15) -> Response:
        """Connect, create or log in, and set the bot prompt. Raises LoginError if the MUD refuses."""
        self.conn = await Connection.open(self.host, self.port, self.transcript)
        answers = {"name": self.name, "password": self.password, "customize": "n", "ansi": "n",
                   "break_connect": "y", "continue": ""}
        if profile:
            answers |= {"race": profile.race, "sex": profile.sex, "cls": profile.cls, "alignment": profile.alignment}
        patterns = [re.compile(p, re.M) for _, p in _LOBBY] + [re.compile(_FAILURES)]
        while True:
            try:
                before, m = await self.conn.expect(*patterns, timeout=timeout)
            except Closed as e:
                raise LoginError(f"{self.name}: the MUD closed the connection in the lobby") from e
            if m.re is patterns[-1]:
                raise LoginError(f"{self.name}: {m.group(0)}")
            step = _LOBBY[patterns.index(m.re)][0]
            if step == "confirm_new":
                if not profile:
                    await self.conn.send("n")
                    raise LoginError(f"{self.name} doesn't exist (pass a Profile to create it)")
                self.created = True
                await self.conn.send("y")
            elif step == "continue":
                # the MOTD's "[Hit return to continue]" (immortals see the IMOTD first, then the MOTD)
                await self.conn.send("")
                while P.HIT_RETURN.search(await self.conn.drain_output()):
                    await self.conn.send("")
                break
            else:
                if step not in answers:
                    raise LoginError(f"{self.name}: unexpected lobby prompt {m.group(0)!r}")
                await self.conn.send(answers[step])
        # Until the bot prompt is set, the end of a command's output can't be recognised (and a returning character
        # may already have it saved, so earlier output can end in one too): sync on the reply to "prompt" itself.
        await self.conn.send("scroll 0")
        await self.conn.send(f"prompt {P.PROMPT_FORMAT}")
        await self.conn.expect(r"Ok - prompt set\.\n", timeout=timeout)
        _, m = await self.conn.expect(P.PROMPT, timeout=timeout)
        self.prompt = P.parse_prompt(m)
        if self.autoloot is not None:
            await self.set_autoloot(self.autoloot)
        return Response("prompt", "Ok - prompt set.\n", self.prompt)

    async def set_autoloot(self, on: bool) -> None:
        """``autoloot`` toggles (do_autoloot, act_info.cpp), so toggle until it's as wanted."""
        for _ in range(2):
            r = await self.do("autoloot", expect=AUTOLOOT_REPLY)
            if ("Automatic corpse looting set" in r.text) == on:
                return
        raise LoginError(f"{self.name}: couldn't set autoloot {'on' if on else 'off'}: {r.text!r}")

    # -- playing -----------------------------------------------------------------------------
    async def do(self, command: str, expect: str | re.Pattern | None = None, timeout: float = 10) -> Response:
        """Send one command and return its output up to a bot prompt, paging through long output.

        The MUD also prompts on its own (regen ticks, combat rounds, other people's actions), so a prompt doesn't
        always end *this* command's reply. With ``expect`` (a regex the reply must contain), reading goes on past
        prompts until it appears; if it never does, the response comes back after ``timeout`` with
        ``complete=False``. Unprompted output read along the way is included in ``text``."""
        assert self.conn, "call enter() first"
        pattern = re.compile(expect, re.M) if isinstance(expect, str) else expect
        await self.conn.send(command)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        text = ""
        while True:
            try:
                before, m = await self.conn.expect(P.PROMPT, P.HIT_RETURN, timeout=max(0.05, deadline - loop.time()))
            except Timeout:
                if pattern is None or self.prompt is None:
                    raise
                return Response(command, text, self.prompt, complete=False)
            text += before
            if m.re is P.HIT_RETURN:
                self._tell(before)
                await self.conn.send("")
                continue
            self.prompt = P.parse_prompt(m)
            self._tell(before)
            if pattern is None or pattern.search(text):
                return Response(command, text, self.prompt)
            text += "\n"

    async def listen(self, timeout: float) -> Response:
        """Wait (without sending anything) for the MUD's next prompt, e.g. a combat round. ``complete=False`` if
        none came within ``timeout``; anything unprompted that did arrive stays buffered for the next read."""
        assert self.conn, "call enter() first"
        try:
            before, m = await self.conn.expect(P.PROMPT, timeout=timeout)
        except Timeout:
            return Response("", "", self.prompt, complete=False)
        self.prompt = P.parse_prompt(m)
        self._tell(before)
        return Response("", before, self.prompt)

    def _tell(self, text: str) -> None:
        for observe in self.observers:
            observe(text, self.prompt)

    async def quit(self) -> str:
        """Quit the game (saving the character) and close the connection. Returns the farewell text."""
        if not self.conn or self.conn.closed:
            return ""
        try:
            await self.conn.send("quit")
            text = await self.conn.drain_output(quiet=1.0)
        except Closed:
            text = ""
        await self.conn.close()
        return text

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
