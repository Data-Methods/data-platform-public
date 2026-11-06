"""Which release an instance is on, and whether it may take a target, from its records.

Each Component Repository records the template commit it holds and, from the engine that records
it, the Platform Version its upgrade named. On `stable`, the release the instance runs is the
newest published one whose manifest names each repository's commit, for every repository it
lists, so a repository a release leaves unchanged counts as that release, and a hand-edited
version never counts. When the records name a version, it's checked against its manifest, and
only the manifests published after it are read; records that name none, as at the contract
floor, or whose version doesn't hold, are matched against every published manifest. A
repository on `canary` runs unpublished commits, a candidate's, and its published release is the
one it takes. The candidate's Platform Version is the one a manifest this workstation's runs ran
names, when its sources are the records' commits; else the one the records name, which counts
once that version is published only where its manifest names the commits, and otherwise is taken
as it is, except where it decides whether the instance may take a target. A release this
dp-instance can't read is taken once `dp-instance self update` installs the newest. Versions
compare whole, so 2026.3.1 is newer than 2026.3.

Each dp-instance knows only its own floor, the oldest Platform Version whose contracts it reads.
Every command that finds an instance on a published release before it, as each repository's
`main` records it, refuses it first, and names the dp-instance that reads it; it fetches `main`
only to make sure before it refuses. So does every command that finds a run under way or
stopped that an earlier walk started, which only the dp-instance before this walk carries on.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops, installed
from . import record as records
from .instance import Instance, SourceRecord, applied_record
from .manifest import Manifest, Releases, family, read_file, version_key
from .outcome import Next, Stop, failed

# The oldest Platform Version whose contracts this dp-instance reads, and the dp-instance that
# reads the releases before it, from the oldest it reads.
FLOOR = "2026.5.1"
BELOW_FLOOR = "3.0.0"
BELOW_FLOOR_READS = "Platform 2026.4 to 2026.5.1"


def apply_steps(recorded: dict[str, SourceRecord], why: str) -> tuple[Next, ...]:
    """What brings `main` to what the platform runs: `apply`, under the dp-instance that walks
    the release these records name, which is an older one for a release below the floor."""
    apply = Next("dp-instance apply", why)
    older = any(
        item.platform_version and version_key(item.platform_version) <= version_key(FLOOR)
        for item in recorded.values()
    )
    return (below_floor(", which walks this release"), apply) if older else (apply,)


def records_of(instance: Instance) -> dict[str, SourceRecord]:
    """Each Component Repository's record, as checked out."""
    return {template: instance.record(template) for template in instance.components}


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


def _holds(releases: Releases, version: str, recorded: dict[str, SourceRecord]) -> bool:
    """Whether a published version's manifest names each record's commit, for every repository
    it lists, read as published, as every published manifest is matched; a tag that moved stops."""
    try:
        document = json.loads(releases.text(version))
    except ValueError:
        return False
    return isinstance(document, dict) and matches(document, recorded)


def release_of(recorded: dict[str, SourceRecord], releases: Releases) -> str | None:
    """The newest published release whose manifest names each record's commit, for every
    repository it lists; None when none does. When the newest version a stable record names
    holds, only the manifests published after it are read, newest first."""
    named = [
        item.platform_version
        for item in recorded.values()
        if item.channel == "stable" and item.platform_version
    ]
    published = releases.published()
    newest = max(named, key=version_key) if named else None
    if newest in published and _holds(releases, newest, recorded):
        later = [version for version in published if version_key(version) > version_key(newest)]
        return next((version for version in reversed(later) if _holds(releases, version, recorded)), newest)
    return next((version for version, document in releases.documents() if matches(document, recorded)), None)


@dataclass(frozen=True)
class Current:
    """What the instance's records say it runs: the published release, or, on candidate
    commits, the canary repositories and the candidate's version, when one is known."""

    version: str | None
    canary: tuple[str, ...]
    candidate: str | None = None
    # The records it was read from.
    recorded: dict[str, SourceRecord] = field(default_factory=dict, compare=False)

    def describe(self) -> str:
        if self.version:
            return f"Platform {self.version}"
        if self.canary:
            return "canary candidate commits" + (f" of Platform {self.candidate}" if self.candidate else "")
        return "no published release"


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


