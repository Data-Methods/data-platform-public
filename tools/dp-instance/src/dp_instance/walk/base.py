"""What a run is: its session, record and manifest, the repositories it walks, the copy of each
it works in, their component CLIs, and the Platform Version it passes them."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from ..components import Cli, Log
from ..manifest import Manifest
from ..outcome import Next
from ..record import Record
from ..screen import Block
from ..sequence import Entry
from ..session import Session


class Base:
    def __init__(self, session: Session, record: Record, manifest: Manifest, places: dict[str, Path]):
        self.session = session
        self.screen = session.screen
        self.instance = session.instance
        self.options = session.options
        self.record = record
        self.manifest = manifest
        # Each repository's copy the run works in: never the checkout, which is only an input.
        self.places = places
        self.log = Log(record.log_path)
        mapped = self.instance.components
        self.repositories = tuple(item for item in manifest.order if item.key in mapped)
        self.unlisted = sorted(key for key in mapped if manifest.repository(key) is None)
        self.absent = [item.key for item in manifest.order if item.key not in mapped]
        self.started = time.monotonic()
        # The time this command's prompts had waited for answers as the run began walking.
        self._waited = self.screen.waited
        self._shown: tuple | None = None
        # Each part's steps that changed nothing, and the repositories it showed a step of.
        self._quiet: dict[str, int] = {}
        self._showing: dict[str, set[str]] = {}
        self._asked_by: str | None = None
        self._position = 0
        self._walked: set[str] = set()
        # The steps a batched skip has already found unchanged, by entry, with their events.
        self._skipped: dict[str, dict] = {}
        self._notes_shown = False
        # Each step's inputs hash as an apply last left it, once read.
        self._hashes: dict[str, str] | None = None
        # One fetch at a time into each repository's own folder, every repository at once.
        self._fetching = {item.key: threading.Lock() for item in self.repositories}
        # The entry under way and its block, which a stop finishes.
        self._entry: Entry | None = None
        self._block: Block | None = None
        # The progress the footer shows for the entry under way; None while reading, which has
        # no count.
        self._progress: str | None = None
        # The part being walked, which ends with a line of its time, and those that have.
        self._part: str | None = None
        self._closed: set[str] = set()
        # Each part's start in this command, as it was first shown, and the time this command's
        # prompts had waited for answers by then.
        self._clock: dict[str, tuple[float, float]] = {}
        # Each pull request the entry under way reported, with its last state, by URL.
        self._pulls: dict[str, str | None] = {}

    @property
    def channel(self) -> str | None:
        return self.record.data.get("channel")

    @property
    def change(self) -> str | None:
        """The change a prepare's code steps commit on: it names the branch."""
        return self.record.data.get("change")

    def checkout(self, repository: str) -> Path:
        return self.places[repository]

    def cli(self, repository: str, command: str, *, fetched: bool = False) -> Cli:
        project = self.source(repository) if fetched else None
        return Cli(command, self.checkout(repository), self.log, project)

    def cli_for(self, entry: Entry) -> Cli:
        return self.cli(entry.repository, entry.command, fetched=entry.fetched)

    def source(self, repository: str) -> Path:
        """The owner's source at the target, which only a prepared upgrade fetches."""
        raise NotImplementedError

    def messages(self, block: Block):
        """Show a component's latest line for a person under its live block."""

        def show(line: str) -> None:
            block.message = line

        return show

    def relative(self, path: Path) -> str:
        return self.session.relative(path)

    @property
    def version(self) -> list[str]:
        """The Platform Version the run's manifest names, as it names it, which `preflight`,
        `plan`, `apply` and `verify` take."""
        return ["--platform-version", self.manifest.platform_version]

    @property
    def command(self) -> str:
        """The command that started the run, without `--no-preview`, which its record keeps
        apart."""
        return self.record.data.get("command") or f"dp-instance {self.record.kind}"

    def restart(self) -> str:
        """The command that starts this kind of run again: a prepare's with `--no-preview` when
        it had it."""
        return f"{self.command} --no-preview" if self.record.data.get("no_preview") else self.command

    def again(self) -> tuple[Next, ...]:
        """A new run in place of resuming this one: an apply or `prepare --to`, which its first
        question replaces; a plain prepare, which runs beside it and asks nothing."""
        if self.record.kind == "plan":
            return (Next(self.restart(), "plans again, as it starts no run to resume"),)
        if self.command == "dp-instance prepare":
            return (
                Next(self.restart(), "starts a new prepare beside this one, with your branch as it is then"),
            )
        return (Next(self.restart(), "starts a new run in place of this one, once asked"),)

    def walk_started(self, entry: Entry) -> None:
        """An apply records the step's component as part way at its first applied step."""

    def entry_done(self, entry: Entry) -> None:
        """An apply records a component's walk complete once its last entry finishes."""
