"""Which release an instance is on, and whether it may take a target, from its records.

Each Component Repository records the template commit it holds. On `stable`, a published
manifest whose sources name those commits is the release the instance runs; the 2026.2
manifest also maps each release snapshot to the development commit 2026.2's records name.
A repository on `canary` runs unpublished commits, which only canary's own candidate
manifests name, kept in its runs' records on the workstation that ran them; there, the
candidate's Platform Version is where the instance stands, and its published release is
the one it takes. A release whose manifest names no `dp-instance`, as 2026.2's doesn't,
predates it: an instance on it runs only `status` and `upgrade`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import record as records
from .instance import SETUP, Instance, SourceRecord
from .manifest import Manifest, Releases, family, read_file
from .outcome import Next, Stop, failed


def _snapshots(document: dict, template: str) -> set[str]:
    found = (document.get("snapshots") or {}).get(template) or {}
    if not isinstance(found, dict):
        return set()
    return {str(commit) for pair in found.items() for commit in pair}


def records_of(instance: Instance, *, applied: bool = False) -> dict[str, SourceRecord]:
    return {template: instance.record(template, applied=applied) for template in instance.components}


def matches(document: dict, recorded: dict[str, SourceRecord], *, snapshots: bool = False) -> bool:
    """Whether every mapped repository the manifest lists records its commit, and it lists at
    least one. With ``snapshots``, a 2026.2 development commit its snapshot map names counts."""
    sources = document.get("sources")
    if not isinstance(sources, dict):
        return False
    listed = [template for template in sources if template in recorded]
    if not listed:
        return False
    for template in listed:
        commit = recorded[template].commit
        if commit != sources[template] and not (snapshots and commit in _snapshots(document, template)):
            return False
    return True


@dataclass(frozen=True)
class Current:
    """What the instance's records say it runs."""

    version: str | None
    canary: tuple[str, ...]

    def describe(self) -> str:
        if self.version:
            return f"Platform {self.version}"
        if self.canary:
            return "canary candidate commits"
        return "no published release"


def current(instance: Instance, releases: Releases, *, applied: bool = False) -> Current:
    """The release the repositories record, as checked out, or with ``applied`` as each
    one's `main` holds it."""
    recorded = records_of(instance, applied=applied)
    stable = {key: value for key, value in recorded.items() if value.channel == "stable"}
    canary = tuple(sorted(key for key, value in recorded.items() if value.channel != "stable"))
    if not stable:
        return Current(None, canary)
    for version, document in releases.documents():
        if matches(document, stable, snapshots=True):
            return Current(version, canary)
    return Current(None, canary)


def saved_candidates(instance: Instance) -> list[Manifest]:
    """The manifests this workstation's upgrade runs ran, newest first."""
    found = []
    for run in records.records(instance.state, kinds=("upgrade",)):
        if run.manifest_path.is_file():
            try:
                found.append(read_file(run.manifest_path))
            except Stop:
                continue
    return found


def _saved_candidate(instance: Instance, recorded: dict[str, SourceRecord]) -> Manifest | None:
    """The newest manifest this workstation's upgrade runs ran whose sources the records name."""
    return next(
        (found for found in saved_candidates(instance) if matches({"sources": found.sources}, recorded)),
        None,
    )


def candidate_of(instance: Instance, *, applied: bool = False) -> str | None:
    """The Platform Version whose candidate the repositories record, as checked out, or with
    ``applied`` as each one's `main` holds it. Only the workstation that ran the candidate
    keeps its manifest, so on any other it's None."""
    found = _saved_candidate(instance, records_of(instance, applied=applied))
    return found.platform_version if found else None


def running(instance: Instance, releases: Releases) -> Manifest:
    """The manifest of the release the checked-out code is: what `plan`, `apply` and `verify`
    run, in its order."""
    recorded = records_of(instance)
    for version, document in releases.documents():
        if document.get("dp_instance_commit") is None:
            if matches(document, recorded, snapshots=True):
                raise _before_dp_instance(releases, version)
            continue
        if matches(document, recorded):
            return releases.manifest(version)
    candidate = _saved_candidate(instance, recorded)
    if candidate is not None:
        return candidate
    listing = ", ".join(f"{key} at {value.describe()}" for key, value in sorted(recorded.items()))
    raise failed(
        "The repositories' recorded template commits match no published release and no "
        f"candidate this workstation upgraded to: {listing}.",
        Next("dp-instance resume", "finishes an upgrade under way, if this workstation started it"),
        Next("dp-instance upgrade --to <version>", "upgrades the instance to a published Platform Version"),
        SETUP,
    )


def _before_dp_instance(releases: Releases, version: str) -> Stop:
    """The repositories record a release that predates `dp-instance`, whose manifest names none:
    only `status` and an upgrade to the next Platform Version run on it."""
    upcoming = next_release(releases, version)
    steps = (
        (
            Next(
                f"dp-instance upgrade --to {upcoming}",
                "takes the next Platform Version, which dp-instance runs",
            ),
        )
        if upcoming
        else (Next("dp-instance status", "shows where the instance stands"),)
    )
    return failed(
        f"The repositories record Platform {version}, which predates dp-instance: on it, dp-instance runs "
        "only status and an upgrade to the next Platform Version.",
        *steps,
    )


def next_release(releases: Releases, version: str) -> str | None:
    """The release an instance on ``version`` upgrades to next: a newer fix of its own
    Platform Version, or else the newest release of the next one."""
    newer = releases.latest_of(family(version))
    if newer and newer != version:
        return newer
    later = [item for item in releases.families() if item > family(version)]
    return releases.latest_of(later[0]) if later else None


def check_may_take(instance: Instance, releases: Releases, target: Manifest) -> Current:
    """Every Platform Version is a required stop: an instance moves to a release of its own
    family or of the next one, from any release of its current one. On candidate commits, its
    own is the Platform Version they're the candidate of, when this workstation ran it. What it
    runs is what each repository's `main` records, so an upgrade under way on its branch still
    counts as the release it started from."""
    found = current(instance, releases, applied=True)
    if found.version is None and not found.canary:
        raise failed(
            "This instance's recorded template commits match no published release, so dp-instance "
            f"can't tell whether it may take Platform {target.platform_version}.",
            Next("dp-instance status", "shows what each repository records"),
        )
    start = found.version or candidate_of(instance, applied=True)
    if start is None:
        return found
    runs = f"Platform {start}" if found.version else f"canary candidate commits of Platform {start}"
    # A candidate's Platform Version may not be published yet.
    families = sorted({*releases.families(), family(start)})
    distance = families.index(family(target.platform_version)) - families.index(family(start))
    if distance < 0:
        raise failed(f"This instance runs {runs}; it can't go back to {target.platform_version}.")
    if distance > 1:
        stop = releases.latest_of(families[families.index(family(start)) + 1])
        raise failed(
            f"This instance runs {runs}. Every Platform Version is a required stop, "
            f"so it upgrades to {stop} before {target.platform_version}.",
            Next(f"dp-instance upgrade --to {stop}", "takes the next required stop"),
        )
    return found


def target(releases: Releases, version: str | None, file: str | None) -> tuple[Manifest, str]:
    """An upgrade's target and its channel: a published Platform Version on `stable`, or
    canary's unpublished candidate manifest on `canary`."""
    if file is not None:
        return read_file(Path(file)), "canary"
    return releases.manifest(version), "stable"
