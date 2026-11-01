"""What every command shares: its options, the screen, the instance and the releases.

Every command that finds an instance first refuses one older than this dp-instance upgrades
from, then moves a repository map from before Platform 2026.5.0 into `.dp-instance/`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from . import gitops, installed, targets, template_access
from .instance import (
    EARLIER_MAP_FILE,
    INFRASTRUCTURE,
    MAP_FILE,
    Instance,
    move_earlier_map,
    state_folder,
    unmapped_infrastructure,
)
from .manifest import Releases
from .outcome import Stop
from .screen import Screen


def terminal() -> bool:
    """Whether a person is at this command: both stdin and stdout are a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def releases_of(root: Path) -> Releases:
    """The published releases, cached in the instance folder's `.dp-instance/releases.git`."""
    return Releases(state_folder(root) / "releases.git")


def move_map(root: Path, screen: Screen) -> None:
    """Move a repository map from before Platform 2026.5.0 into `.dp-instance/`, saying so.
    Removed at the 2026.6.0 stop."""
    if move_earlier_map(root):
        screen.note(f"Moved {EARLIER_MAP_FILE} into {MAP_FILE.as_posix()}")
        screen.emit({"event": "moved", "from": EARLIER_MAP_FILE, "to": MAP_FILE.as_posix()})


def ready(instance: Instance, screen: Screen) -> Releases:
    """A found instance's first steps: refuse it when it's older than this dp-instance upgrades
    from, then move its map in."""
    releases = releases_of(instance.root)
    targets.refuse_older({key: item.path for key, item in instance.components.items()}, releases)
    move_map(instance.root, screen)
    return releases


def ready_for_template_access(start: Path, screen: Screen) -> None:
    """The same first steps for `template-access`, which also runs before the instance is mapped:
    on the instance a map it reads names or, without one, on the Client Infrastructure Repository
    checkout it runs in, whose folder holds the release cache."""
    root = Instance.locate(start)
    instance = None
    if root is not None:
        try:
            instance = Instance.load(root)
        except Stop:
            instance = None
    if instance is not None:
        ready(instance, screen)
        return
    checkout = unmapped_infrastructure(start.resolve())
    if checkout is not None:
        targets.refuse_older({INFRASTRUCTURE: checkout}, releases_of(checkout.parent))


@dataclass(frozen=True)
class Options:
    json: bool = False
    auto_approve: bool = False
    approvals: tuple[str, ...] = ()


class Session:
    def __init__(self, options: Options, start: Path, *, screen: Screen | None = None):
        self.options = options
        self.screen = screen or Screen(json_mode=options.json, interactive=terminal())
        self.instance = Instance.find(start)
        self.instance.check()
        # The instance's template access reaches the Git commands and component CLIs it starts.
        gitops.pass_configuration(template_access.rewrites(self.instance.key, self.instance))
        self.releases = ready(self.instance, self.screen)

    @property
    def interactive(self) -> bool:
        return self.screen.interactive

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.instance.root).as_posix()
        except ValueError:
            return str(path)

    @staticmethod
    def own_version() -> str | None:
        return installed.version()
