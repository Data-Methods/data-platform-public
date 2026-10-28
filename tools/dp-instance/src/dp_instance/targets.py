"""Which release an instance is on, and whether it may take a target, from its records.

Each Component Repository records the template commit it holds. On `stable`, a published
manifest whose sources name those commits is the release the instance runs. A repository on
`canary` runs unpublished commits, which only canary's own candidate manifests name, kept in
its completed runs' records on the workstation that ran them; there, the candidate's Platform
Version is where the instance stands, and its published release is the one it takes. A release
this dp-instance doesn't run, such as 2026.3's, which names its dp-instance by commit, is taken
with the dp-instance its manifest names. Versions compare whole, so 2026.3.1 is newer than
2026.3.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import installed
from . import record as records
from .instance import SETUP, Instance, SourceRecord
from .manifest import Manifest, Releases, family, read_file, version_key
from .outcome import Next, Stop, failed


def records_of(instance: Instance, *, applied: bool = False) -> dict[str, SourceRecord]:
    return {template: instance.record(template, applied=applied) for template in instance.components}


def matches(document: dict, recorded: dict[str, SourceRecord]) -> bool:
    """Whether every mapped repository the manifest lists records its commit, and it lists at
    least one."""
    sources = document.get("sources")
    if not isinstance(sources, dict):
        return False
    listed = [template for template in sources if template in recorded]
    if not listed:
        return False
    for template in listed:
        if recorded[template].commit != sources[template]:
            return False
    return True


def named_before(releases: Releases, version: str) -> dict[str, set[str]]:
    """Each template's commits that the releases published before ``version`` name. A
    release reuses the commit of a template that didn't change, so one that names such a
    commit leaves that template as it was."""
    found: dict[str, set[str]] = {}
    for published, document in releases.documents():
        sources = document.get("sources")
        if version_key(published) >= version_key(version) or not isinstance(sources, dict):
            continue
        for template, commit in sources.items():
            found.setdefault(template, set()).add(commit)
    return found


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
        if matches(document, stable):
            return Current(version, canary)
    return Current(None, canary)


def saved_candidates(instance: Instance) -> list[Manifest]:
    """The manifests this workstation's completed runs ran, newest first. A run refused, failed
    or replaced never stands for what the instance runs, though the records may match its
    manifest, as they match one that names a repository only just set up."""
    found = []
    for run in records.records(instance.state):
        if run.status == "complete" and run.manifest_path.is_file():
            try:
                found.append(read_file(run.manifest_path))
            except Stop:
                continue
    return found


def _saved_candidate(instance: Instance, recorded: dict[str, SourceRecord]) -> Manifest | None:
    """The newest manifest this workstation's completed runs ran whose sources the records
    name."""
    return next(
        (found for found in saved_candidates(instance) if matches({"sources": found.sources}, recorded)),
        None,
    )


def under_way(
    instance: Instance, recorded: dict[str, SourceRecord]
) -> tuple[records.Record, Manifest] | None:
    """The run under way or stopped, and the manifest it runs, when the records name its
    sources, as a candidate upgrade that hasn't finished leaves them."""
    run = records.active(instance.state)
    if run is None or not run.manifest_path.is_file():
        return None
    try:
        found = read_file(run.manifest_path)
    except Stop:
        return None
    return (run, found) if matches({"sources": found.sources}, recorded) else None


def candidate_of(instance: Instance, *, applied: bool = False) -> str | None:
    """The Platform Version whose candidate the repositories record, as checked out, or with
    ``applied`` as each one's `main` holds it: a completed run's, or else the run under way's.
    Only the workstation that ran the candidate keeps its manifest, so on any other it's None."""
    recorded = records_of(instance, applied=applied)
    found = _saved_candidate(instance, recorded)
    if found is None:
        unfinished = under_way(instance, recorded)
        found = unfinished[1] if unfinished else None
    return found.platform_version if found else None


