"""What every command shares: its options, the screen, the instance and the releases."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from . import installed
from .instance import Instance
from .manifest import Releases
from .screen import Screen


def terminal() -> bool:
    """Whether a person is at this command: both stdin and stdout are a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


@dataclass(frozen=True)
class Options:
    json: bool = False
    auto_approve: bool = False
    approvals: tuple[str, ...] = ()
    change: str | None = None


class Session:
    def __init__(self, options: Options, start: Path, *, screen: Screen | None = None):
        self.options = options
        self.screen = screen or Screen(json_mode=options.json, interactive=terminal())
        self.instance = Instance.find(start)
        self.instance.check()
        self.releases = Releases(self.instance.state / "releases.git")

    @property
    def interactive(self) -> bool:
        return self.screen.interactive

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.instance.root).as_posix()
        except ValueError:
            return str(path)

    @staticmethod
    def own_commit() -> str | None:
        return installed.commit()
