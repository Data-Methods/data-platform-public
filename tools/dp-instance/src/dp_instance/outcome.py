"""Result statuses, exit codes and the stop every command can end with.

The statuses and exit codes are the components' own (the engine's common interface), so a
person or an agent reads `dp-instance` the way it reads any component. Every stop names what it
waits for: 0 done, 1 failed, 2 a usage error, 3 an external handoff, 4 values only a person
supplies, 5 a decision, 6 a sign-in to renew, 7 runs a step waits out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

EXIT_CODES = {"done": 0, "failed": 1, "handoff": 3, "values": 4, "decision": 5, "sign-in": 6, "runs": 7}
USAGE = 2
# The stops for what a run waits on besides a decision or an external handoff.
WAITS = ("values", "sign-in", "runs")
# What a step's line says while the run waits on each stop.
WAITING_FOR = {
    "handoff": "waiting: handoff",
    "values": "waiting: values",
    "sign-in": "waiting: a sign-in",
    "runs": "waiting: runs to finish",
}


@dataclass(frozen=True)
class Next:
    """One command that continues, and why."""

    command: str
    why: str

    def describe(self) -> dict:
        return {"command": self.command, "why": self.why}


class Stop(Exception):
    """A command ends early: ``status`` says what's needed, ``next`` how to continue."""

    def __init__(self, status: str, message: str, *, next: tuple[Next, ...] = (), **detail):
        super().__init__(message)
        if status not in EXIT_CODES or status == "done":
            raise ValueError(f"Not a stop status: {status}")
        self.status = status
        self.message = message
        self.next = tuple(next)
        self.detail = detail

    @property
    def code(self) -> int:
        return EXIT_CODES[self.status]


def failed(message: str, *next: Next) -> Stop:
    return Stop("failed", message, next=next)


@dataclass
class Result:
    """What a command ends with, for the screen and for `--json`."""

    command: str
    status: str = "done"
    message: str | None = None
    next: tuple[Next, ...] = ()
    detail: dict = field(default_factory=dict)

    @property
    def code(self) -> int:
        return EXIT_CODES[self.status]

    def describe(self) -> dict:
        return {
            "event": "result",
            "schema_version": 1,
            "command": self.command,
            "status": self.status,
            "message": self.message,
            "next": [item.describe() for item in self.next],
            **self.detail,
        }
