"""A run: the walk over its entries, part by part, and its end. A prepare and an apply each
build their entries and end their own way; both walk them here."""

from __future__ import annotations

from ..outcome import Result, Stop
from ..sequence import Entry
from .checks import Checks
from .code import Code
from .common import LEFT_OPEN, adoption_left, part_of
from .holds import Holds
from .progress import Progress
from .reading import Reading
from .skip import Skip
from .sources import Sources
from .steps import Steps
from .stops import Stops
from .waits import Waits


class Run(Progress, Sources, Reading, Checks, Code, Skip, Holds, Steps, Waits, Stops):
    def walk(self) -> Result:
        self.header()
        try:
            self.go()
        except KeyboardInterrupt:
            return self.stopped(Stop("failed", "Interrupted.", reason="interrupted"))
        except Stop as stop:
            return self.stopped(stop)
        self.pause()
        return self.end()

    def go(self) -> None:
        """Build the run's entries, when it's new, and walk them."""
        raise NotImplementedError

    def end(self) -> Result:
        """The run completed: its record and its result."""
        raise NotImplementedError

    def handlers(self) -> dict:
        return {"drift": self.drift_check, "upgrade": self.step, "migrate": self.step, "step": self.step}

    def walk_entries(self) -> None:
        """Walk the entries not yet walked in this command, in order, each part ending with a
        line of its time."""
        handlers = self.handlers()
        for index, entry in enumerate(self.record.entries, 1):
            if part_of(entry) != self._part:
                self.close_part()
                self._part = part_of(entry)
            if entry.id in self._walked:
                continue
            self._position = index
            if entry.kind == "preflight":
                self.replay_checks()
                self.preflights([item for item in self.record.entries if item.kind == "preflight"])
                continue
            if self.record.finished(entry.id) or (entry.kind == "preview" and self.record.outcome(entry.id)):
                self.replay(index - 1)
                continue
            if entry.kind == "preview":
                self.preview_group(entry)
                continue
            self.enter(entry)
            self._walked.add(entry.id)
            self.count(entry)
            self._pulls = {}
            block = self.block(entry)
            self._entry, self._block = entry, block
            handlers[entry.kind](entry, block)
            self._entry = self._block = None
            self.entry_done(entry)
        self.close_part()

    def preview_group(self, entry: Entry) -> None:
        """A prepare plans its preview a phase at a time."""
        raise NotImplementedError

    def show_adoption(self, adoption: list[dict]) -> None:
        """Each Adoption PR the run leaves open, and what it waits for."""
        self.screen.blank()
        for item in adoption:
            shown = f"{item['url']} ({item['state']})" if item["state"] else item["url"]
            self.screen.labelled("Adoption PR:", f"{shown}: {adoption_left(item)} it")
        self.screen.labelled("Left Open:", LEFT_OPEN)
