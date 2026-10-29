"""A run and its record, in `.dp-instance/runs/<run>/`.

A run is one journey through the whole sequence. Its record holds the manifest it runs, the
commits it's tied to, each step's outcome as it happens with the pull requests the step reports,
where it stopped and why, its log, its plan files and its handoff pages. `status` reads it
without planning anything, and `resume` carries on from where it stopped. The record never
stands in for a plan: each step is still planned right before it's applied.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .outcome import failed
from .sequence import Entry

ACTIVE = ("running", "stopped", "failed")
# A new upgrade that stopped reading the steps while another run had stopped: it replaced
# nothing and never started, so it isn't one of the workstation's runs, and the run that
# stopped stays the active one.
NOT_STARTED = "not started"
# Windows refuses to replace a file another program has open, such as a reader of the record or
# an antivirus scan of a file just written; that passes within moments.
REPLACE_TRIES = 20
REPLACE_WAIT = 0.1
# Outcomes after which a resumed run passes a step without running it again.
FINISHED = ("applied", "no change", "passed", "no drift", "approved", "skipped")


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

    @classmethod
    def new(cls, runs: Path, kind: str, target: str | None, goal: str, **extra) -> Record:
        runs.mkdir(parents=True, exist_ok=True)
        name = identifier(runs, kind, target)
        folder = runs / name
        folder.mkdir()
        record = cls(
            folder,
            {
                "schema_version": 1,
                "id": name,
                "kind": kind,
                "target": target,
                "goal": goal,
                "started": now(),
                "updated": now(),
                "status": "running",
                "commits": {},
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
        return cls(folder, json.loads((folder / "run.json").read_text(encoding="utf-8")))

    def save(self) -> None:
        self.data["updated"] = now()
        path = self.folder / "run.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        replace(temporary, path)

    def note(self, entry: Entry, outcome: str, seconds: float, **detail) -> None:
        self.data["steps"][entry.id] = {
            "outcome": outcome,
            "seconds": round(seconds, 1),
            "at": now(),
            **{key: value for key, value in detail.items() if value not in (None, [], {})},
        }
        self.data["position"] = entry.id
        self.save()

    def stop(self, status: str, reason: str, message: str, **detail) -> None:
        self.data["status"] = "failed" if status == "failed" else "stopped"
        self.data["stop"] = {"reason": reason, "message": message, "at": now(), **detail}
        self.save()

    def complete(self) -> None:
        self.data["status"], self.data["stop"] = "complete", None
        self.save()

    def file(self, *parts: str) -> Path:
        path = self.folder.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


def runs_folder(state: Path) -> Path:
    return state / "runs"


def records(state: Path, kinds: tuple[str, ...] = ("upgrade", "apply")) -> list[Record]:
    """This workstation's runs, newest first; a new upgrade that never started isn't one."""
    folder = runs_folder(state)
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.iterdir(), reverse=True):
        if (path / "run.json").is_file():
            try:
                record = Record.load(path)
            except (OSError, ValueError):
                continue
            if record.data.get("kind") in kinds and record.status != NOT_STARTED:
                found.append(record)
    return found


def active(state: Path) -> Record | None:
    """The run under way or stopped: the newest run, unless it completed. A new run replaces
    the one before it, an upgrade once it has read the steps, so only the newest can be
    active."""
    found = records(state)
    return found[0] if found and found[0].status in ACTIVE else None


def under_way(state: Path, identifier: str) -> Record | None:
    """The run, while it's under way or stopped."""
    try:
        found = Record.load(runs_folder(state) / identifier)
    except (OSError, ValueError):
        return None
    return found if found.status in ACTIVE else None


def mark_replaced(state: Path, identifier: str) -> None:
    """Mark the run replaced, if it's under way or stopped, as the run after it starts."""
    found = under_way(state, identifier)
    if found is not None:
        found.data["status"] = "replaced"
        found.save()


def replace_active(state: Path) -> None:
    """Mark the active run replaced, as a new run starts."""
    previous = active(state)
    if previous is not None:
        previous.data["status"] = "replaced"
        previous.save()
