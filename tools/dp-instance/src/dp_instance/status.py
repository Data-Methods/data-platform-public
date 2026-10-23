"""`dp-instance` alone, or `status`: where the instance stands, planning nothing.

It shows the active run's journey from its record, then what the repositories record, what
the hold acts on, their open pull requests, and each Managed Repository's adoption: whether
its adoption pull request is merged and released, and which Platform Version each
Environment's workflows pin, read once its tags and `main` are fetched. It reads the run
record, Git and GitHub, and the hold from the cluster when it can reach it, so it runs from
any workstation, with no network check: outside the instance's network the hold is unknown.
It asks nothing: it prints, names what comes next and returns.
"""

from __future__ import annotations

import re
from contextlib import nullcontext

from rich.text import Text

from . import github, gitops, hold, record, targets
from .instance import INFRASTRUCTURE, Mapped
from .manifest import Releases, family
from .outcome import Next, Result, Stop
from .screen import NAME_LABELS, level_line
from .sequence import Entry
from .session import Session

REASONS = {
    "decision": "stopped for review",
    "declined": "stopped: the change wasn't approved",
    "handoff": "waiting for an external handoff",
    "values": "waiting for values only a person supplies",
    "sign-in": "waiting for a sign-in",
    "runs": "waiting for runs to finish",
    "failed": "stopped on a failure",
    "preflight": "stopped: a preflight failed",
    "interrupted": "interrupted",
    "drift": "stopped on drift",
    "already-at-target": "stopped: the upgrade is already under way",
    "not-published": "stopped: a component wasn't published cleanly",
    "unmapped": "stopped: a repository of the instance isn't in the repository map",
}


def journey(session: Session, run: record.Record) -> None:
    """The run as its record left it: every step's outcome and time, and its details."""
    screen = session.screen
    shown = (None, None, None, None)
    for entry in run.entries:
        shown = _enter(screen, entry, shown)
        level, counter, name = _labels(entry)
        noted = run.outcome(entry.id)
        if not noted:
            screen.print(level_line(level, counter, "not reached"))
            if name:
                screen.labelled(NAME_LABELS.get(level, "Step Name:"), name)
            continue
        screen.print(level_line(level, counter, noted["outcome"], noted.get("seconds")))
        if name:
            screen.labelled(NAME_LABELS.get(level, "Step Name:"), Text(name, style="bold"))
        for label, text in noted.get("details", []):
            screen.labelled(label, text)
        if noted.get("details"):
            screen.blank()


def _labels(entry: Entry) -> tuple[str, str, str]:
    if entry.kind == "preflight":
        return "preflight", entry.preflight_counter, entry.component
    if entry.kind == "drift":
        return "drift check", "", ""
    if entry.kind == "published":
        return "published outputs", "", ""
    if entry.kind == "migrate":
        return "migrate step", "", "migrate"
    return "step", entry.step_counter, entry.step


def _enter(screen, entry: Entry, shown: tuple) -> tuple:
    stage, repository, component, phase = shown
    now = "before" if entry.kind in ("preflight", "drift", "published") else "run"
    if now != stage:
        screen.stage("before anything changes" if now == "before" else "run sequence")
        repository = component = phase = None
    if now == "before":
        return now, None, None, None
    part = (entry.kind in ("upgrade", "migrate"), entry.repository)
    if part != repository:
        screen.blank()
        screen.print(level_line("repository", entry.repository_counter))
        screen.labelled("Repo Name:", Text(entry.repository, style="bold"))
        repository, component, phase = part, None, None
    if entry.component != component:
        screen.blank()
        screen.print(
            level_line("component", entry.component_counter + (" (Repository Owner)" if entry.owner else ""))
        )
        screen.labelled("Component Name:", Text(entry.component, style="bold"))
        component, phase = entry.component, None
    if entry.kind != "migrate" and entry.phase != phase:
        screen.blank()
        screen.print(level_line("phase", entry.phase_counter))
        screen.labelled("Phase Name:", Text(entry.phase, style="bold"))
        screen.blank()
        phase = entry.phase
    return now, repository, component, phase


def _pins(managed: Mapped, revision: str, infrastructure: str) -> set[str]:
    """The Client Infrastructure Repository commits a revision's workflows pin."""
    listing = gitops.git(
        "ls-tree", "--name-only", revision, ".github/workflows/", cwd=managed.path, check=False
    )
    if not listing.ok:
        return set()
    pattern = re.compile(rf"uses:\s*[^\s@]*/{re.escape(infrastructure)}/[^\s@]*@([0-9a-f]{{40}})")
    found = set()
    for name in listing.stdout.splitlines():
        content = gitops.show(managed.path, revision, name) or ""
        found.update(pattern.findall(content))
    return found


