"""A live record of a trial as it runs: one JSON line per step in ``<run folder>/progress.jsonl`` and a short line on
stderr, flushed at once. A long trial can be checked in seconds (``tail runs/<run>/progress.jsonl``), and a run
that died shows where it stopped. ``Phases`` adds up where a bot's time went (explore, fight, rest, trade...).
"""

from __future__ import annotations

import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path


class Heartbeat:
    def __init__(self, folder: Path, trial: str, quiet: bool = False):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "progress.jsonl"
        self.trial, self.quiet = trial, quiet
        self.t0 = time.monotonic()

    def __call__(self, who: str, what: str, **data) -> None:
        t = round(time.monotonic() - self.t0)
        line = {"t": t, "at": time.strftime("%H:%M:%S"), "who": who, "what": what, **data}
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, default=str) + "\n")
        except OSError:
            pass  # a heartbeat must never stop a trial
        if not self.quiet:
            extra = " ".join(f"{k}={v}" for k, v in data.items() if not isinstance(v, (dict, list)))
            print(f"[{t // 60:02d}:{t % 60:02d}] {self.trial} {who}: {what}" + (f" ({extra})" if extra else ""),
                  file=sys.stderr, flush=True)


def null_beat(who: str, what: str, **data) -> None:
    """A heartbeat that records nothing (for callers that don't pass one)."""


class Phases:
    """Seconds spent per phase: ``with phases("explore"): ...`` (nested phases count in the outer one too)."""

    def __init__(self):
        self.seconds: dict[str, float] = {}

    @contextmanager
    def __call__(self, name: str):
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.seconds[name] = self.seconds.get(name, 0.0) + time.monotonic() - t0

    def rounded(self) -> dict[str, int]:
        return {k: round(v) for k, v in self.seconds.items()}
