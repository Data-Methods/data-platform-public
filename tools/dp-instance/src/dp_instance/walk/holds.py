"""A hold applied just in time: a step whose effect lasts until a later step of its component,
its `holds_until`, such as one that holds Dagster's runs until a `release`.

An apply never plans or applies a hold in its turn: it stands by. Each step it guards, every
later step of its component before the one its `holds_until` names, is planned in its turn, and
the first with a change asks once for that step and the hold together; on approval the hold
applies, then the step, and every later guarded step runs under the same hold. With no change in
the range, nothing is held. The question and the preview say what the hold does in the first
sentence of its own description. The hold's state is shared, and its component's walk recorded
part way, before it runs; it reports what it held when it says so. The step that lifts the hold
runs in its turn, with the hold's approval when its decision says it destroys nothing. A hold
that stopped with runs still going applies again when the run resumes, before the step it was
applied for, which keeps its approval; one that failed is named, never the step. Nothing here
names a component's steps: only `holds_until` does."""

from __future__ import annotations

import re

from ..components import Call
from ..outcome import WAITS, Stop
from ..record import Record
from ..screen import Block
from ..sequence import Entry
from .base import Base
from .common import STANDING_BY

# A hold's outcome in its turn, and its state in the record: being applied, which a stop leaves
# for a resume to apply again, or held until the step that lifts it.
HOLDING = "holding"
HELD = "held"
# Held when the run stopped: `release`, run by hand, may have lifted it since, so the next guarded
# change applies it again, which finds it on and only waits, or sets it again.
HELD_BEFORE = "held before the stop"
ON = (HOLDING, HELD, HELD_BEFORE)


def after_stop(record: Record) -> None:
    """A resumed apply can't tell whether a hold that was on is still on."""
    for state in (record.data.get("holds") or {}).values():
        if state.get("state") == HELD:
            state["state"] = HELD_BEFORE


def same_component(one: Entry, other: Entry) -> bool:
    return (one.repository, one.component) == (other.repository, other.component)


class Holds(Base):
    def holds(self) -> dict[str, dict]:
        """Each hold the run has reached, by its entry: its state, and the step it was applied for
        with the decisions approved for that step."""
        return self.record.data.setdefault("holds", {})

    def guarding(self, entry: Entry) -> Entry | None:
        """The hold that guards the entry: the nearest earlier step of its component with a
        `holds_until` that names no step up to the entry."""
        entries = self.record.entries
        at = entries.index(entry)
        for index in range(at - 1, -1, -1):
            item = entries[index]
            if not item.holds_until or not same_component(item, entry):
                continue
            lifted = any(
                later.step == item.holds_until and same_component(later, item)
                for later in entries[index + 1 : at + 1]
            )
            return None if lifted else item
        return None

    @staticmethod
    def hold_note(entry: Entry) -> str:
        """What the hold does, in the first sentence of its own description."""
        sentence = re.split(r"(?<=[.!?])\s", entry.description.strip(), maxsplit=1)[0]
        return sentence.rstrip(".") or f"{entry.step}, until {entry.holds_until}"

    def standing_by(self, entry: Entry) -> str:
        return (
            f"{self.hold_note(entry)}; applied just before the first step up to {entry.holds_until} "
            "with a change"
        )

    def stand_by(self, entry: Entry, block: Block) -> None:
        """A hold in its turn: it applies only for the first step it guards that has a change."""
        block.add("Standing By:", self.standing_by(entry))
        self.finish(entry, block, STANDING_BY, compact=True)

    def lifting(self, entry: Entry) -> Entry | None:
        """The hold this run applied that the entry lifts, its `holds_until`."""
        return next(
            (
                item
                for item in self.record.entries
                if item.holds_until == entry.step
                and same_component(item, entry)
                and self.holds().get(item.id, {}).get("state") in (HELD, HELD_BEFORE)
            ),
            None,
        )

    def apply_hold(self, hold: Entry, guarded: Entry, block: Block, approved: list[str]) -> None:
        """Apply the hold for the guarded step, whose approved decisions it keeps for a resume; a
        hold that stops, as with runs still going at its time limit, applies again on resume."""
        state = self.holds().setdefault(hold.id, {})
        before = dict(state)
        state.update(state=HOLDING, step=guarded.id, approved=list(dict.fromkeys(approved)))
        # The hold takes effect as it runs: its state is shared, and its component's walk part way,
        # before it does. An upload that fails leaves the record as it was: the hold never ran.
        self.record.save()
        try:
            self.record.publish()
            self.walk_started(hold)
        except Stop:
            state.clear()
            state.update(before)
            self.record.save()
            raise
        block.outcome = "holding"
        block.message = f"Applying {hold.component}'s {hold.step}, which holds until {hold.holds_until}"
        cli = self.cli(hold.repository, hold.command)
        call: Call = self.screen.run_live(
            block,
            cli.run,
            "apply",
            "--phase",
            hold.phase,
            "--step",
            hold.step,
            "--json",
            *self.version,
            on_line=self.messages(block),
        )
        block.message = ""
        if call.status != "done":
            if call.status in WAITS:
                self.wait(guarded, block, call)
            # The hold failed, not the step it was applied for.
            self.failure(hold, block, call)
        state["state"] = HELD
        events = call.finished(hold.step)
        # What the hold says it did, when it says: its own one line.
        summary = events[-1].get("summary") if events else None
        block.add(
            "Held:",
            f"{hold.component}'s {hold.step}, until {hold.holds_until}" + (f": {summary}" if summary else ""),
        )
        self.record.note(hold, "applied", call.seconds, summary=summary, held_for=guarded.id)
        self.applied_change(hold, block)

    def held_approvals(self, entry: Entry) -> list[str]:
        """The decisions approved for the step a hold was applied for, which a resume keeps."""
        return [
            identifier
            for state in self.holds().values()
            if state.get("step") == entry.id
            for identifier in state.get("approved") or []
        ]
