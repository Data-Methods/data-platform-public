"""Which components hold configuration no run has applied, by hash.

Each Component Outputs publish carries the hash of the configuration it was applied from, as
the engine stamps it: SHA-256 over each of the component's phases' `config/<component>/<phase>.json`,
in name order, each name below `config/` and its content followed by a NUL, a missing file
counting as empty. The phases are those its published outputs declare as entities, except the
`merge` of outputs from before Platform 2026.6.0, which read no configuration. The files are read on
`main`, the code
last merged, with their line endings as LF, as the engine hashes them, so a repository that holds
them with CRLF, as Git on Windows can, hashes the same. A component whose configuration on `main`
differs from the one it last published holds a change no run has applied. Hashes are compared,
never commits: a run publishes from its branch, whose commit a squash merge never puts on `main`.
A component that has published no outputs is skipped.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from . import gitops
from .sequence import MERGE


def files(key: str, outputs: dict) -> list[str]:
    """The configuration files a component's published hash covers, by their names below
    `config/`: one for each phase its outputs declare, except the `merge` of outputs from before
    Platform 2026.6.0."""
    phases = [
        entity["key"].removeprefix("phase:")
        for entity in outputs.get("entities") or []
        if isinstance(entity, dict) and entity.get("kind") == "phase" and isinstance(entity.get("key"), str)
    ]
    return sorted(f"{key}/{phase}.json" for phase in phases if phase != MERGE)


def digest(checkout: Path, names: list[str]) -> str:
    """The hash of these configuration files on the repository's `main`."""
    main = gitops.main_commit(checkout)
    found = hashlib.sha256()
    for name in sorted(names):
        content = gitops.blob(checkout, main, f"config/{name}") if main is not None else None
        found.update(name.encode() + b"\0" + (content or b"").replace(b"\r\n", b"\n") + b"\0")
    return found.hexdigest()


def unapplied(repositories: Iterable[tuple[str, Path]], aggregate: dict) -> list[str]:
    """The components of these repositories, each its URL and a clone that has fetched its
    `main`, in their order, whose configuration on `main` isn't the one they last published, as
    `outputs aggregate` prints each one's outputs and the repository its registration names."""
    published = []
    for item in aggregate.get("components", []):
        if not isinstance(item, dict) or item.get("status") != "available":
            continue
        registration = item.get("registration") or {}
        outputs = item.get("outputs") or {}
        key = (registration.get("component") or {}).get("key")
        repository = registration.get("repository")
        stamped = (outputs.get("source") or {}).get("configuration_sha256")
        if isinstance(key, str) and isinstance(repository, str) and isinstance(stamped, str):
            published.append((key, repository, files(key, outputs), stamped))
    return [
        key
        for remote, folder in repositories
        for key, repository, names, stamped in published
        if gitops.same_repository(repository, remote) and digest(folder, names) != stamped
    ]
