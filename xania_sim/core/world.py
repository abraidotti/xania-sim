"""The static world, from Xania Explorer, loaded from the same checkout the test MUD runs."""

from __future__ import annotations

from functools import lru_cache

from xania_sim.core.server import checkout_path


@lru_cache(maxsize=None)
def load(path: str | None = None):
    """An Explorer World for ``path`` (default: the test MUD's checkout). Needs xania-explorer installed."""
    try:
        from xania_explorer import api
    except ImportError as e:  # pragma: no cover
        raise ImportError("xania-sim needs xania-explorer: pip install -e ../xania-explorer") from e
    return api.load_world(path or checkout_path())


def area_namer(world):
    """vnum -> the name of the area the room is in (None if unknown), for events.Recorder."""
    def name(vnum: int | None) -> str | None:
        a = world.room_area.get(vnum) if vnum is not None else None
        return a.name if a is not None else None
    return name
