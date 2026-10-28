"""`dp-instance` alone, or `status`: where the instance stands, planning nothing.

It shows the active run's journey from its record, each part with its time and each step's
pull requests as it reported them, then what the repositories record, this dp-instance's
version and any newer one published, with the install a next release needs before its
upgrade; each of its own two parts, the run record and the instance, ends with its time. It
reads the run record and Git, and starts no component CLI, so it answers quickly from any
workstation. It asks nothing: it prints, names what comes next and returns.
"""

from __future__ import annotations

from rich.text import Text

from . import gitops, installed, record, targets
from .manifest import family
from .outcome import Next, Result, Stop
from .screen import NAME_LABELS, level_line
from .sequence import Entry
from .session import Session
from .walk import PARTS, part_of

REASONS = {
    "decision": "stopped for review",
    "declined": "stopped: the change wasn't approved",
    "handoff": "waiting for an external handoff",
    "values": "waiting for values only a person supplies",
    "sign-in": "waiting for a sign-in",
    "runs": "waiting for runs to finish",
    "pending": "waiting for a step still pending",
    "failed": "stopped on a failure",
    "preflight": "stopped: a preflight failed",
    "interrupted": "interrupted",
    "drift": "stopped on drift",
    "unapplied": "stopped: a configuration change merged to main isn't applied",
    "already-at-target": "stopped: the upgrade is already under way",
    "unmapped": "stopped: a repository of the instance isn't in the repository map",
    "fetch": "stopped: a target source couldn't be fetched",
}


def journey(session: Session, run: record.Record) -> None:
    """The run as its record left it: every step's outcome and time, and its details, and each
    part that ended with its time."""
    screen = session.screen
    shown = (None, None, None, None)
    entries = run.entries
    parts = run.data.get("parts", {})
    for index, entry in enumerate(entries):
        shown = _enter(screen, entry, shown)
        level, counter, name = _labels(entry)
        noted = run.outcome(entry.id)
        if not noted:
            screen.print(level_line(level, counter, "not reached"))
            if name:
                screen.labelled(NAME_LABELS.get(level, "Step Name:"), name)
        else:
            screen.print(level_line(level, counter, noted["outcome"], noted.get("seconds")))
            if name:
                screen.labelled(NAME_LABELS.get(level, "Step Name:"), Text(name, style="bold"))
            for label, text in noted.get("details", []):
                screen.labelled(label, text)
            if noted.get("details"):
                screen.blank()
        part = part_of(entry)
        if part in parts and (index + 1 == len(entries) or part_of(entries[index + 1]) != part):
            screen.part(PARTS[part], sum(part_of(item) == part for item in entries), parts[part])


def _labels(entry: Entry) -> tuple[str, str, str]:
    if entry.kind == "preflight":
        return "preflight", entry.preflight_counter, entry.component
    if entry.kind == "drift":
        return "drift check", "", ""
    if entry.kind == "migrate":
        return "migrate step", "", "migrate"
    if entry.kind == "upgrade":
        return "upgrade step", "", "upgrade"
    return "step", entry.step_counter, entry.step


def _enter(screen, entry: Entry, shown: tuple) -> tuple:
    stage, repository, component, phase = shown
    now = part_of(entry)
    if now != stage:
        screen.stage(PARTS[now])
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
    if entry.kind not in ("upgrade", "migrate") and entry.phase != phase:
        screen.blank()
        screen.print(level_line("phase", entry.phase_counter))
        screen.labelled("Phase Name:", Text(entry.phase, style="bold"))
        screen.blank()
        phase = entry.phase
    return now, repository, component, phase


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


def _own_version(session: Session, detail: dict) -> None:
    """This dp-instance's version, and the newest published, when it's newer."""
    own = installed.version()
    try:
        newest = next(reversed(session.releases.tools()), None)
    except Stop:
        newest = None
    detail["dp_instance"] = {"installed": own, "newest": newest}
    shown = own or "a local copy, not a published version"
    if newest is not None and not installed.at_least(own, newest):
        shown += f"; {newest} is published, which dp-instance update installs"
    session.screen.labelled("dp-instance:", shown)


def status(session: Session) -> Result:
    screen = session.screen
    instance = session.instance
    run = record.active(instance.state)
    detail: dict = {"active": None}
    screen.header("status", ("instance", instance.key))
    screen.begin("the run record")
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

    screen.begin("the instance")
    screen.stage("the instance")
    releases = session.releases
    upcoming: tuple[Next, ...] = ()
    try:
        current = targets.current(instance, releases)
        screen.labelled("Repositories Record:", current.describe())
        detail["platform_version"] = current.version
        if current.version:
            release = targets.next_release(releases, current.version)
            if release:
                upcoming = targets.upgrade_steps(
                    releases,
                    release,
                    f"takes Platform {release}, {targets.describe_next(current.version, release)}",
                )
        elif current.canary:
            release = _candidate_release(session, detail)
            if release:
                upcoming = targets.upgrade_steps(releases, release, f"Platform {release} is published")
    except Stop as stop:
        screen.labelled("Repositories Record:", f"unknown: {stop.message}")
    _own_version(session, detail)
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
        repositories.append(found)
    detail["repositories"] = repositories
    screen.end()

    steps: list[Next] = []
    if run is not None:
        steps = [Next(**item) for item in (run.data.get("stop") or {}).get("next", [])] or [
            Next("dp-instance resume", "carries on from where the run stopped")
        ]
    elif upcoming:
        steps = list(upcoming)
    if steps:
        screen.next_block(steps, session.relative(run.folder) + "/" if run is not None else None)
    return Result("status", "done", None, tuple(steps), detail)
