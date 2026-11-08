"""A run that stops: its entry's outcome, the commands that carry it on, and its record."""

from __future__ import annotations

from ..outcome import WAITING_FOR, Next, Result, Stop, naming_preflight
from ..record import Record
from ..sequence import Entry
from .base import Base
from .holds import HELD, HOLDING, ON


def held_on(record: Record, version: str) -> list[tuple[Entry, str]]:
    """Each hold a run leaves on: one that applied, or was applying when the run stopped, whose
    `holds_until` step hasn't finished; each with the command that lifts it, that step alone in
    its phase, once for each such step."""
    found: dict[str, Entry] = {}
    for entry in record.entries:
        state = (record.data.get("holds") or {}).get(entry.id, {}).get("state")
        if not entry.holds_until or state not in ON:
            continue
        lifted = f"{entry.repository}/{entry.component}/{entry.holds_until_phase}/{entry.holds_until}"
        if record.finished(lifted):
            continue
        command = (
            f"uv run --locked {entry.command} apply --phase {entry.holds_until_phase} "
            f"--step {entry.holds_until} --platform-version {version}"
        )
        found.setdefault(command, entry)
    return [(entry, command) for command, entry in found.items()]


class Stops(Base):
    def hold_lines(self) -> list[Next]:
        """A run that stops while a hold is on names, once for each step that lifts holds, the
        command that lifts them without carrying on."""
        return [
            Next(
                command,
                f"in {self.checkout(entry.repository)}: lifts {entry.component}'s hold without carrying on; "
                f"dp-instance resume carries on instead, holding again before the next step it guards "
                f"with a change, and lifts it at its {entry.holds_until} step",
            )
            for entry, command in held_on(self.record, self.manifest.platform_version)
        ]

    def step_alone(self, entry: Entry | None, reason: str) -> tuple[Next, ...]:
        """A run's failed step, by its own command in its repository, which a person applies
        alone only once its plan has been reviewed. A step a hold guards is never offered alone
        while the hold isn't on: the hold's own command comes first, and when the hold itself
        failed, only its command."""
        if reason != "failed" or entry is None or entry.kind != "step":
            return ()
        alone = Next(
            f"uv run --locked {entry.command} apply --phase {entry.phase} --step {entry.step} "
            f"{' '.join(self.version)}",
            f"in {self.checkout(entry.repository)}: only once the accountable person has reviewed its "
            "plan; then dp-instance resume",
        )
        hold = self.guarding(entry)
        state = self.holds().get(hold.id, {}).get("state") if hold is not None else None
        if hold is None or state == HELD:
            return (alone,)
        first = Next(
            f"uv run --locked {hold.command} apply --phase {hold.phase} --step {hold.step} "
            f"{' '.join(self.version)}",
            f"in {self.checkout(hold.repository)}: {hold.component}'s {hold.step}, which {entry.where} "
            f"needs on first: {self.hold_note(hold)}",
        )
        return (first,) if state == HOLDING else (first, alone)

    def beside(self, steps: tuple[Next, ...]) -> tuple[Next, ...]:
        """A prepare that ran beside the instance's stopped run is never resumed, which carries
        that run on: a new prepare starts it again, with `--no-preview` when this one had it."""
        found: list[Next] = []
        for item in steps:
            if item.command.startswith("dp-instance resume"):
                item = Next(self.restart(), "starts a new prepare, with your branch as it is then")
            if all(other.command != item.command for other in found):
                found.append(item)
        return tuple(found)

    def stopped(self, stop: Stop) -> Result:
        entry, block = self._entry, self._block
        reason = stop.detail.get("reason", stop.status)
        self.pause()
        if entry is not None and block is not None and block.live:
            outcome = {"failed": "failed", "decision": "stopped", **WAITING_FOR}[stop.status]
            outcome = {
                "interrupted": "interrupted",
                "drift": "drift",
                "unmapped": "not mapped",
            }.get(reason, outcome)
            # A step that stops has no finished event: the pull requests it reported as they
            # happened are its only report. The stop below uploads it, so a failed upload never
            # stands in for the stop.
            self.finish(entry, block, outcome, upload=False, pull_requests=self.pull_requests(block))
        next_steps = stop.next
        if reason == "transient" and not next_steps:
            # A read that failed for a moment inside a run, its retries spent: the run goes on
            # from here once the service answers.
            next_steps = (Next("dp-instance resume", "carries on from here once the service answers"),)
        if reason == "sign-in" and not any(item.command == "dp-instance resume" for item in next_steps):
            # A read that waits for a sign-in inside a run: the run goes on from here once signed in.
            next_steps = (*next_steps, Next("dp-instance resume", "carries on from here once signed in"))
        if self.record.kind == "plan":
            # A plan starts no run to resume: it's planned again.
            again = self.again()
            next_steps = (
                *(item for item in next_steps if item.command != "dp-instance resume"),
                *(item for item in again if item not in next_steps),
            )
        # A step's own command, alone, comes last.
        steps = (
            *naming_preflight(stop.status, reason, next_steps),
            *self.hold_lines(),
            *self.step_alone(entry, reason),
        )
        if self.record.data.get("beside"):
            steps = self.beside(steps)
        detail = {key: value for key, value in stop.detail.items() if key != "reason"}
        unshared = self.record.stop(
            stop.status,
            reason,
            stop.message,
            entry=entry.id if entry is not None else None,
            next=[item.describe() for item in steps],
            **detail,
        )
        style = {"failed": "bold red", "decision": "bold yellow"}.get(stop.status, "bold orange1")
        self.screen.blank()
        self.screen.note(stop.message, style)
        if unshared:
            self.screen.note(f"Only this workstation records the stop. {unshared}", "bold orange1")
        self.screen.continuing(steps, self.relative(self.record.folder) + "/")
        return Result(
            self.record.kind,
            stop.status,
            stop.message,
            tuple(steps),
            {
                "run": self.record.id,
                "reason": reason,
                **detail,
                **({"unshared": unshared} if unshared else {}),
            },
        )