def _pinned_version(session: Session, releases: Releases, pins: set[str]) -> str:
    """The Platform Version a set of pinned infrastructure commits records."""
    infrastructure = session.instance.infrastructure
    if infrastructure is None or len(pins) != 1:
        return "unknown" if pins else "no pins found"
    pin = next(iter(pins))
    text = gitops.show(infrastructure.path, pin, ".dp-template/template-source.json")
    if text is None:
        return f"commit {pin[:12]} (fetch the infrastructure checkout to read it)"
    match = re.search(r'"commit"\s*:\s*"([0-9a-f]{40})"', text)
    commit = match.group(1) if match else None
    for version, document in releases.documents():
        sources = document.get("sources") or {}
        if commit and sources.get(INFRASTRUCTURE) == commit:
            return f"Platform {version}"
    return f"infrastructure commit {pin[:12]}"


def adoption(session: Session, releases: Releases, managed: Mapped) -> dict:
    """A Managed Repository's adoption, from GitHub, and from Git once the checkout has
    fetched its tags and `main`. Checks and reviewers matter only while the pull request is
    open."""
    screen = session.screen
    found: dict = {"repository": managed.name}
    with screen.console.status(f"Fetching {managed.name}") if session.interactive else nullcontext():
        fetched = gitops.git("fetch", "--quiet", "--tags", cwd=managed.path, check=False).ok
    found["fetched"] = fetched
    screen.blank()
    screen.labelled("Repo Name:", Text(managed.name, style="bold"))
    pull = github.pull_request(managed.path, github.ADOPTION_BRANCH, state="all")
    if pull is None:
        screen.labelled("Adoption PR:", "none open or merged")
    else:
        merge = (pull.get("mergeCommit") or {}).get("oid")
        state = "draft" if pull.get("isDraft") else str(pull.get("state", "")).lower()
        if merge:
            main = gitops.contains(managed.path, merge, "origin/main")
            tags = gitops.git(
                "tag", "--list", "v*", "--sort=-v:refname", cwd=managed.path, check=False
            ).stdout.split()
            released = bool(tags) and gitops.contains(managed.path, merge, tags[0])
            state = ("merged" if main else "merged on GitHub") + (
                f", released in {tags[0]}" if released else ", not released yet"
            )
        screen.labelled("Adoption PR:", f"#{pull['number']} {state} {pull.get('url', '')}".strip())
        if pull.get("state") == "OPEN":
            screen.labelled("Adoption Checks:", github.checks(pull))
            reviewers = github.reviewers(pull)
            if reviewers:
                screen.labelled("Adoption Reviewers:", ", ".join(reviewers))
        found["pull_request"] = {"number": pull["number"], "state": state}
    infrastructure = session.instance.infrastructure
    if infrastructure is not None:
        name = gitops.repository_name(infrastructure.remote)
        tags = gitops.git(
            "tag", "--list", "v*", "--sort=-v:refname", cwd=managed.path, check=False
        ).stdout.split()
        for environment, revision in (
            ("Integration", "origin/main"),
            ("Production", tags[0] if tags else None),
        ):
            if revision is None:
                screen.labelled(f"{environment} Pins:", "no release tag yet")
                continue
            if not gitops.git("rev-parse", "--verify", "--quiet", revision, cwd=managed.path, check=False).ok:
                screen.labelled(
                    f"{environment} Pins:", f"unknown: {revision} isn't fetched in {managed.stored}"
                )
                continue
            pinned = _pinned_version(session, releases, _pins(managed, revision, name))
            screen.labelled(
                f"{environment} Pins:",
                f"{pinned} ({revision})" if fetched else f"{pinned} ({revision}, as last fetched)",
            )
            found[environment.lower()] = pinned
    return found


def _candidate_release(session: Session, detail: dict) -> str | None:
    """On candidate commits, the Platform Version they're the candidate of and its newest
    published release, the one to take. Unknown or unpublished, nothing is suggested: an
    earlier release would take the instance back."""
    screen, releases = session.screen, session.releases
    version = targets.candidate_of(session.instance)
    detail["candidate_of"] = version
    if version is None:
        screen.labelled(
            "Candidate Of:", "unknown: the commits match no candidate this workstation upgraded to"
        )
        newest = next(reversed(releases.published()), None)
        if newest:
            screen.labelled("Newest Published:", f"Platform {newest}")
        return None
    screen.labelled("Candidate Of:", f"Platform {version}")
    release = releases.latest_of(family(version))
    if release is None:
        screen.note(f"Platform {version} isn't published yet.")
    return release