def _candidate(recorded: dict[str, SourceRecord], given: tuple[Manifest, ...]) -> Manifest | None:
    """The newest of ``given`` candidate manifests whose sources the records name."""
    return next((found for found in given if matches({"sources": found.sources}, recorded)), None)


def candidate_version(
    instance: Instance,
    releases: Releases,
    recorded: dict[str, SourceRecord],
    *,
    given: tuple[Manifest, ...] = (),
    verified: bool = False,
) -> str | None:
    """The Platform Version whose candidate the records hold: the one a manifest this
    workstation's completed runs ran names, or one of ``given``, when its sources are the
    records' commits; else the newest the canary records name, which counts once it's published
    only where its manifest names their commits, and while it isn't, unless ``verified`` asks for
    a checked version or this workstation's runs ran a manifest of it that names other commits.
    None when nothing names one."""
    found = _candidate(recorded, (*saved_candidates(instance), *given))
    if found is not None:
        return found.platform_version
    named = [
        item.platform_version
        for item in recorded.values()
        if item.channel != "stable" and item.platform_version
    ]
    if not named:
        return None
    newest = max(named, key=version_key)
    if newest in releases.published():
        return newest if _holds(releases, newest, recorded) else None
    if verified or any(item.platform_version == newest for item in saved_candidates(instance)):
        return None
    return newest


def standing(
    instance: Instance,
    releases: Releases,
    recorded: dict[str, SourceRecord],
    *,
    given: tuple[Manifest, ...] = (),
    verified: bool = False,
) -> Current:
    """Where the records say the instance stands: the published release, when every record is
    on `stable`, or else the candidate's version."""
    canary = tuple(sorted(key for key, value in recorded.items() if value.channel != "stable"))
    if not canary:
        return Current(release_of(recorded, releases) if recorded else None, (), recorded=recorded)
    return Current(
        None,
        canary,
        candidate_version(instance, releases, recorded, given=given, verified=verified),
        recorded=recorded,
    )


def mixed(recorded: dict[str, SourceRecord]) -> str:
    """Each repository's record, for a stop that names them."""
    return "; ".join(f"{key} records {value.describe()}" for key, value in sorted(recorded.items()))


def manifest_of(
    instance: Instance,
    releases: Releases,
    recorded: dict[str, SourceRecord],
    *,
    given: tuple[Manifest, ...] = (),
) -> Manifest:
    """The manifest of the release the records hold, which a walk runs in its order: the
    published release every repository records, or the candidate manifest this workstation's
    runs ran, or one of ``given``, whose sources they name. Repositories that record different
    Platform Versions, or commits no release or candidate here names, stop, naming each."""
    version = release_of(recorded, releases) if recorded else None
    if version is not None:
        return releases.manifest(version)
    found = _candidate(recorded, (*saved_candidates(instance), *given))
    if found is not None:
        return found
    raise Stop(
        "failed",
        "The repositories' records match no published release and no candidate this workstation "
        f"ran: {mixed(recorded)}. Nothing has changed.",
        reason="mixed",
        next=(Next("dp-instance status", "shows what each repository records"),),
    )


def running(instance: Instance, releases: Releases) -> Manifest:
    """The manifest of the release the checked-out code is, in its order."""
    return manifest_of(instance, releases, records_of(instance))


SELF_UPDATE = Next("dp-instance self update", "installs the newest dp-instance")


def upgrade_steps(releases: Releases, version: str, why: str, *, pinned: bool = False) -> tuple[Next, ...]:
    """The commands that take a release: first, when a newer dp-instance is published and this
    one isn't pinned, or when this one can't read the release's manifest, `self update`, which
    installs the newest."""
    upgrade = Next(f"dp-instance prepare --to {version}", why)
    newest = next(reversed(releases.tools()), None)
    if not pinned and newest is not None and not installed.at_least(installed.version(), newest):
        return (SELF_UPDATE, upgrade)
    try:
        releases.manifest(version)
    except Stop:
        return (SELF_UPDATE, upgrade)
    return (upgrade,)


