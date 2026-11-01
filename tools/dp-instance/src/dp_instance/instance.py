"""The instance folder and `.dp-instance/`, which holds the repository map, `repositories.json`.

`dp-instance setup` writes the map: the Platform Instance Key and each repository's remote
and path. Every other command finds the map from the current folder or one above it, and
checks it against the checkouts before doing anything else. Before Platform 2026.5.0 the map
was `dp-instance.json`, beside `.dp-instance/`: the first command that finds it there moves it
in, unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import gitops
from .outcome import Next, Stop, failed
from .sequence import MERGED_COMMIT

STATE_FOLDER = ".dp-instance"
MAP_FILE = Path(STATE_FOLDER, "repositories.json")
# The map's place before Platform 2026.5.0. Removed at the 2026.6.0 stop, with the move.
EARLIER_MAP_FILE = "dp-instance.json"
SOURCE_RECORD = Path(".dp-template/template-source.json")
# The Client Infrastructure Repository's template, and its owner's CLI, which reads the
# instance's published Component Outputs.
INFRASTRUCTURE = "data-platform-infrastructure"
INFRASTRUCTURE_COMMAND = "dp-infrastructure"
SETUP = Next("dp-instance setup", "maps this instance's repositories again")


@dataclass(frozen=True)
class SourceRecord:
    """A Component Repository's `.dp-template/template-source.json`."""

    template: str
    repository: str
    clone_url: str
    channel: str
    version: str | None
    commit: str

    @classmethod
    def read(cls, checkout: Path, revision: str | None = None) -> SourceRecord | None:
        """The record in the checkout, or at ``revision`` of its repository."""
        path = checkout / SOURCE_RECORD
        if revision is not None:
            text = gitops.show(checkout, revision, SOURCE_RECORD.as_posix())
        elif path.is_file():
            text = path.read_text(encoding="utf-8")
        else:
            text = None
        if text is None:
            return None
        try:
            data = json.loads(text)
            return cls(
                data["template_repository_key"],
                data["repository"],
                data["clone_url"],
                data["channel"],
                data.get("version"),
                data["commit"],
            )
        except (ValueError, KeyError, TypeError):
            raise failed(f"{path} isn't a template source record dp-instance reads.") from None

    @property
    def release_url(self) -> str:
        """The clone URL of the template's release repository."""
        if self.repository.endswith(gitops.DEVELOPMENT_SUFFIX):
            return gitops.counterpart_url(self.clone_url, development=False)
        return self.clone_url

    @property
    def development_url(self) -> str:
        if self.repository.endswith(gitops.DEVELOPMENT_SUFFIX):
            return self.clone_url
        return gitops.counterpart_url(self.clone_url, development=True)

    def describe(self) -> str:
        at = f"Template Version {self.version}" if self.version else f"commit {self.commit[:12]}"
        return f"{self.repository} {at} ({self.channel})"


@dataclass(frozen=True)
class Mapped:
    """One mapped repository: a Component Repository by its template key, or a Managed
    Repository by the component that maintains it."""

    path: Path
    stored: str
    remote: str
    template: str | None = None
    managed_by: str | None = None

    @property
    def name(self) -> str:
        return self.template or gitops.repository_name(self.remote)

    def describe(self) -> dict:
        found = {"template": self.template} if self.template else {"managed_by": self.managed_by}
        return {**found, "remote": self.remote, "path": self.stored}


def registrations(aggregate: dict) -> list[tuple[str, str]]:
    """Each component the instance registers in its Component Outputs, as `outputs aggregate`
    prints them: its key and the repository its registration names."""
    found = []
    for item in aggregate.get("components", []):
        registration = item.get("registration") if isinstance(item, dict) else None
        if not isinstance(registration, dict):
            continue
        key = (registration.get("component") or {}).get("key")
        repository = registration.get("repository")
        if isinstance(key, str) and isinstance(repository, str) and repository:
            found.append((key, repository))
    return found