def running(instance: Instance, releases: Releases) -> Manifest:
    """The manifest of the release the checked-out code is: what `plan`, `apply` and `verify`
    run, in its order. A candidate run that hasn't finished is resumed, never run anew."""
    recorded = records_of(instance)
    for version, document in releases.documents():
        if matches(document, recorded):
            return releases.manifest(version)
    candidate = _saved_candidate(instance, recorded)
    if candidate is not None:
        return candidate
    unfinished = under_way(instance, recorded)
    if unfinished is not None:
        run, found = unfinished
        raise failed(
            f"The repositories record candidate commits of Platform {found.platform_version} from run "
            f"{run.id}, which hasn't finished.",
            Next("dp-instance resume", "carries on that run"),
        )
    listing = ", ".join(f"{key} at {value.describe()}" for key, value in sorted(recorded.items()))
    raise failed(
        "The repositories' recorded template commits match no published release and no "
        f"candidate this workstation upgraded to: {listing}.",
        Next("dp-instance resume", "finishes an upgrade under way, if this workstation started it"),
        Next("dp-instance upgrade --to <version>", "upgrades the instance to a published Platform Version"),
        SETUP,
    )


def upgrade_steps(releases: Releases, version: str, why: str) -> tuple[Next, ...]:
    """The commands that take a release: first, when this dp-instance doesn't run it, the install
    of the one its manifest names, as a manifest newer than this dp-instance reads names it."""
    upgrade = Next(f"dp-instance upgrade --to {version}", why)
    try:
        found = releases.manifest(version)
    except Stop as stop:
        return (*stop.next, upgrade)
    return (upgrade,) if found.runs_on(installed.version()) else (found.install(), upgrade)


def next_release(releases: Releases, version: str) -> str | None:
    """The release an instance on ``version`` upgrades to next: the newest release of the next
    Platform Version, which holds its own version's fixes, or else a newer fix of its own."""
    later = [item for item in releases.families() if item > family(version)]
    if later:
        return releases.latest_of(later[0])
    newer = releases.latest_of(family(version))
    return newer if newer is not None and version_key(newer) > version_key(version) else None


def describe_next(version: str, upcoming: str) -> str:
    """What ``upcoming``, the next release, is to an instance on ``version``."""
    if family(upcoming) > family(version):
        return "the next Platform Version, a required stop"
    return "the newest fix of Platform " + ".".join(str(part) for part in family(version))


def next_stop(instance: Instance, releases: Releases) -> tuple[str, str | None]:
    """With no target named, the release the instance runs, as each repository's `main`
    records it, and the next one it takes, or None on the newest release. Candidate commits
    take none: only a candidate manifest keeps canary on the canary channel."""
    found = current(instance, releases, applied=True)
    if found.canary:
        raise failed(
            "This instance runs canary candidate commits, so dp-instance doesn't choose the release it "
            "takes: a candidate manifest keeps it on the canary channel.",
            Next("dp-instance upgrade --manifest <candidate manifest>", "upgrades to that candidate"),
        )
    if found.version is None:
        raise failed(
            "This instance's recorded template commits match no published release, so dp-instance "
            "can't tell which release it takes next.",
            Next("dp-instance status", "shows what each repository records"),
        )
    return found.version, next_release(releases, found.version)


def check_may_take(instance: Instance, releases: Releases, target: Manifest) -> Current:
    """Every Platform Version is a required stop: an instance moves to a release of its own
    family or of the next one, from any release of its current one, and never back to an older
    release, an older fix included. On candidate commits, its own is the Platform Version
    they're the candidate of, when this workstation ran it. What it runs is what each
    repository's `main` records, so an upgrade under way on its branch still counts as the
    release it started from."""
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
    if version_key(target.platform_version) < version_key(start):
        raise failed(f"This instance runs {runs}; it can't go back to {target.platform_version}.")
    # A candidate's Platform Version may not be published yet.
    families = sorted({*releases.families(), family(start)})
    distance = families.index(family(target.platform_version)) - families.index(family(start))
    if distance > 1:
        stop = releases.latest_of(families[families.index(family(start)) + 1])
        raise failed(
            f"This instance runs {runs}. Every Platform Version is a required stop, "
            f"so it upgrades to {stop} before {target.platform_version}.",
            *upgrade_steps(releases, stop, "takes the next required stop"),
        )
    return found


def target(releases: Releases, version: str | None, file: str | None) -> tuple[Manifest, str]:
    """An upgrade's target and its channel: a published Platform Version on `stable`, or
    canary's unpublished candidate manifest on `canary`."""
    if file is not None:
        return read_file(Path(file)), "canary"
    return releases.manifest(version), "stable"
