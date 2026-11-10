"""This dp-instance's version, and the command that installs another.

dp-instance has versions of its own, from 2.0.0, in `tools/dp-instance/pyproject.toml`, each
published as the tag `dp-instance/X.Y.Z` on data-platform-public. Each knows only its own floor,
and a refusal names the version to install. dp-instance never replaces itself while a command
runs a release: `dp-instance self update` installs the newest, and `--to` an exact version,
which it pins in the instance folder's `.dp-instance/pinned.json` until a `self update` without
`--to` clears it.
"""

from __future__ import annotations

import json
import re
from importlib import metadata
from pathlib import Path

from .outcome import Next

VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# Where an instance folder keeps the version `self update --to` installed.
PINNED = "pinned.json"


def version() -> str | None:
    """The installed version, or None when dp-instance isn't installed as a package."""
    try:
        return metadata.version("dp-instance")
    except metadata.PackageNotFoundError:
        return None


def shown(own: str | None) -> str:
    """This dp-instance as a sentence names it."""
    return f"dp-instance {own}" if own else "a local copy of dp-instance"


def key(value: str) -> tuple[int, int, int]:
    """A version as numbers, so 2.10.0 is newer than 2.9.0."""
    match = VERSION.fullmatch(value)
    if match is None:
        raise ValueError(f"{value} isn't a dp-instance version (X.Y.Z).")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def at_least(installed: str | None, minimum: str) -> bool:
    return (
        installed is not None and VERSION.fullmatch(installed) is not None and key(installed) >= key(minimum)
    )


def install(version: str, then: str = "") -> Next:
    """The command that installs dp-instance ``version``, and ``then``, what comes after it."""
    return Next(f"dp-instance self update --to {version}", f"installs dp-instance {version}{then}")


def pin(state: Path, version: str) -> None:
    """Keep the version `self update --to` installs in the instance folder's `.dp-instance/`."""
    (state / PINNED).write_text(
        json.dumps({"schema_version": 1, "version": version}) + "\n", encoding="utf-8"
    )


def unpin(state: Path) -> None:
    (state / PINNED).unlink(missing_ok=True)


def pinned(state: Path) -> str | None:
    """The version `self update --to` pinned, while it's the one installed: a pin another install
    replaced, or that names a version that never installed, pins nothing."""
    try:
        data = json.loads((state / PINNED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    found = data.get("version") if isinstance(data, dict) and data.get("schema_version") == 1 else None
    return found if isinstance(found, str) and found == version() else None
