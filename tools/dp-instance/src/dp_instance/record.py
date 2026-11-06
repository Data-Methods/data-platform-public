"""A run and its record, in `.dp-instance/runs/<run>/`.

A run is one journey through the whole sequence. Its record holds the manifest it runs, the
commits it's tied to, who started it and who resumed it, each step's outcome as it happens with
the pull requests the step reports and who walked it, where it stopped and why, its log, its
plan files and its handoff pages. A prepare's or an apply's record is the
working copy of the one in the instance's shared run record, which it uploads to at every step
boundary: a failed upload stops the run, for `resume`. `status` reads it without planning
anything, and `resume` carries on from where it stopped. The record never stands in for a
plan: each step is still planned right before it's applied.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from . import installed
from .outcome import Next, Stop, failed
from .sequence import Entry

if TYPE_CHECKING:
    from .shared import Shared

ACTIVE = ("running", "stopped", "failed")
# A new run that stopped reading the steps while another run had stopped: it replaced
# nothing and never started, so it isn't one of the instance's runs, and the run that stopped
# stays the unfinished one.
NOT_STARTED = "not started"
# 2 is the walk of prepare then apply; a record at 1 is an earlier walk's, which only the
# dp-instance before it carries on.
SCHEMA_VERSION = 2
# The runs this walk walks, and an earlier walk's.
KINDS = ("prepare", "apply")
EARLIER_KINDS = ("upgrade",)
# Windows refuses to replace a file another program has open, such as a reader of the record or
# an antivirus scan of a file just written; that passes within moments.
REPLACE_TRIES = 20
REPLACE_WAIT = 0.1
# Outcomes after which a resumed run passes a step without running it again.
FINISHED = ("applied", "no change", "passed", "no drift", "approved", "skipped", "standing by")


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def replace(source: Path, destination: Path) -> None:
    """Replace ``destination`` with ``source``, waiting out another program briefly holding it
    open, as Windows refuses the replace meanwhile."""
    for attempt in range(REPLACE_TRIES):
        try:
            source.replace(destination)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise failed(
                    f"Couldn't save {destination}: another program kept it open. Close it, then "
                    "run the command again."
                ) from None
            time.sleep(REPLACE_WAIT)


def remove(path: Path) -> None:
    """Remove a file, waiting out another program briefly holding it open, as Windows refuses the
    removal meanwhile."""
    for attempt in range(REPLACE_TRIES):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise failed(
                    f"Couldn't remove {path}: another program kept it open. Close it, then run the "
                    "command again."
                ) from None
            time.sleep(REPLACE_WAIT)


def identifier(runs: Path, kind: str, target: str | None) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{kind}" + (f"-{target}" if target else "")
    name, suffix = base, 1
    while (runs / name).exists():
        suffix += 1
        name = f"{base}-{suffix}"
    return name


@dataclass
class Record:
    folder: Path
    data: dict = field(default_factory=dict)
    # The instance's shared run record, which a run uploads to at each step boundary while its
    # command holds the run lock; before the state account exists, what joins it there once a
    # step has created it; and who runs that command. None is part of the record.
    shared: Shared | None = field(default=None, repr=False, compare=False)
    join: Callable[[Record], None] | None = field(default=None, repr=False, compare=False)
    by: str | None = field(default=None, repr=False, compare=False)

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def kind(self) -> str:
        return self.data["kind"]

    @property
    def status(self) -> str:
        return self.data["status"]

    @property
    def manifest_path(self) -> Path:
        return self.folder / "manifest.json"

    @property
    def log_path(self) -> Path:
        return self.folder / "log.txt"

    @property
    def entries(self) -> list[Entry]:
        return [Entry.load(item) for item in self.data.get("entries", [])]

    @entries.setter
    def entries(self, entries: list[Entry]) -> None:
        self.data["entries"] = [entry.describe() for entry in entries]

    def outcome(self, entry_id: str) -> dict | None:
        return self.data["steps"].get(entry_id)

    def finished(self, entry_id: str) -> bool:
        noted = self.outcome(entry_id)
        return bool(noted) and noted.get("outcome") in FINISHED

    def stopped_at(self) -> str:
        """Where the run stopped and why, as its record says; a run whose command ended without
        recording a stop, such as one that crashed or whose workstation restarted, says so."""
        stop = self.data.get("stop") or {}
        if not stop:
            return "stopped without saying why, such as after a crash or a restart"
        where = next((entry.where for entry in self.entries if entry.id == stop.get("entry")), "the start")
        return f"stopped at {where}: {stop.get('message') or stop.get('reason')}"

    @classmethod
    def new(cls, runs: Path, kind: str, target: str | None, goal: str, **extra) -> Record:
        runs.mkdir(parents=True, exist_ok=True)
        name = identifier(runs, kind, target)
        folder = runs / name
        folder.mkdir()
        record = cls(
            folder,
            {
                "schema_version": SCHEMA_VERSION,
                "id": name,
                "kind": kind,
                "target": target,
                "goal": goal,
                # The dp-instance that started it: a record without one is from a dp-instance
                # before `self update`, which only such a dp-instance carries on.
                "dp_instance": installed.version(),
                "started": now(),
                "updated": now(),
                "status": "running",
                "entries": [],
                "steps": {},
                "stop": None,
                **extra,
            },
        )
        record.save()
        return record

    @classmethod
    def load(cls, folder: Path) -> Record:
        data = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("schema_version"), int):
            raise ValueError(f"{folder / 'run.json'} isn't a run record")
        if data["schema_version"] > SCHEMA_VERSION:
            raise Stop(
                "failed",
                f"Run {folder.name}'s record is at schema version {data['schema_version']}, which "
                "this dp-instance doesn't read: a newer dp-instance wrote it.",
                next=(Next("dp-instance self update", "installs the newest dp-instance, which reads it"),),
            )
        return cls(folder, data)

    def save(self) -> None:
        self.data["updated"] = now()
        path = self.folder / "run.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        replace(temporary, path)

    def note(self, entry: Entry, outcome: str, seconds: float, *, upload: bool = True, **detail) -> None:
        """An entry's outcome, a step boundary: saved, then uploaded before the next step, unless
        the run is stopping, whose stop makes the upload."""
        self.data["steps"][entry.id] = {
            "outcome": outcome,
            "seconds": round(seconds, 1),
            "at": now(),
            **{key: value for key, value in detail.items() if value not in (None, [], {})},
            **({"by": self.by} if self.by else {}),
        }
        self.save()
        if upload:
            self.publish(applied=outcome == "applied")

    def stop(self, status: str, reason: str, message: str, **detail) -> str | None:
        """Where and why the run stopped, saved, then uploaded. An upload that fails leaves the
        shared copy at the last step boundary, which the log says: what failed is returned,
        and the stop stands."""
        self.data["status"] = "failed" if status == "failed" else "stopped"
        self.data["stop"] = {
            "reason": reason,
            "message": message,
            "at": now(),
            **detail,
            **({"by": self.by} if self.by else {}),
        }
        self.save()
        try:
            self.publish()
        except Stop as stop:
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    f"== {now()} the shared run record keeps the last step boundary: {stop.message}\n"
                )
            return stop.message
        return None

    def complete(self) -> None:
        """The run completed: saved, uploaded, and no longer the instance's unfinished run."""
        self.data["status"], self.data["stop"] = "complete", None
        self.save()
        self.publish()
        if self.shared is not None:
            try:
                self.shared.finished(self)
            except Stop as stop:
                raise Stop(
                    "failed",
                    f"{stop.message} The run completed.",
                    reason="shared-record",
                    next=(Next("dp-instance resume", "finishes recording that the run completed"),),
                ) from None

    def publish(self, *, applied: bool = False) -> None:
        """Upload the record to the instance's shared run record, when it's a run that has one;
        before the state account exists, after a step ``applied``, look for the account, which
        only a step that applied can have created."""
        if self.shared is not None:
            self.shared.publish(self)
        elif self.join is not None and applied:
            self.join(self)

    def file(self, *parts: str) -> Path:
        path = self.folder.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


def runs_folder(state: Path) -> Path:
    return state / "runs"


def records(state: Path, kinds: tuple[str, ...] = KINDS) -> list[Record]:
    """This workstation's runs, newest first; a new run that never started isn't one."""
    folder = runs_folder(state)
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.iterdir(), reverse=True):
        if (path / "run.json").is_file():
            try:
                record = Record.load(path)
            except (OSError, ValueError, Stop):
                continue
            if record.data.get("kind") in kinds and record.status != NOT_STARTED:
                found.append(record)
    return found


def active(state: Path, kinds: tuple[str, ...] = KINDS) -> Record | None:
    """This workstation's run under way or stopped: the newest run, unless it completed. A new
    run replaces the one before it, so only the newest can be active."""
    found = records(state, kinds)
    return found[0] if found and found[0].status in ACTIVE else None


def under_way(state: Path, identifier: str) -> Record | None:
    """The run, while it's under way or stopped."""
    try:
        found = Record.load(runs_folder(state) / identifier)
    except (OSError, ValueError, Stop):
        return None
    return found if found.status in ACTIVE else None
