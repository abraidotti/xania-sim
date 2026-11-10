"""Navigation against a small fake world: the map comes from an Explorer World built from area text, and the fake MUD
renders rooms the way room_look does (xania/src/act_info.cpp)."""

import pytest

from fake_mud import FakeMud
from xania_explorer.areafile import flags as F
from xania_explorer.areafile.world import load_world
from xania_sim.activities.move import Navigator
from xania_sim.record.rooms import Fingerprints, parse_view
from xania_sim.core.session import Bot, Profile


def _room(vnum, name, desc, exits=""):
    return f"#{vnum}\n{name}~\n{desc}\n~\n0 0\n{exits}S\n"


def _exit(d, to, locks=0, key=-1, kw=""):
    return f"D{d}\n~\n{kw}~\n{locks} {key} {to}\n"


#   3001 Temple -n-> 3002 Square -e-> 3003 Alley =door n=> 3007 Vault (locked in the fake)
#                     3002 -w-> 3004 Street -w-> 3005 Street (same name, text and exits) -w-> 3006 End -u-> 3007
#   3008 Den is north of 3006: the "danger" room in the avoid test
AREA = ("#AREA\nTown.are~\nTown~\n{ 1 10} Test Town~\n3000 3099\n\n#ROOMS\n"
        + _room(3001, "The Temple", "A quiet temple.", _exit(0, 3002))
        + _room(3002, "Town Square", "A busy square.", _exit(2, 3001) + _exit(1, 3003) + _exit(3, 3004))
        + _room(3003, "|yA dark alley|w", "Rubbish everywhere.", _exit(3, 3002) + _exit(0, 3007, locks=1, kw="door"))
        + _room(3004, "Long Street", "The street runs east-west.", _exit(1, 3002) + _exit(3, 3005))
        + _room(3005, "Long Street", "The street runs east-west.", _exit(1, 3004) + _exit(3, 3006))
        + _room(3006, "Street End", "The street stops here.", _exit(1, 3005) + _exit(4, 3007) + _exit(0, 3008))
        + _room(3007, "The Vault\n   |R<Warning!>|w", "Gold glitters.", _exit(2, 3003, locks=1, kw="door") + _exit(5, 3006))
        + _room(3008, "A Den", "Bones.", _exit(2, 3006))
        + "#0\n\n#$\n")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    d = tmp_path_factory.mktemp("town") / "area"
    d.mkdir()
    (d / "area.lst").write_text("Town.are\n$\n")
    (d / "Town.are").write_text(AREA)
    return load_world(d)


class Town:
    """The fake MUD's world: one character moving through ``world``. The vault door is closed and locked."""

    def __init__(self, mud: FakeMud, world, start=3001, move_cost=1):
        self.mud, self.w, self.here, self.cost = mud, world, start, move_cost
        self.closed = {(3003, 0), (3007, 2)}
        self.locked = {(3003, 0), (3007, 2)}
        self.resting = False
        self.commands: list[str] = []

    def view(self) -> str:
        r = self.w.rooms[self.here]
        from xania_sim.record.rooms import plain

        exits = " ".join(F.DIRECTIONS[d] for d in sorted(r.exits) if (self.here, d) not in self.closed) or "none"
        return f"{plain(r.name)}\n\r\n\r  {r.description}\n\r\n\r[Exits: {exits}]\n\r"

    def __call__(self, cmd: str):
        self.commands.append(cmd)
        if cmd in F.DIRECTIONS:
            d = F.DIRECTIONS.index(cmd)
            e = self.w.rooms[self.here].exits.get(d)
            if e is None:
                return "Alas, you cannot go that way.\n\r"
            if (self.here, d) in self.closed:
                return f"The {e.keyword} is closed.\n\r"
            if self.mud.moves < self.cost:
                return "You are too exhausted.\n\r"
            if self.resting:
                return "Nah... You feel too relaxed...\n\r"
            self.mud.moves -= self.cost
            self.here = e.to_vnum
            return self.view()
        if cmd.startswith("open "):
            d = F.DIRECTIONS.index(cmd.split()[1])
            if (self.here, d) in self.locked:
                return "It's locked.\n\r"
            self.closed.discard((self.here, d))
            return "Ok.\n\r"
        if cmd == "look":
            return self.view()
        if cmd == "recall":
            self.here = 3001
            return "You pray for transportation!\n\r" + self.view()
        if cmd in ("rest", "sleep"):
            self.resting = True
            self.mud.moves = 100  # a tick passes instantly in the fake
            return "You sit down and rest.\n\r" if cmd == "rest" else "You go to sleep.\n\r"
        if cmd == "wake":
            self.resting = False
            return "You wake and stand up.\n\r"
        if cmd == "stand":
            self.resting = False
            return "You stand up.\n\r"
        if cmd == "time":
            return "It is 5 o'clock.\n\r"
        return None


