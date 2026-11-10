"""What the platform last applied, beside what `main` holds on GitHub: each component's applied
commit, from the instance's Component Outputs, and the components `main` is ahead of.

The drift check and `prepare --to` read the Component Outputs the same way, and fail closed: a
read that warns of a component the walk includes, or of one it can't attribute to a component,
a listing that isn't complete, a component whose outputs can't be read and one whose publication
names no commit each stop, naming what. A retired component's old registration, which the walk
doesn't include, doesn't. A component's applied commit is its marker, which a walk records once
it has applied the whole component; one no walk has applied has none."""

from __future__ import annotations

from pathlib import Path

from . import gitops
from .components import Cli, Log
from .instance import SETUP, Instance, check_aggregate
from .manifest import Manifest
from .outcome import Next, Stop, failed


def read(instance: Instance, manifest: Manifest, folder: Path, log: Log | None = None) -> dict:
    """The instance's Component Outputs, read by the first repository's owner, which every
    other's depends on, from ``folder``; another Platform Instance's are refused."""
    first = next((item for item in manifest.order if item.key in instance.components), None)
    if first is None:
        raise failed("The release names none of the instance's repositories.")
    command = first.owner.command
    found = Cli(command, folder, log or Log(None)).document("outputs", "aggregate")
    check_aggregate(found, command)
    key = (found.get("platform") or {}).get("key")
    if key != instance.key:
        raise failed(
            f"The Component Outputs {command} reads belong to {key or 'no Platform Instance'}, and the "
            f"map names {instance.key}; nothing has changed.",
            SETUP,
        )
    return found


def problems(found: dict, included: set[str]) -> list[str]:
    """What keeps the Component Outputs from being read whole for the components ``included``:
    each warning the read lists about one of them, and a listing that isn't complete."""
    found_problems = [
        item["message"] for item in found.get("warnings") or [] if item.get("component") in included
    ]
    enumeration = (found.get("enumeration") or {}).get("status", "complete")
    if enumeration != "complete":
        found_problems.append(f"its listing of the registered components is {enumeration}")
    return found_problems


def applied_commits(found: dict) -> tuple[dict[str, str | None], list[str]]:
    """Each registered component's applied commit, None for one no walk has applied, and what
    can't be read."""
    commits: dict[str, str | None] = {}
    unread: list[str] = []
    for item in found.get("components", []):
        if not isinstance(item, dict):
            continue
        key = ((item.get("registration") or {}).get("component") or {}).get("key")
        if not isinstance(key, str):
            unread.append("a registration that names no component")
            continue
        commit = (item.get("applied") or {}).get("commit") if isinstance(item.get("applied"), dict) else None
        if isinstance(commit, str) and gitops.COMMIT.fullmatch(commit):
            commits[key] = commit
        elif commit is not None:
            unread.append(f"{key}'s applied commit: {commit!r} isn't a commit")
        elif item.get("status") == "available" or item.get("reason") == "unpublished":
            commits[key] = None
        else:
            unread.append(f"{key}'s Component Outputs ({item.get('reason') or 'unavailable'})")
    return commits, unread


def unread(what: str, found: list[str], again: Next) -> Stop:
    return Stop(
        "failed",
        f"{what}: " + "; ".join(found) + ". Nothing has changed.",
        reason="outputs-unread",
        unread=found,
        next=(again,),
    )


def checked(
    instance: Instance, manifest: Manifest, folder: Path, included: set[str], again: Next
) -> tuple[dict, dict[str, str | None]]:
    """The Component Outputs, read whole for the components ``included``, and each component's
    applied commit; whatever can't be read stops, naming it."""
    found = read(instance, manifest, folder)
    whole = problems(found, included)
    if whole:
        raise unread("The instance's Component Outputs can't be read whole", whole, again)
    commits, missing = applied_commits(found)
    if missing:
        raise unread("What the platform last applied can't be read", missing, again)
    return found, commits


def pending(
    instance: Instance, found: dict, applied: dict[str, str | None], commits: dict[str, str]
) -> list[str]:
    """Each registered component `main` on GitHub, ``commits``, is ahead of: one whose applied
    commit is another, or that no walk has applied. `apply` is pending for each."""
    found_pending = []
    for item in found.get("components", []):
        registration = (item.get("registration") or {}) if isinstance(item, dict) else {}
        key = (registration.get("component") or {}).get("key")
        repository = registration.get("repository")
        template = next(
            (
                name
                for name, mapped in instance.components.items()
                if isinstance(repository, str) and gitops.same_repository(mapped.remote, repository)
            ),
            None,
        )
        if isinstance(key, str) and template in commits and applied.get(key) != commits[template]:
            found_pending.append(key)
    return found_pending