def next_release(releases: Releases, version: str) -> str | None:
    """The release an instance on ``version`` upgrades to next: the newest release of the next
    Platform Version, which holds its own version's fixes, or else a newer fix of its own."""
    later = [item for item in releases.families() if item > family(version)]
    if later:
        return releases.latest_of(later[0])
    newer = releases.latest_of(family(version))
    return newer if newer is not None and version_key(newer) > version_key(version) else None


def upgrades(releases: Releases, found: Current) -> tuple[list[tuple[str, str]], list[str]]:
    """The published releases an instance may upgrade to, as `check_next_stop` allows them: first
    the latest of the next Platform Version and the newest fix of its own, each saying which, then
    any other it allows, newest first. A candidate may take its own release once published."""
    start = found.version or found.candidate
    if start is None:
        return [], []
    own = family(start)
    later = [item for item in releases.families() if item > own]
    allowed = [
        version
        for version in releases.published()
        if (family(version) == own or (later and family(version) == later[0]))
        and (
            version_key(version) > version_key(start)
            or (found.version is None and version_key(version) == version_key(start))
        )
    ]
    first = []
    if later and releases.latest_of(later[0]) in allowed:
        first.append((releases.latest_of(later[0]), "latest next Platform Version"))
    newest = releases.latest_of(own)
    if newest in allowed:
        first.append((newest, "newest fix"))
    named = {version for version, _ in first}
    return first, [version for version in reversed(allowed) if version not in named]


def current_version(found: Current, releases: Releases) -> str:
    """The Platform Version the records hold, as a line says it."""
    if found.version:
        return found.version
    if not found.canary:
        return "no published release"
    if found.candidate is None:
        return "unknown: the commits match no candidate this workstation upgraded to"
    state = "candidate" if found.candidate in releases.published() else "candidate, not published"
    return f"{found.candidate} ({state})"


def describe_next(version: str, upcoming: str) -> str:
    """What ``upcoming``, the next release, is to an instance on ``version``."""
    if family(upcoming) > family(version):
        return "the next Platform Version, a required stop"
    return "the newest fix of Platform " + ".".join(str(part) for part in family(version))


def check_next_stop(found: Current, releases: Releases, target: Manifest, *, pinned: bool = False) -> None:
    """Every Platform Version is a required stop: an instance moves to a newer release of its
    own family or to one of the next, and never back to an older release, an older fix
    included, nor to the release it runs. On candidate commits, its own is the version
    they're a checked candidate of; with none, nothing is compared, as before records named
    one."""
    start = found.version or found.candidate
    if found.version is None and not found.canary:
        raise failed(
            "This instance's recorded template commits match no published release, so dp-instance "
            f"can't tell whether it may take Platform {target.platform_version}.",
            Next("dp-instance status", "shows what each repository records"),
        )
    if start is None:
        # Candidate commits no manifest names: the newest Platform Version their records name
        # bounds them, and with none named, nothing tells a downgrade apart.
        named = [item.platform_version for item in found.recorded.values() if item.platform_version]
        if not named:
            raise failed(
                "This instance's canary candidate commits name no Platform Version, so dp-instance can't "
                f"tell whether Platform {target.platform_version} would take it back.",
                Next("dp-instance status", "shows what each repository records"),
            )
        newest = max(named, key=version_key)
        if version_key(target.platform_version) < version_key(newest):
            raise failed(
                f"This instance runs canary candidate commits of Platform {newest} or later; it can't go "
                f"back to {target.platform_version}."
            )
        return
    runs = f"Platform {start}" if found.version else f"canary candidate commits of Platform {start}"
    if found.version is not None and target.platform_version == start:
        raise failed(f"This instance runs {runs} already; nothing to prepare.")
    if version_key(target.platform_version) < version_key(start):
        raise failed(f"This instance runs {runs}; it can't go back to {target.platform_version}.")
    # A candidate's Platform Version may not be published yet.
    families = sorted({*releases.families(), family(start), family(target.platform_version)})
    distance = families.index(family(target.platform_version)) - families.index(family(start))
    if distance > 1:
        stop = releases.latest_of(families[families.index(family(start)) + 1])
        raise failed(
            f"This instance runs {runs}. Every Platform Version is a required stop, "
            f"so it upgrades to {stop} before {target.platform_version}.",
            *upgrade_steps(releases, stop, "takes the next required stop", pinned=pinned),
        )


