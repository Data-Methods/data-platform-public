"""Which components hold configuration no run has applied, by hash.

Each Component Outputs publish carries the hash of the configuration it was applied from, as
the engine stamps it: SHA-256 over each of the component's phases' `config/<component>/<phase>.json`,
in name order, each name and its content followed by a NUL, a missing file counting as empty.
The engine hashes the files as the checkout holds them, so this does too. A component whose
configuration differs from the one it last published holds a change no run has applied.
Hashes are compared, never commits: a run publishes from its branch, whose commit a squash
merge never puts on `main`. A component that has published no outputs is skipped.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .sequence import MERGE, Declaration


def digest(checkout: Path, declared: Declaration) -> str:
    """The hash of a component's configuration in a checkout. The repository owner's `merge`
    phase, which `steps --json` lists, reads no configuration."""
    found = hashlib.sha256()
    names = sorted(f"{declared.key}/{phase.name}.json" for phase in declared.phases if phase.name != MERGE)
    for name in names:
        path = checkout / "config" / name
        content = path.read_bytes() if path.is_file() else b""
        found.update(name.encode() + b"\0" + content + b"\0")
    return found.hexdigest()


def published(aggregate: dict) -> dict[str, str]:
    """Each component's configuration hash, as its published Component Outputs stamp it, from
    `outputs aggregate`; a component with no outputs available is left out."""
    found = {}
    for item in aggregate.get("components", []):
        if not isinstance(item, dict) or item.get("status") != "available":
            continue
        key = ((item.get("registration") or {}).get("component") or {}).get("key")
        value = (((item.get("outputs") or {}).get("source")) or {}).get("configuration_sha256")
        if isinstance(key, str) and isinstance(value, str):
            found[key] = value
    return found


def unapplied(
    places: dict[str, Path], declarations: dict[tuple[str, str], Declaration], aggregate: dict
) -> list[str]:
    """The components, in order, whose configuration in their repository's place in ``places``
    differs from the one they last published."""
    stamped = published(aggregate)
    return [
        key
        for (repository, key), declared in declarations.items()
        if repository in places and key in stamped and digest(places[repository], declared) != stamped[key]
    ]
