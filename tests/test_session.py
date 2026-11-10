"""The bot's lobby and command loop, against the fake MUD in fake_mud.py."""

import pytest

from fake_mud import FakeMud
from xania_sim.core.session import Bot, LoginError, Profile


async def test_creates_a_character_and_sets_the_prompt(tmp_path):
    async with FakeMud() as mud:
        bot = Bot("newbie", "pass123", port=mud.port, transcript_dir=tmp_path)
        r = await bot.enter(Profile(race="dwarf", cls="cleric", sex="f", alignment="g"))
        assert bot.created and bot.name == "Newbie"
        assert r.has("prompt set") and r.prompt.max_hp == 20
        assert mud.received[:9] == ["Newbie", "y", "pass123", "pass123", "dwarf", "f", "cleric", "g", "n"]
        look = await bot.do("look")
        assert look.lines[0] == "Entrance to Mud School" and look.lines[-1] == "[Exits: north down]"
        assert "You quit reality" in await bot.quit()
        log = (tmp_path / "Newbie.log").read_text()
        assert "> look" in log and "< Entrance to Mud School" in log


async def test_logs_in_an_existing_character():
    async with FakeMud() as mud:
        bot = Bot("oldie", "secret1", port=mud.port)
        await bot.enter()
        assert not bot.created
        assert (await bot.do("look")).has(r"^Entrance to Mud School$")
        await bot.quit()


async def test_refusals_raise_login_errors():
    async with FakeMud() as mud:
        with pytest.raises(LoginError, match="Wrong password"):
            await Bot("oldie", "nope123", port=mud.port).enter()
        with pytest.raises(LoginError, match="Illegal name"):
            await Bot("illi", "pass123", port=mud.port).enter(Profile())
        with pytest.raises(LoginError, match="doesn't exist"):
            await Bot("ghost", "pass123", port=mud.port).enter()


async def test_long_output_is_paged_through():
    async with FakeMud() as mud:
        bot = Bot("pager", "pass123", port=mud.port)
        await bot.enter(Profile())
        r = await bot.do("long")
        assert [ln for ln in r.lines if ln.startswith("line")] == [f"line {i}" for i in range(30)]
        assert "[Hit Return" not in r.text
        await bot.quit()
