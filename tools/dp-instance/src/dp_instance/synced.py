"""Folders a sync client manages: OneDrive, iCloud Drive, Dropbox and Google Drive.

A sync client rewrites files under Git and Terraform while they run, so an instance folder
in one is refused, with no override.
"""

from __future__ import annotations

import os
from pathlib import Path

CLOUD_STORAGE = {
    "onedrive": "OneDrive",
    "dropbox": "Dropbox",
    "googledrive": "Google Drive",
    "icloud": "iCloud Drive",
}


def _within(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def provider(path: Path) -> str | None:
    """The sync client that manages ``path``, if dp-instance recognises one."""
    resolved = path.resolve()
    parts = [part.casefold() for part in resolved.parts]
    home = Path.home().resolve()
    for variable in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        root = os.environ.get(variable)
        if root and _within(resolved, Path(root).resolve()):
            return "OneDrive"
    cloud = home / "Library" / "CloudStorage"
    if _within(resolved, cloud) and resolved != cloud:
        name = resolved.relative_to(cloud).parts[0].casefold()
        return next((label for prefix, label in CLOUD_STORAGE.items() if name.startswith(prefix)), name)
    mobile = home / "Library" / "Mobile Documents"
    if _within(resolved, mobile):
        return "iCloud Drive"
    documents = mobile / "com~apple~CloudDocs"
    for name in ("Desktop", "Documents"):
        if (documents / name).is_dir() and _within(resolved, home / name):
            return "iCloud Drive"
    for part in parts:
        if part == "onedrive" or part.startswith(("onedrive - ", "onedrive-")):
            return "OneDrive"
        if part == "iclouddrive":
            return "iCloud Drive"
        if part == "dropbox" or part.startswith("dropbox ("):
            return "Dropbox"
        if part in ("google drive", "my drive") or part.startswith("googledrive"):
            return "Google Drive"
    return None