def status(session: Session) -> Result:
    screen = session.screen
    instance = session.instance
    run = record.active(instance.state)
    detail: dict = {"active": None}
    screen.header("status", ("instance", instance.key))
    if run is not None:
        screen.blank()
        screen.print(Text.assemble((run.data["goal"], "bold cyan"), f" · run {run.id}"))
        screen.print(
            Text.assemble(
                ("recorded ", "bold"),
                f"started {run.data['started'][11:16]}, last step {run.data['updated'][11:16]}; "
                "nothing is planned now",
            )
        )
        journey(session, run)
        stop = run.data.get("stop") or {}
        moved = [
            name
            for name, commit in run.data.get("commits", {}).items()
            if name in instance.components and gitops.head(instance.components[name].path) != commit
        ]
        screen.blank(2)
        if stop:
            where = next((entry.where for entry in run.entries if entry.id == stop.get("entry")), "the start")
            screen.print(
                Text.assemble(
                    ("Stopped at ", "bold"),
                    (where, "bold"),
                    f": {REASONS.get(stop.get('reason'), stop.get('reason'))}",
                )
            )
            screen.note(stop.get("message", ""))
        else:
            screen.print(
                Text("Under way, or ended without saying why: dp-instance resume carries on.", style="bold")
            )
        if moved:
            screen.note(
                f"{', '.join(moved)} moved to other commits since this run stopped, so it can't resume.",
                "red",
            )
        detail["active"] = {
            "run": run.id,
            "goal": run.data["goal"],
            "status": run.status,
            "stop": stop or None,
            "can_resume": not moved,
        }
    else:
        last = next(iter(record.records(instance.state)), None)
        screen.blank()
        if last is not None:
            screen.note(
                f"No run under way on this workstation. The last, {last.data['goal']}, ended {last.status}."
            )
        else:
            screen.note("No run on this workstation.")

    screen.stage("the instance")
    releases = session.releases
    upcoming: Next | None = None
    try:
        current = targets.current(instance, releases)
        screen.labelled("Repositories Record:", current.describe())
        detail["platform_version"] = current.version
        if current.version:
            release = targets.next_release(releases, current.version)
            if release:
                upcoming = Next(
                    f"dp-instance upgrade --to {release}",
                    f"takes Platform {release}, {targets.describe_next(current.version, release)}",
                )
        elif current.canary:
            release = _candidate_release(session, detail)
            if release:
                upcoming = Next(f"dp-instance upgrade --to {release}", f"Platform {release} is published")
    except Stop as stop:
        screen.labelled("Repositories Record:", f"unknown: {stop.message}")
    with screen.console.status("Reading the hold from the cluster") if session.interactive else nullcontext():
        found = hold.read(instance)
    if found.describe() is not None:
        screen.labelled("Hold:", found.describe(), "bold yellow" if found.held else None)
    detail["hold"] = {"held": list(found.held), "unknown": found.unknown}
    repositories = []
    for item in instance.repositories:
        if item.managed_by:
            continue
        screen.blank()
        screen.labelled("Repo Name:", Text(item.name, style="bold"))
        found = {"repository": item.name, "branch": gitops.branch(item.path)}
        recorded = targets.records_of(instance).get(item.template)
        if recorded is not None:
            screen.labelled("Repo Template:", recorded.describe())
        if found["branch"]:
            screen.labelled("Repo Branch:", found["branch"])
            if found["branch"] != "main":
                pull = github.pull_request(item.path, found["branch"])
                if pull:
                    screen.labelled("Repo PR:", f"{github.describe(pull)} {pull.get('url', '')}".strip())
                    found["pull_request"] = pull["number"]
        repositories.append(found)
    detail["repositories"] = repositories
    if instance.managed:
        screen.stage("adoption")
        detail["adoption"] = [adoption(session, releases, managed) for managed in instance.managed]

    steps: list[Next] = []
    if run is not None:
        steps = [Next(**item) for item in (run.data.get("stop") or {}).get("next", [])] or [
            Next("dp-instance resume", "carries on from where the run stopped")
        ]
    elif upcoming:
        steps = [upcoming]
    if steps:
        screen.next_block(steps, session.relative(run.folder) + "/" if run is not None else None)
    return Result("status", "done", None, tuple(steps), detail)
