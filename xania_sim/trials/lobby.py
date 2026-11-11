"""The lobby trial: ``python -m xania_sim lobby``. A careless player pressing Enter at each login prompt.

``MudImpl::nanny`` (MudImpl.cpp) reads ``argument[0]`` at several lobby prompts without checking the reply is
empty, and an empty line does reach it (``Descriptor::pop_incomm`` passes blank lines on; ``nanny`` trims spaces to
nothing). On an empty ``std::string_view`` that index is out of range: a debug build aborts on the libstdc++
assertion, taking the MUD down. The prompts: "Did I hear that right" (ConfirmNewName), male/female/other
(GetNewSex), alignment (GetAlignment), "Customize" (DefaultChoice) and "connect anyway" (BreakConnect, for a
character that's already playing).

For each prompt: walk the lobby to it, send an empty line, and see whether xania is still up. A crash is a finding
(with its report saved in the run folder); the MUD is restarted before the next prompt.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from xania_sim.core import world as W
from xania_sim.core.names import bot_names, mob_words
from xania_sim.core.server import TestMud
from xania_sim.core.session import _LOBBY, Bot, Profile
from xania_sim.core.telnet import Closed, Connection, Timeout
from xania_sim.record.findings import Finding, crash_finding
from xania_sim.record.heartbeat import Heartbeat
from xania_sim.record.result import Outcome

PROMPTS = {  # Sim's lobby step -> nanny's state (MudImpl.cpp) and the line that reads argument[0]
    "confirm_new": ("ConfirmNewName", 740),
    "sex": ("GetNewSex", 854),
    "alignment": ("GetAlignment", 890),
    "customize": ("DefaultChoice", 915),
    "break_connect": ("BreakConnect", 711),
}
PASSWORD = "lobbypass1"
SETTLE = 4.0  # seconds to give xania to die (and gdb to write its backtrace) after the empty line


async def to_prompt(conn: Connection, target: str, name: str, timeout: float = 15) -> str:
    """Answer the lobby normally until ``target``'s prompt appears; return the prompt's text."""
    answers = {"name": name, "confirm_new": "y", "password": PASSWORD, "race": "human", "sex": "o", "cls": "knight",
               "alignment": "n", "customize": "n", "ansi": "n", "break_connect": "y", "continue": ""}
    patterns = [re.compile(p, re.M) for _, p in _LOBBY]
    while True:
        _, m = await conn.expect(*patterns, timeout=timeout)
        step = _LOBBY[patterns.index(m.re)][0]
        if step == target:
            return m.group(0)
        if step == "continue":
            raise RuntimeError(f"reached the MOTD without seeing the {target} prompt")
        await conn.send(answers[step])


async def _press_enter(mud: TestMud, conn: Connection) -> tuple[bool, str]:
    """Send an empty line; (xania still up, what it replied)."""
    await conn.send("")
    reply = ""
    try:
        reply = await asyncio.wait_for(conn.drain_output(), timeout=SETTLE)
    except (asyncio.TimeoutError, Timeout, Closed):
        pass
    await asyncio.sleep(SETTLE)
    return await asyncio.to_thread(mud.alive), reply.strip()[-200:]


async def run_lobby(result, port: int = 9170, prompts: list[str] | None = None) -> list[Finding]:
    world = W.load()
    names = iter(bot_names("lobby", 200, mob_words(world)))
    runs = Path("runs") / time.strftime("lobby-%Y%m%d-%H%M%S")
    beat = Heartbeat(runs, "lobby")
    found: list[Finding] = []
    targets = prompts or list(PROMPTS)
    async with TestMud(port=port) as mud:
        for target in targets:
            state, line = PROMPTS[target]
            repro = f"python -m xania_sim lobby --prompts {target}"
            beat(state, "walking to the prompt")
            first = conn = None
            try:
                name = next(names)
                if target == "break_connect":  # the character has to be playing already
                    first = Bot(name, PASSWORD, host=mud.host, port=mud.port, transcript_dir=runs)
                    await first.enter(Profile())
                conn = await Connection.open(mud.host, mud.port, runs / f"{state}.log")
                prompt = await to_prompt(conn, target, name)
                alive, reply = await _press_enter(mud, conn)
            except Exception as e:  # noqa: BLE001 - the lobby didn't go as expected: say so, try the next prompt
                result.outcomes.append(Outcome("lobby", None, f"{state}: couldn't reach the prompt ({e})", [], {}))
                beat(state, "couldn't reach the prompt", error=str(e)[:120])
                alive, prompt, reply = await asyncio.to_thread(mud.alive), "", ""
            else:
                ok = alive
                result.outcomes.append(Outcome("lobby", ok, f"{state}: {'still up' if alive else 'XANIA DIED'} after "
                                               f"an empty reply to {prompt.strip()!r}", [reply] if reply else [],
                                               {"state": state, "line": f"MudImpl.cpp:{line}"}))
                beat(state, "survived" if alive else "xania died", prompt=prompt.strip(), reply=reply[-80:])
            finally:
                for c in (conn, first):
                    if c is not None:
                        try:
                            await asyncio.wait_for(c.close(), timeout=5)
                        except Exception:  # noqa: BLE001
                            pass
            result.measurements[state] = "still up" if alive else "crashed"
            if not alive:
                report = mud.crash_report()
                path = runs / f"xania-crash-{state}.txt"
                path.write_text(report, encoding="utf-8")
                result.artifacts[f"crash_{state}"] = str(path)
                f = crash_finding(report, repro, f"an empty reply at the {state} prompt (MudImpl.cpp:{line})")
                found.append(f)
                await mud.restart()
    return found