def merged_commits(aggregate: dict) -> dict[str, str | None]:
    """Each registered component's `merged_commit`, as `outputs aggregate` prints its published
    Component Outputs: the evidence status, such as `available` or `not_collected`, or None
    when its outputs couldn't be read or name no `merged_commit`. A Repository Owner's `merge`
    publishes it once a run has applied every other phase of its repository."""
    found = {}
    for item in aggregate.get("components", []):
        if not isinstance(item, dict):
            continue
        key = ((item.get("registration") or {}).get("component") or {}).get("key")
        if not isinstance(key, str):
            continue
        merged = ((item.get("outputs") or {}).get("exports") or {}).get(MERGED_COMMIT)
        status = (merged.get("evidence") or {}).get("status") if isinstance(merged, dict) else None
        found[key] = status if item.get("status") == "available" else None
    return found


def managed_registrations(aggregate: dict) -> list[tuple[str, str]]:
    """Each Managed Repository a registered component lists in its registration, as
    `outputs aggregate` prints them: the component's key and the repository's URL. A
    registration at 2026.3's schema 1 lists none."""
    found = []
    for item in aggregate.get("components", []):
        registration = item.get("registration") if isinstance(item, dict) else None
        if not isinstance(registration, dict):
            continue
        key = (registration.get("component") or {}).get("key")
        for managed in registration.get("managed_repositories") or []:
            url = managed.get("url") if isinstance(managed, dict) else None
            if isinstance(key, str) and isinstance(url, str) and url:
                found.append((key, url))
    return found


