"""The game's git repository: the one place the sim learns about the game from, and the version it's at.

The sim never builds or changes the repo. It records the commit (and whether there are uncommitted changes), so
everything bots learn can be tagged with the code it was learned on, and it warns when the built game is older
than its source, in which case the running MUD doesn't match what the sim read.

Settings: ``XANIA_SIM_XANIA`` (the repo as a Linux path, default ``$HOME/xania``) and, on Windows,
``XANIA_SIM_DISTRO`` (see server.py). The repo is read from Windows through ``\\\\wsl.localhost\\...``.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from xania_sim.core.server import XANIA, checkout_path, run_bash


@dataclass(frozen=True)
class Version:
    commit: str
    dirty: bool  # uncommitted changes to tracked files
    commit_time: int  # unix seconds

    @property
    def label(self) -> str:
        return self.commit[:10] + ("+dirty" if self.dirty else "")


@dataclass(frozen=True)
class BuildCheck:
    built: bool
    stale: bool
    reason: str = ""


class GameRepo:
    def __init__(self, linux_path: str = XANIA):
        self.linux_path = linux_path

    @cached_property
    def path(self) -> Path:
        """The repo as a path this Python can read."""
        return Path(checkout_path() if self.linux_path == XANIA else self.linux_path)

    @property
    def src_dir(self) -> Path:
        return self.path / "src"

    def _git(self, args: str) -> str:
        return run_bash(f'cd "{self.linux_path}" && git {args}').strip()

    def version(self) -> Version:
        commit, when = self._git("log -1 --format='%H %ct'").split()
        dirty = bool(self._git("status --porcelain --untracked-files=no"))
        return Version(commit, dirty, int(when))

    def build_check(self) -> BuildCheck:
        """Is ``install/bin/xania`` newer than the last commit and every uncommitted change under src/ and area/?"""
        out = run_bash(f"""
            cd "{self.linux_path}" || exit 0
            bin=install/bin/xania
            test -x "$bin" || {{ echo NOBUILD; exit 0; }}
            built=$(stat -c %Y "$bin")
            echo BUILT $built
            echo COMMIT $(git log -1 --format=%ct)
            git status --porcelain --untracked-files=no -- src area | cut -c4- | while read f; do
                test -e "$f" && echo CHANGED $(stat -c %Y "$f") "$f"
            done
        """, check=False)
        if "NOBUILD" in out:
            return BuildCheck(False, True, "the game isn't built (run make in the repo)")
        built = commit = 0
        newer = []
        for line in out.splitlines():
            kind, _, rest = line.partition(" ")
            if kind == "BUILT":
                built = int(rest)
            elif kind == "COMMIT":
                commit = int(rest)
            elif kind == "CHANGED":
                when, _, name = rest.partition(" ")
                if int(when) > built:
                    newer.append(name)
        if commit > built:
            return BuildCheck(True, True, "the last commit is newer than the build (run make in the repo)")
        if newer:
            return BuildCheck(True, True, f"{len(newer)} changed file(s) are newer than the build, e.g. {newer[0]}")
        return BuildCheck(True, False)
