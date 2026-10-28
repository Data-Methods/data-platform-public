"""This dp-instance's version.

dp-instance has versions of its own, from 2.0.0, in `tools/dp-instance/pyproject.toml`, each
published as the tag `dp-instance/X.Y.Z` on data-platform-public. A Release Manifest names the
oldest version it runs, and a command refuses only an older one. dp-instance never replaces
itself while a command runs a release: `dp-instance update` installs the newest.
"""

from __future__ import annotations

import re
from importlib import metadata

VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def version() -> str | None:
    """The installed version, or None when dp-instance isn't installed as a package."""
    try:
        return metadata.version("dp-instance")
    except metadata.PackageNotFoundError:
        return None


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
