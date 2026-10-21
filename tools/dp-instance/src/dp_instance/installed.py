"""The commit this dp-instance was installed from.

`uv tool install "git+<repository>@<commit>#subdirectory=tools/dp-instance"` records the
commit in the installed distribution's `direct_url.json` (PEP 610). dp-instance compares it
with the release's and never replaces itself while running, since Windows locks a running
program's file.
"""

from __future__ import annotations

import json
from importlib import metadata


def commit() -> str | None:
    """The Git commit of this installation, or None for any other kind of install."""
    try:
        text = metadata.distribution("dp-instance").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if not text:
        return None
    try:
        info = json.loads(text).get("vcs_info") or {}
    except ValueError:
        return None
    found = info.get("commit_id")
    return found if isinstance(found, str) and len(found) == 40 else None
