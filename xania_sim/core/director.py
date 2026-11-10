"""The director: an immortal on the test MUD that sets the stage for other bots (levels, skills, gear, rooms).

A fresh test MUD has no players, so there's no immortal to log in as. ``Director.create`` makes one without
touching Xania's code: it creates an ordinary character through the lobby, quits, rewrites the ``Levl`` line of
its player file (save.cpp / CharFileMeta.hpp: ``Levl``) in the test MUD's throwaway data directory, and logs back
in. Only ever on a ``TestMud``: the player file it edits lives under ``mud.data_dir``.

Commands it wraps (InterpreterImpl.cpp, with the level each needs; the director is level 100, IMPLEMENTOR):
``advance`` (act_wiz.cpp do_advance: keeps the victim's practice sessions as they were), ``set skill <name>
<skill|all> <0-100>``, ``set char <name> <field> <value>``, ``load obj <vnum>`` / ``load mob <vnum>``,
``transfer``, ``restore``, ``peace``, ``goto``, ``force``, ``at``.
"""

from __future__ import annotations

import re
import shlex

from xania_sim.core.session import Bot, Profile

LEVEL = 100  # Constants.hpp IMPLEMENTOR
NAME = "Directrix"
PASSWORD = "directorpass1"
OK = r"Ok\.|done|You|They|is now|set|Syntax|isn't|not found|No such|Huh"


class DirectorError(Exception):
    pass


class Director:
    """``async with await Director.create(mud) as d: await d.advance("Bob", 40)``."""

    def __init__(self, bot: Bot):
        self.bot = bot

    @classmethod
    async def create(cls, mud, name: str = NAME, password: str = PASSWORD) -> Director:
        """Log in the test MUD's immortal, creating it (and promoting it in its player file) the first time."""
        if not await cls._exists(mud, name):
            bot = Bot(name, password, host=mud.host, port=mud.port)
            await bot.enter(Profile(cls="mage"))
            await bot.quit()
            pfile = f"{mud.data_dir}/player/{name.capitalize()}"
            out = mud.run(f"test -f {shlex.quote(pfile)} && sed -i 's/^Levl .*/Levl {LEVEL}/' {shlex.quote(pfile)} "
                          f"&& grep '^Levl' {shlex.quote(pfile)}", check=False)
            if f"Levl {LEVEL}" not in out:
                raise DirectorError(f"couldn't promote {name} in {pfile}: {out.strip()}")
        bot = Bot(name, password, host=mud.host, port=mud.port)
        await bot.enter()
        d = cls(bot)
        r = await d.do("score", expect=r"Level: \S*\d+")  # act_info.cpp do_score: "Level: 100 (trust 100)"
        if not re.search(rf"Level: \S*{LEVEL}\b", r):
            raise DirectorError(f"{name} logged in but isn't level {LEVEL}:\n{r[:400]}")
        await d.do("holylight", expect=r"[Hh]oly light")  # see everything, in the dark too
        return d

    @staticmethod
    async def _exists(mud, name: str) -> bool:
        out = mud.run(f"test -f {shlex.quote(f'{mud.data_dir}/player/{name.capitalize()}')} && echo yes", check=False)
        return "yes" in out

    async def __aenter__(self) -> Director:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        try:
            await self.bot.quit()
        except Exception:
            await self.bot.close()

    async def do(self, command: str, expect: str | None = OK, timeout: float = 10) -> str:
        return (await self.bot.do(command, expect=expect, timeout=timeout)).text

    # --- stage setting -------------------------------------------------------------------------------------------

    async def advance(self, name: str, level: int) -> str:
        """Raise (or lower) a mortal to ``level``. Hit points and mana grow level by level as in play; practice
        sessions don't (do_advance restores them), so give skills with ``set_skill``."""
        return await self.do(f"advance {name} {level}", expect=r"Raising a player|Lowering a player|"
                                                                r"already|not here|Not on NPC|Limited to|Syntax")

    async def set_skill(self, name: str, skill: str, value: int) -> str:
        """``skill`` may be ``all``. Whether the victim can *use* it still depends on its class and level
        (skills.cpp get_skill_level)."""
        # silent when it works (act_wiz.cpp do_sset), so read to the next prompt only
        cmd = f"set skill {name} all {value}" if skill == "all" else f"set skill {name} '{skill}' {value}"
        return await self.do(cmd, expect=None)

    async def set_char(self, name: str, field: str, value: int | str) -> str:
        return await self.do(f"set char {name} {field} {value}")

    async def give_obj(self, name: str, vnum: int, keyword: str | None = None) -> str:
        """Load an object and hand it to ``name`` (who must be in the director's room: see ``bring``). do_oload only
        says "Ok.", so the keyword to give it by comes from the area files unless given."""
        r = await self.do(f"load obj {vnum}", expect=r"Ok\.|No object|Syntax")
        if "Ok." not in r:
            raise DirectorError(f"load obj {vnum}: {r.strip()}")
        keyword = keyword or _keyword(vnum)
        return r + await self.do(f"give {keyword} {name}", expect=r"You give|can't carry|can't let go|"
                                                                 r"They aren't|You do not have|isn't here")

    async def bring(self, name: str) -> str:
        """Move ``name`` to the director's room."""
        # act_wiz.cpp do_transfer: "Ok." to the immortal once the victim has arrived (and looked) (400)
        return await self.do(f"transfer {name}", expect=re.compile(
            r"^Ok\.$|They aren't here|They are in limbo|No such location|private right now|Transfer whom", re.M))

    async def goto(self, vnum: int | str) -> str:
        # do_goto ends with look_auto: the room's exits line (or a refusal)
        return await self.do(f"goto {vnum}", expect=r"\[Exits:|No such location|private right now|Goto where")

    async def restore(self, name: str = "all") -> str:
        return await self.do(f"restore {name}", expect=r"restored|Ok\.|They aren't")

    async def peace(self) -> str:
        return await self.do("peace", expect=r"Ok\.")

    async def force(self, name: str, command: str) -> str:
        return await self.do(f"force {name} {command}", expect=r"Ok\.|They aren't|Do it yourself|Huh")


_WORLD = None


def _keyword(vnum: int) -> str:
    """The first keyword of an object, from the area files the test MUD loads."""
    global _WORLD
    if _WORLD is None:
        from xania_sim.core import world as W

        _WORLD = W.load()
    obj = _WORLD.objects.get(vnum) if hasattr(_WORLD, "objects") else None
    if obj is None:
        raise DirectorError(f"no object {vnum} in the area files")
    return obj.keywords.split()[0]
