"""What the platform last applied, beside what `main` holds on GitHub: each component's applied
commit, from the instance's Component Outputs, and the components `main` is ahead of.

The drift check and `prepare --to` read the Component Outputs the same way, and fail closed: a
read that warns of a component the walk includes, or of one it can't attribute to a component,
a listing that isn't complete, a component whose outputs can't be read and one whose publication
names no commit each stop, naming what. A retired component's old registration, which the walk
doesn't include, doesn't. A component's applied commit is its marker, or, before any walk
recorded one, the commit its last publication's `source` stamp names; only a component
registered and never published has none."""

from __future__ import annotations

import re
from pathlib import Path

from . import configuration, gitops
from .components import Cli, Log
from .instance import SETUP, Instance, check_aggregate
from .manifest import Manifest
from .outcome import Next, Stop, failed

# The engine's warning about one component's record: its applied commit it couldn't read, or its
# registration at an older schema, which it leaves out.
ABOUT = re.compile(r"^(?P<key>[a-z0-9][a-z0-9._-]*)/(applied|registration)\.json\b")


def read(
    instance: Instance, manifest: Manifest, folder: Path, log: Log | None = None
) -> tuple[dict, list[str]]:
    """The instance's Component Outputs and the warnings the read gave, read by the first
    repository's owner, which every other's depends on, from ``folder``; another Platform
    Instance's are refused."""
    first = next((item for item in manifest.order if item.key in instance.components), None)
    if first is None:
        raise failed("The release names none of the instance's repositories.")
    command = first.owner.command
    found, warnings = Cli(command, folder, log or Log(None)).document_and_warnings("outputs", "aggregate")
    check_aggregate(found, command)
    key = (found.get("platform") or {}).get("key")
    if key != instance.key:
        raise failed(
            f"The Component Outputs {command} reads belong to {key or 'no Platform Instance'}, and the "
            f"map names {instance.key}; nothing has changed.",
            SETUP,
        )
    return found, warnings


def problems(found: dict, warnings: list[str], included: set[str]) -> list[str]:
    """What keeps the Component Outputs from being read whole for the components ``included``:
    each warning about one of them, or about no component it names, and a listing that isn't
    complete."""
    listed = found.get("warnings")
    if isinstance(listed, list):
        # What an engine from 0.21.0 lists, each with its component. The next stop removes the
        # stderr text below, which an earlier release's engine gives.
        found_problems = [item["message"] for item in listed if item.get("component") in included]
    else:
        named = [(ABOUT.match(warning), warning) for warning in warnings]
        found_problems = [warning for match, warning in named if match is None or match["key"] in included]
    enumeration = (found.get("enumeration") or {}).get("status", "complete")
    if enumeration != "complete":
        found_problems.append(f"its listing of the registered components is {enumeration}")
    return found_problems


def applied_commits(found: dict) -> tuple[dict[str, str | None], list[str]]:
    """Each registered component's applied commit, None only for one registered and never
    published, and what can't be read."""
    commits: dict[str, str | None] = {}
    unread: list[str] = []
    for item in found.get("components", []):
        if not isinstance(item, dict):
            continue
        key = ((item.get("registration") or {}).get("component") or {}).get("key")
        if not isinstance(key, str):
            unread.append("a registration that names no component")
            continue
        marker = (item.get("applied") or {}).get("commit") if isinstance(item.get("applied"), dict) else None
        source = (
            ((item.get("outputs") or {}).get("source") or {}) if item.get("status") == "available" else {}
        )
        commit = marker or source.get("commit")
        if isinstance(commit, str) and gitops.COMMIT.fullmatch(commit):
            commits[key] = commit
        elif item.get("status") != "available" and item.get("reason") == "unpublished" and not marker:
            commits[key] = None
        elif item.get("status") != "available":
            unread.append(f"{key}'s Component Outputs ({item.get('reason') or 'unavailable'})")
        else:
            unread.append(f"{key}'s applied commit: its publication names none")
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
    found, warnings = read(instance, manifest, folder)
    whole = problems(found, warnings, included)
    if whole:
        raise unread("The instance's Component Outputs can't be read whole", whole, again)
    commits, missing = applied_commits(found)
    if missing:
        raise unread("What the platform last applied can't be read", missing, again)
    return found, commits


def pending(
    instance: Instance,
    found: dict,
    applied: dict[str, str | None],
    commits: dict[str, str],
    copies: dict[str, Path],
) -> list[str]:
    """Each component `main` on GitHub, ``commits``, is ahead of: one whose applied commit is
    another and, when only its publication's stamp names that commit, whose configuration on
    `main` in ``copies`` isn't the one it published, as a squash merge leaves the stamp's commit
    off `main`; and one registered and never published. `apply` is pending for each."""
    found_pending, stamped = [], []
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
        if not isinstance(key, str) or template is None or template not in commits:
            continue
        marker = (item.get("applied") or {}).get("commit") if isinstance(item.get("applied"), dict) else None
        if key in applied and applied[key] is None:
            found_pending.append(key)
        elif marker is not None:
            if marker != commits[template]:
                found_pending.append(key)
        elif applied.get(key) != commits[template]:
            stamped.append(template)
    places = [(instance.components[name].remote, copies[name]) for name in dict.fromkeys(stamped)]
    return [
        *found_pending,
        *(name for name in configuration.unapplied(places, found) if name not in found_pending),
    ]