def test_room_views_parse_and_match(world):
    fp = Fingerprints(world)
    v = parse_view("You pray for transportation!\nThe Vault\n<Warning!>\n\n  Gold glitters.\n\n[Exits: down]\nA coin.\n")
    assert v.exits == {"down"} and "The Vault" in v.lines
    assert fp.candidates(v) == [3007]  # the closed door south is hidden, which the fingerprint allows
    assert fp.matches(3003, parse_view("A dark alley\n\n  Rubbish everywhere.\n\n[Exits: west]\n"))
    street = parse_view("Long Street\n\n  The street runs east-west.\n\n[Exits: east west]\n")
    assert fp.candidates(street) == [3004, 3005]  # same fingerprint: only dead reckoning tells them apart
    assert parse_view("Alas, you cannot go that way.\n") is None


async def _navigator(mud, world, **kw):
    bot = Bot("walker", "pass123", port=mud.port)
    await bot.enter(Profile())
    return bot, Navigator(bot, world, **kw)


async def test_walks_and_replans_around_a_locked_door(world):
    async with FakeMud() as mud:
        town = mud.play = Town(mud, world)
        bot, nav = await _navigator(mud, world)
        assert await nav.locate() == 3001
        r = await nav.walk_to(3007)
        assert r.reached and nav.here == town.here == 3007
        assert nav.blocked == {(3003, 0): "locked"}
        # tries the short way through the alley, finds the door locked, goes back and round the long way
        moves = [c for c in town.commands if c != "autoloot"]  # bots switch autoloot off on entering
        assert moves == ["look", "north", "east", "open north", "west", "west", "west", "west", "up"]
        await bot.quit()


async def test_dead_reckoning_tells_identical_rooms_apart(world):
    async with FakeMud() as mud:
        town = mud.play = Town(mud, world)
        bot, nav = await _navigator(mud, world)
        await nav.locate()
        assert (await nav.walk_to(3005)).reached and town.here == 3005
        assert (await nav.walk_to(3004)).reached and town.here == 3004
        await bot.quit()


async def test_lost_bot_recalls(world):
    async with FakeMud() as mud:
        town = mud.play = Town(mud, world, start=3005)
        bot, nav = await _navigator(mud, world)
        assert await nav.locate() == 3001  # 3004 or 3005: can't tell, so it recalls
        assert "recall" in town.commands
        await bot.quit()


async def test_rests_when_exhausted_and_avoids_danger(world):
    async with FakeMud() as mud:
        town = mud.play = Town(mud, world, move_cost=40)
        bot, nav = await _navigator(mud, world, avoid={3006})
        await nav.locate()
        assert nav.plan(3008) is None  # the only way passes the dangerous 3006
        r = await nav.walk_to(3005)
        assert r.reached and "sleep" in town.commands and "stand" in town.commands
        await bot.quit()


async def test_explores_an_area(world):
    async with FakeMud() as mud:
        mud.play = Town(mud, world)
        bot, nav = await _navigator(mud, world, avoid={3008})
        cov = await nav.explore("Town")
        assert cov.visited == {3001, 3002, 3003, 3004, 3005, 3006, 3007}
        assert cov.skipped == {3008: "dangerous"} and cov.share == pytest.approx(7 / 8)
        await bot.quit()


class TrippedFight(Town):
    """A fight in which the bot has been tripped: until it stands, flee and recall say it's resting."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.down, self.fighting = True, True

    def __call__(self, cmd: str):
        if cmd in ("flee", "recall") and self.down:
            self.commands.append(cmd)
            return "You're resting. Try standing up first!\n\r"
        if cmd == "stand":
            self.down = False
            self.commands.append(cmd)
            return "You stand up.\n\r"
        if cmd == "flee":
            self.commands.append(cmd)
            if not self.fighting:
                return "You aren't fighting anyone.\n\r"
            self.fighting, self.here = False, 3002
            return "You flee from combat!\n\r" + self.view()
        return super().__call__(cmd)


async def test_tripped_bot_stands_up_to_flee(world):
    async with FakeMud() as mud:
        town = mud.play = TrippedFight(mud, world, start=3006)
        bot, nav = await _navigator(mud, world)
        nav.here = 3006
        assert await nav.escape() == "fled"  # not "calm": being knocked down doesn't end the fight
        assert [c for c in town.commands if c != "autoloot"] == ["flee", "stand", "flee", "recall"]
        assert nav.here == 3001 and 3006 in nav.avoid
        await bot.quit()


class Ambush(Town):
    """The bot sat down to get its moves back and something attacked: it can't run (no moves), so it prays."""

    def __call__(self, cmd: str):
        if cmd == "sleep":
            self.commands.append(cmd)
            return "You are already fighting!\n\rThe small spider is in excellent condition.\n\r"
        if cmd == "wake":
            self.commands.append(cmd)
            return "You are already awake.\n\r"
        if cmd == "flee":
            self.commands.append(cmd)
            return "You are too exhausted.\n\rPANIC! You couldn't escape!\n\r"
        return super().__call__(cmd)


async def test_attacked_while_resting_it_escapes_even_without_moves(world):
    async with FakeMud() as mud:
        town = mud.play = Ambush(mud, world, start=3006)
        bot, nav = await _navigator(mud, world)
        nav.here = 3006
        await nav._rest_until()
        assert [c for c in town.commands if c != "autoloot"] == ["sleep", "wake", "stand", "flee", "recall"]
        assert nav.here == 3001 and 3006 in nav.avoid
        await bot.quit()