def stored_path(root: Path, path: Path) -> str:
    """A path inside the instance folder is kept relative to it, any other absolute."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix() or "."
    except ValueError:
        return str(path.resolve())


def applied_record(checkout: Path) -> SourceRecord | None:
    """A Component Repository's record as its `main` holds it, the code last merged, which the
    platform runs; as checked out when `main` holds none."""
    return SourceRecord.read(checkout, gitops.main_commit(checkout)) or SourceRecord.read(checkout)


def state_folder(root: Path) -> Path:
    """`.dp-instance/` in an instance folder, which belongs to dp-instance alone and ignores
    itself."""
    folder = root / STATE_FOLDER
    folder.mkdir(exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    return folder


def map_path(root: Path) -> Path | None:
    """The folder's repository map, `.dp-instance/repositories.json`, or the earlier
    `dp-instance.json` until a command moves it in; None when it has neither. A folder with both
    stops every command."""
    current, earlier = root / MAP_FILE, root / EARLIER_MAP_FILE
    if earlier.is_file() and current.is_file():
        raise failed(
            f"{root} holds both {EARLIER_MAP_FILE} and {MAP_FILE.as_posix()}, so dp-instance moved "
            f"neither: dp-instance 3.0.0 reads {MAP_FILE.as_posix()}."
        )
    if earlier.is_file():
        return earlier
    return current if current.is_file() else None


def move_earlier_map(root: Path) -> bool:
    """Move a map from before Platform 2026.5.0 into `.dp-instance/`, unchanged; whether there
    was one to move. Removed at the 2026.6.0 stop."""
    earlier = root / EARLIER_MAP_FILE
    if not earlier.is_file():
        return False
    moved = state_folder(root) / MAP_FILE.name
    try:
        os.replace(earlier, moved)
    except OSError as error:
        raise failed(
            f"Couldn't move {earlier} to {moved}: {error.strerror or error}; nothing has changed. Run "
            "the command again once nothing holds either file open."
        ) from None
    return True


def unmapped_infrastructure(start: Path) -> Path | None:
    """The Client Infrastructure Repository checkout ``start`` is in, as template access reads it
    before the instance is mapped, at a first deployment; None in any other folder."""
    checkout = next((item for item in (start, *start.parents) if (item / ".git").exists()), None)
    if checkout is None:
        return None
    try:
        record = SourceRecord.read(checkout)
    except Stop:
        return None
    return checkout if record is not None and record.template == INFRASTRUCTURE else None


@dataclass
class Instance:
    root: Path
    key: str
    repositories: list[Mapped]

    @property
    def state(self) -> Path:
        return state_folder(self.root)

    def own(self, kind: str, repository: str) -> Path:
        """A folder of dp-instance's own for one repository, `.dp-instance/<kind>/` and eight
        hex digits of its key's hash: about as deep as the repository's checkout. Windows
        refuses a path over 260 characters, and a template's deepest file comes near that from
        the checkout itself."""
        return self.state / kind / hashlib.sha256(repository.encode()).hexdigest()[:8]

    @property
    def components(self) -> dict[str, Mapped]:
        return {item.template: item for item in self.repositories if item.template}

    @property
    def infrastructure(self) -> Mapped | None:
        """The Client Infrastructure Repository, when it's mapped."""
        return self.components.get(INFRASTRUCTURE)

    @property
    def managed(self) -> list[Mapped]:
        return [item for item in self.repositories if item.managed_by]

    @staticmethod
    def locate(start: Path) -> Path | None:
        """The instance folder: ``start``, or the nearest folder above it, with a repository map."""
        return next(
            (folder for folder in (start.resolve(), *start.resolve().parents) if map_path(folder)), None
        )

    @classmethod
    def find(cls, start: Path) -> Instance:
        """The instance whose map is in ``start`` or a folder above it."""
        root = cls.locate(start)
        if root is None:
            raise failed(
                f"No {MAP_FILE.as_posix()} here or in a folder above. Run setup in the instance folder.",
                Next("dp-instance setup", "maps the instance's repositories in the current folder"),
            )
        return cls.load(root)

    @classmethod
    def load(cls, root: Path) -> Instance:
        path = map_path(root) or root / MAP_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            key = data["platform_instance_key"]
            entries = data["repositories"]
            if data.get("schema_version") != 1 or not isinstance(entries, list):
                raise ValueError
            repositories = []
            for entry in entries:
                stored = entry["path"]
                place = Path(stored)
                repositories.append(
                    Mapped(
                        (place if place.is_absolute() else root / place).resolve(),
                        stored,
                        entry["remote"],
                        entry.get("template"),
                        entry.get("managed_by"),
                    )
                )
            if any(bool(item.template) == bool(item.managed_by) for item in repositories):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise failed(f"{path} isn't a repository map dp-instance reads.", SETUP) from None
        return cls(root.resolve(), key, repositories)

    def save(self) -> Path:
        path = self.state / MAP_FILE.name
        document = {
            "schema_version": 1,
            "platform_instance_key": self.key,
            "repositories": [item.describe() for item in self.repositories],
        }
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return path

    def check(self) -> None:
        """Stop unless every mapped repository is where the map says, with its remote."""
        problems = []
        for item in self.repositories:
            if not item.path.is_dir() or not gitops.is_checkout_root(item.path):
                problems.append(f"{item.stored} isn't a Git checkout")
                continue
            remote = gitops.origin(item.path)
            if remote is None or not gitops.same_repository(remote, item.remote):
                problems.append(f"{item.stored}'s origin is {remote or 'missing'}, not {item.remote}")
                continue
            record = SourceRecord.read(item.path)
            if item.template and (record is None or record.template != item.template):
                problems.append(f"{item.stored} doesn't hold the {item.template} template")
            if item.managed_by and record is not None:
                problems.append(f"{item.stored} is a Component Repository, not a Managed Repository")
        if problems:
            raise failed(
                "The repository map doesn't match the checkouts: " + "; ".join(problems) + ".",
                SETUP,
            )

    def record(self, template: str, *, applied: bool = False) -> SourceRecord:
        """A Component Repository's record, as checked out, or with ``applied`` as `main`
        holds it: the code last merged, which the platform runs."""
        checkout = self.components[template].path
        found = applied_record(checkout) if applied else SourceRecord.read(checkout)
        if found is None:
            raise failed(f"{self.components[template].stored} has no template source record.", SETUP)
        return found

    def heads(self) -> dict[str, str]:
        """Each mapped repository's checked-out commit, by its name."""
        return {item.name: gitops.head(item.path) for item in self.repositories}