def below_floor(why: str) -> Next:
    return installed.install(BELOW_FLOOR, f", which reads {BELOW_FLOOR_READS}{why}")


def fetch_main(checkout: Path) -> None:
    """Fetch the checkout's `main` from GitHub, as `origin/main`; a fetch that fails leaves the
    one last fetched."""
    try:
        gitops.remote_git("fetch", "--quiet", "origin", "main", cwd=checkout)
    except Stop:
        pass


def applied_release(checkouts: dict[str, Path], releases: Releases) -> str | None:
    """The published release each Component Repository's `main`, as last fetched, records; None
    when the records that can be read match none."""
    recorded = {}
    for template, checkout in checkouts.items():
        try:
            found = applied_record(checkout)
        except Stop:
            continue
        if found is not None and found.channel == "stable":
            recorded[template] = found
    return release_of(recorded, releases) if recorded else None


def check_floor(checkouts: dict[str, Path], releases: Releases) -> None:
    """Stop when the instance runs a published release before FLOOR, as each Component
    Repository's `main` records it, naming the dp-instance that reads it. `main` as last fetched
    decides, and only when it would refuse is each `main` fetched from GitHub to decide again, so a
    current instance fetches nothing and a workstation that fetched long ago isn't refused for it.
    Records that can't be read, or that match no published release, say nothing. Only the release
    cache, and when it would refuse each checkout's `origin/main`, are written."""
    if all(version_key(version) >= version_key(FLOOR) for version in releases.published()):
        return
    version = applied_release(checkouts, releases)
    if version is None or version_key(version) >= version_key(FLOOR):
        return
    with ThreadPoolExecutor(max_workers=max(len(checkouts), 1)) as pool:
        list(pool.map(fetch_main, checkouts.values()))
    version = applied_release(checkouts, releases)
    if version is None or version_key(version) >= version_key(FLOOR):
        return
    raise Stop(
        "failed",
        f"This instance runs Platform {version}, and {installed.shown(installed.version())} reads "
        f"Platform {FLOOR} and later; nothing has changed." + installed.before_self_update(BELOW_FLOOR),
        next=(below_floor(""),),
        reason="below-floor",
        platform_version=version,
        floor=FLOOR,
    )


def refuse_earlier_walk(run: records.Record) -> None:
    """Stop at a run a dp-instance before this walk started, one before `self update` among
    them, whose record this one doesn't walk: only such a dp-instance carries it on. Nothing is
    written."""
    if run.data.get("schema_version") == records.SCHEMA_VERSION and "dp_instance" in run.data:
        return
    found = run.data.get("dp_instance")
    started = f"dp-instance {found}" if found else f"a dp-instance before {installed.SELF_UPDATE}"
    raise Stop(
        "failed",
        f"Run {run.id}, which {started} started, hasn't finished, and {installed.shown(installed.version())} "
        "walks prepare then apply, so it can't carry it on; nothing has changed."
        + installed.before_self_update(BELOW_FLOOR),
        next=(below_floor(", and finishes the run with dp-instance resume"),),
        reason="earlier-run",
        run=run.id,
    )


def check_earlier_run(state: Path) -> None:
    """Stop when this workstation's run under way or stopped is one an earlier walk started."""
    run = records.active(state, kinds=records.EARLIER_KINDS + records.KINDS)
    if run is not None:
        refuse_earlier_walk(run)


def target(releases: Releases, version: str | None, file: str | None) -> tuple[Manifest, str]:
    """A prepared upgrade's target and its channel: a published Platform Version on `stable`, or
    canary's unpublished candidate manifest on `canary`."""
    if file is not None:
        return read_file(Path(file)), "canary"
    return releases.manifest(version), "stable"
