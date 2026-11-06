"""`dp-instance` alone, or `status`: where the instance stands, planning nothing.

It states facts and suggests nothing, in up to five sections. Current Run, only while a run is
unfinished: its journey from its record, fetched from the instance's shared run record whenever
this workstation's copy isn't the newest, who started it, whether it's under way or stopped, and
how to continue from a stop. Then, holding the run lock briefly while no command holds it, it
reads each repository's `main` on GitHub in dp-instance's own copy at `main`, never your
checkout. Versions: the Platform Version the records hold, a newer one to upgrade to, and this
dp-instance's version and any newer one published. Repositories: what each records. Pending
Changes: the open change and its pull requests, each component `main` is ahead of by what it
last applied, each Managed Repository's Adoption PR still to merge and release, read now from
each component that declares it publishes one, or, where none does, each the last run left open
until a whole `verify` converges after it, and the files and commits in your checkouts, by the
rule `prepare` takes them by. Optional Handoffs: the outside work the newest completed apply
recommended, with each page, fetched here from the shared run record when another workstation
ran it. While another command holds the lock, it reads only the open change. It starts only the
Azure CLI's token and read-only component commands, in its copies: each component's `steps`, the
first repository's `outputs aggregate` and each `repo adoption`. It asks nothing: it prints and
returns.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from rich.text import Text

from . import adoption, applied, github, gitops, handoffs, installed, lock, places, record, shared, targets
from .instance import INFRASTRUCTURE
from .manifest import Manifest, family
from .outcome import Next, Result, Stop
from .screen import LABEL, NAME_LABELS, level_line, link
from .sequence import Entry
from .session import Session
from .walk import LEFT_OPEN, PARTS, adoption_left, part_of
from .walk.common import by_system

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
    "unmapped": "stopped: a repository of the instance isn't in the repository map",
    "conflict": "stopped: the change's branch conflicts",
    "fetch": "stopped: a target source couldn't be fetched",
    "development-keys": "stopped: a development repository's key isn't on this workstation",
    "unknown-platform-version": "stopped: which Platform Version the instance runs isn't known",
    "shared-record": "stopped: its record couldn't be uploaded to the instance's shared run record",
    "lock-lost": "stopped: its command lost the run lock",
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
    if entry.kind == "pull-request":
        return "pull request", "", entry.repository
    if entry.kind == "mark":
        return "applied commit", "", entry.component
    if entry.kind == "preview":
        return "step preview", entry.step_counter, entry.step
    return "step", entry.step_counter, entry.step


def _enter(screen, entry: Entry, shown: tuple) -> tuple:
    stage, repository, component, phase = shown
    now = part_of(entry)
    if now != stage:
        screen.stage(PARTS[now])
        repository = component = phase = None
    if now == "before":
        return now, None, None, None
    part = (now, entry.repository)
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
    if entry.kind in ("step", "preview") and entry.phase != phase:
        screen.blank()
        screen.print(level_line("phase", entry.phase_counter))
        screen.labelled("Phase Name:", Text(entry.phase, style="bold"))
        screen.blank()
        phase = entry.phase
    return now, repository, component, phase


def _candidate_release(session: Session, detail: dict, recorded: dict) -> str | None:
    """On candidate commits, the Platform Version they're the candidate of, and its newest
    published release, the one to take; nothing when it's unknown, since an earlier release would
    take the instance back."""
    releases = session.releases
    version = targets.candidate_version(session.instance, releases, recorded)
    detail["candidate_of"] = version
    if version is None:
        newest = next(reversed(releases.published()), None)
        if newest:
            session.screen.labelled("Newest Published:", newest)
        return None
    return releases.latest_of(family(version))


def _own_version(session: Session, detail: dict) -> None:
    """This dp-instance's version, and the newest published, when it's newer; or that `self
    update --to` pinned it."""
    own = installed.version()
    pinned = installed.pinned(session.instance.state)
    try:
        newest = next(reversed(session.releases.tools()), None)
    except Stop:
        newest = None
    detail["dp_instance"] = {"installed": own, "newest": newest, "pinned": pinned is not None}
    shown = own or "a local copy, not a published version"
    if pinned is not None:
        shown += (
            ", pinned by self update --to; dp-instance self update installs the newest and clears the pin"
        )
    elif newest is not None and not installed.at_least(own, newest):
        shown += f"; {newest} is published, which dp-instance self update installs"
    session.screen.labelled("dp-instance:", shown)


def _aggregate(
    session: Session, manifest: Manifest | None, copies: dict[str, Path]
) -> tuple[dict | None, str | None]:
    """The instance's published Component Outputs, read by the first repository's owner, which
    every other's depends on, from its copy at `main`, or None and why they weren't read; None
    too without a release to read them by."""
    first = next((item for item in (manifest.order if manifest else ()) if item.key in copies), None)
    if first is None:
        return None, None
    try:
        found, _ = applied.read(session.instance, manifest, copies[first.key])
    except Stop as stop:
        return None, stop.message
    return found, None


def _edits(session: Session, detail: dict, commits: dict[str, str], branch: str | None) -> list[str]:
    """What your checkouts hold that isn't part of the change: each repository's files not
    committed, by count, and its commits `main` on GitHub hasn't."""
    found = places.edits(session.instance, commits, branch)
    components = session.instance.components
    detail["edits"] = [components[key].template for key in found]
    files = {key: names for key, (names, _) in found.items() if names}
    if files:
        session.screen.blank()
        session.screen.print(Text("Uncommitted Files, not part of this change", style="bold"))
        for key, names in files.items():
            session.screen.note(
                f"{components[key].stored}: {len(names)} file{'s' if len(names) != 1 else ''}"
            )
    for key, (_, ahead) in found.items():
        if ahead:
            session.screen.blank()
            session.screen.labelled(
                "Unmerged Commits:", f"{components[key].stored}: {ahead} commit{'s' if ahead != 1 else ''}"
            )
    return detail["edits"]


def _open_change(session: Session, detail: dict) -> tuple[list[dict], list[dict], str | None]:
    """The instance's open change, each of its pull requests that's open and, when some merged
    and none is open, each closed without merging, which its reopening and merge complete, as
    GitHub answers."""
    try:
        found = session.located().change() if session.located().ready(create=False) else None
        if found is None:
            detail["open_change"] = None
            return [], [], None
        states = {
            item["url"]: github.state(session.instance.root, item["url"])
            for item in found.get("pull_requests") or []
        }
    except Stop as stop:
        session.screen.labelled("Change:", f"not read: {stop.message}")
        detail["open_change"] = None
        return [], [], None
    listed = [{**item, "state": states.get(item["url"])} for item in found.get("pull_requests") or []]
    open_now = [item for item in listed if item["state"] == "open"]
    # An upgrade partly merged waits for its closed pull requests; a Configuration Change applies
    # what merged.
    closed = (
        [item for item in listed if item["state"] == "closed"]
        if not open_now
        and found.get("kind") == "upgrade"
        and any(item["state"] == "merged" for item in listed)
        else []
    )
    detail["open_change"] = {
        "title": found.get("title"),
        "branch": found.get("branch"),
        "open": open_now,
        "closed": closed,
    }
    session.screen.labelled("Change:", found.get("title") or "")
    for item in listed:
        # The address while there's something to do in GitHub; once merged, its number will do.
        shown = (
            f"{item['repository']} #{item['url'].rsplit('/', 1)[-1]}"
            if item["state"] == "merged" and item.get("repository")
            else item["url"]
        )
        session.screen.labelled(
            "Pull Request:", Text.assemble(link(shown, item["url"]), f" ({item['state']})")
        )
    return open_now, closed, (found.get("branch") if open_now else None)


def _recorded_adoption(session: Session) -> list[dict]:
    """Each Adoption PR the last completed upgrade or configuration change left open, as its
    record keeps them, until a whole `verify` since then converged: what an instance whose
    components publish no adoption state says."""
    for item in record.records(session.instance.state, ("upgrade", "apply", "verify")):
        if item.kind == "verify":
            if item.data.get("converged"):
                return []
            continue
        if item.status == "complete":
            return item.data.get("adoption_prs") or []
    return []


def _adoption(
    session: Session, detail: dict, manifest: Manifest | None, copies: dict[str, Path]
) -> list[dict]:
    """Each Managed Repository's Adoption PR still to merge and release, or to release: read now
    from each component that declares it publishes one, or else as the last completed run left
    them open; `adoption_read` says which, and `failed` when a read now failed. The Completion
    Check passes only once each is merged and released."""
    screen = session.screen
    found: list[dict] | None = None
    detail["adoption_read"] = "recorded"
    try:
        live = adoption.read(session.instance, manifest, copies) if manifest is not None and copies else None
        if live is not None:
            found, detail["adoption_read"] = adoption.outstanding(live), "now"
    except Stop as stop:
        detail["adoption_read"] = "failed"
        screen.blank()
        screen.labelled("Adoption PR:", f"not read now: {stop.message}")
        screen.labelled("", "each one the last run left open follows, if any")
    if found is None:
        found = _recorded_adoption(session)
    detail["adoption_prs"] = found
    if found:
        screen.blank()
    for item in found:
        state = f" ({item['state']})" if item.get("state") else ""
        screen.labelled("Adoption PR:", f"{item['url']}{state}: {adoption_left(item)} it")
    if found:
        screen.labelled("Left Open:", LEFT_OPEN)
    return found


COMPONENTS = "config/infrastructure/components.json"


def _listed_differences(session: Session, detail: dict, copies: dict[str, Path]) -> None:
    """How the components Infrastructure lists differ from the repositories this instance maps:
    what `dp-infrastructure components` prints, read from `main`'s file so status starts nothing."""
    copy = copies.get(INFRASTRUCTURE)
    path = copy / COMPONENTS if copy is not None else None
    if path is None or not path.is_file():
        return
    try:
        members = json.loads(path.read_text(encoding="utf-8"))["components"]
        listed = {member["template"]: key for key, member in members.items()}
    except (ValueError, KeyError, TypeError, AttributeError):
        session.screen.labelled("Components:", f"{COMPONENTS} can't be read")
        return
    mapped = {*session.instance.components} - {INFRASTRUCTURE}
    differences = [
        *(
            f"{listed[template]}: listed in {COMPONENTS}, and no repository here is mapped to {template}"
            for template in sorted(listed.keys() - mapped - {INFRASTRUCTURE})
        ),
        *(
            f"{template}: mapped here, and not listed in {COMPONENTS}"
            for template in sorted(mapped - listed.keys())
        ),
    ]
    if differences:
        session.screen.blank()
    for difference in differences:
        session.screen.labelled("Component Differs:", difference)
    detail["component_differences"] = differences


def _pending(
    session: Session, detail: dict, aggregate: dict | None, unread: str | None, commits: dict[str, str]
) -> list[str]:
    """Each component `main` on GitHub is ahead of, by what it last applied: `apply` is pending
    for it, which also covers a change applied outside a run."""
    instance = session.instance
    if aggregate is None:
        if unread is not None:
            session.screen.labelled("Apply Pending:", f"not checked: {unread}")
        detail["pending"] = None
        return []
    copies = {key: instance.own(places.APPLY, key) for key in commits}
    found = applied.pending(instance, aggregate, applied.applied_commits(aggregate)[0], commits, copies)
    if found:
        session.screen.labelled("Apply Pending:", ", ".join(found))
    detail["pending"] = found
    return found


def _shared(session: Session, detail: dict) -> tuple[record.Record | None, lock.Holder | None]:
    """The instance's unfinished run, in this workstation's working copy, and who holds the run
    lock. Without its shared run record, it's this workstation's own run, and no holder is
    known; before the state account exists, as on a first deployment, that's all there is."""
    state = session.instance.state
    try:
        found = session.located()
        try:
            there = found.ready(create=False)
        except Stop as stop:
            if stop.detail.get("reason") != "unanswered" or found.account_exists():
                raise
            session.screen.blank()
            session.screen.labelled("Shared Run Record:", "none yet: the state account doesn't exist yet")
            detail["shared_record"] = None
            return shared.unnamed(state), None
        if not there:
            detail["shared_record"] = None
            return shared.unnamed(state), None
        held = lock.held(found)
        run = found.unfinished(record.runs_folder(state)) or shared.unnamed(state)
        # A shared run record a command can lock: status takes the lock, briefly, to read main.
        detail["lockable"] = True
    except Stop as stop:
        session.screen.blank()
        session.screen.labelled("Shared Run Record:", f"not read: {stop.message}")
        detail["shared_record"] = stop.message
        return record.active(state), None
    detail["shared_record"] = None
    holder = held[0] if held is not None else None
    if held is not None:
        session.screen.blank()
        session.screen.labelled(
            "Run Lock:", holder.describe() if holder else "just taken by a command that hasn't named itself"
        )
    detail["lock"] = asdict(holder) if holder is not None else None
    return run, holder


def _component_states(aggregate: dict | None) -> dict[str, str]:
    """Each registered component: `active` once it has an applied commit, `pending` until then."""
    if aggregate is None:
        return {}
    commits = applied.applied_commits(aggregate)[0]
    states = {}
    for item in aggregate.get("components", []):
        key = (
            ((item.get("registration") or {}).get("component") or {}) if isinstance(item, dict) else {}
        ).get("key")
        if isinstance(key, str):
            states[key] = "active" if commits.get(key) else "pending"
    return states


def _repositories(
    session: Session, detail: dict, manifest: Manifest | None, recorded: dict, aggregate: dict | None
) -> None:
    """Each Component Repository: its template source and its components, each with how far it
    has come, when Component Outputs could be read."""
    screen = session.screen
    states = _component_states(aggregate)
    members = {item.key: item.components for item in manifest.order} if manifest is not None else {}
    repositories = []
    for index, item in enumerate(item for item in session.instance.repositories if not item.managed_by):
        if index:
            screen.blank()
        name = gitops.parse_repository_url(item.remote)[1]
        screen.print(Text(name, style="bold"))
        found: dict = {"repository": item.name, "name": name}
        source = recorded.get(item.template)
        if source is not None:
            screen.labelled("Template:", source.repository.rsplit("/", 1)[-1])
            if source.version:
                screen.labelled("Template Version:", source.version)
            else:
                screen.labelled("Template Commit:", source.commit[:12])
        components = {
            member.key: states.get(member.key, "not registered") for member in members.get(item.template, ())
        }
        if components:
            found["components"] = components
            screen.print(Text("Components:", style=LABEL))
            for key, state in components.items():
                screen.note(f"- {key} ({state})" if aggregate is not None else f"- {key}")
        repositories.append(found)
    detail["repositories"] = repositories


def _optional_handoffs(session: Session, detail: dict) -> None:
    """The outside work the newest completed apply recommended, each system with its page, which
    a run another workstation walked keeps only in the shared run record."""
    detail["optional_handoffs"] = []
    try:
        located = session.located()
        if not located.ready(create=False):
            return
        run, optional, pages = located.optional_handoffs()
    except Stop:
        return
    if not optional:
        return
    folder = record.runs_folder(session.instance.state) / run / "handoffs"
    systems = {}
    for system, count in by_system(list(optional.values())).items():
        page = folder / f"{system}.md"
        if not page.exists() and f"{system}.md" in pages:
            folder.mkdir(parents=True, exist_ok=True)
            page.write_bytes(pages[f"{system}.md"])
        systems[system] = (count, session.relative(page), page)
        detail["optional_handoffs"].append({"system": system, "items": count, "page": session.relative(page)})
    handoffs.show_optional(session.screen, systems)


def _instance(session: Session, detail: dict, unread: str | None) -> None:
    """The instance as `main` on GitHub holds it, read in dp-instance's own copies at `main`,
    which status takes the run lock for, briefly. While another command holds the lock, only the
    open change is read, and ``unread`` says why the rest isn't."""
    screen, instance, releases = session.screen, session.instance, session.releases
    commits: dict[str, str] = {}
    copies: dict[str, Path] = {}
    recorded: dict = {}
    manifest = None
    screen.stage("Versions")
    try:
        if unread is not None:
            raise Stop("failed", unread.removeprefix("not read: "))
        commits, copies = places.mains(instance)
        recorded = places.records(copies)
        current = targets.standing(instance, releases, recorded)
        screen.labelled("Current Platform Version:", targets.current_version(current, releases))
        detail["platform_version"] = current.version
        release = None
        if current.version:
            release = targets.next_release(releases, current.version)
        elif current.canary:
            release = _candidate_release(session, detail, recorded)
        detail["upgrade_available"] = release
        if release:
            screen.labelled("Upgrade Available:", release)
        manifest = targets.manifest_of(instance, releases, recorded)
    except Stop as stop:
        screen.labelled("Current Platform Version:", unread or f"unknown: {stop.message}")
    _own_version(session, detail)
    aggregate, why = _aggregate(session, manifest, copies)
    screen.stage("Repositories")
    _repositories(session, detail, manifest, recorded, aggregate)
    _listed_differences(session, detail, copies)
    screen.stage("Pending Changes")
    _, _, branch = _open_change(session, detail)
    pending = _pending(session, detail, aggregate, why or unread, commits)
    left = _adoption(session, detail, manifest, copies)
    edits = _edits(session, detail, commits, branch)
    if not (detail.get("open_change") or pending or left or edits or why or unread):
        screen.note("None")
    if unread is None:
        _optional_handoffs(session, detail)


def status(session: Session) -> Result:
    screen = session.screen
    instance = session.instance
    detail: dict = {"active": None, "lock": None}
    screen.header("status", ("instance", instance.key))
    screen.reading("Reading the run record")
    run, holder = _shared(session, detail)
    live = run is not None and holder is not None and holder.run == run.id
    if run is not None:
        screen.stage("Current Run")
        screen.print(Text(run.data["goal"], style="bold cyan"))
        screen.labelled("Run:", run.id)
        screen.print(
            Text.assemble(
                ("recorded ", "bold"),
                f"started {run.data['started'][11:16]}, last step {run.data['updated'][11:16]}; "
                "nothing is planned now",
            )
        )
        started = run.data.get("started_by")
        if started:
            screen.labelled(
                "Started By:", f"{started.get('who')} on {started.get('host')}, {started.get('at')}"
            )
        for item in shared.unlocks(run):
            screen.labelled("Lock Cleared By:", f"{item.get('who')} on {item.get('host')}, {item.get('at')}")
        journey(session, run)
        stop = run.data.get("stop") or {}
        screen.blank(2)
        if live:
            screen.print(Text(f"Under way: {holder.describe()}.", style="bold"))
        elif stop:
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
                Text(
                    "Stopped without saying why, such as after a crash or a restart: dp-instance resume "
                    "carries on.",
                    style="bold",
                )
            )
        detail["active"] = {
            "run": run.id,
            "goal": run.data["goal"],
            "status": run.status,
            "started_by": started,
            "live": live,
            "stop": stop or None,
            "can_resume": not live,
        }
    screen.reading("Reading the instance's repositories")
    lockable = detail.pop("lockable", False)
    if holder is not None:
        _instance(session, detail, f"not read while {holder.describe()}")
    elif not lockable:
        # No shared run record to lock yet, so no command holds the lock: nothing else reads main's
        # copies meanwhile, and status creates no record.
        _instance(session, detail, None)
    else:
        try:
            with session.locked("status"):
                _instance(session, detail, None)
        except Stop as stop:
            _instance(session, detail, f"not read: {stop.message}")
    screen.read()
    # Only what continues a stopped run or clears a hung command's lock: the rest is stated above.
    steps: list[Next] = []
    if holder is not None and holder.run:
        steps.append(
            Next(
                f"dp-instance unlock {holder.run}",
                "clears the run lock, only if that command hangs while its process is alive; otherwise "
                "it releases the lock as it ends",
            )
        )
    if run is not None and not live:
        steps += [Next(**item) for item in (run.data.get("stop") or {}).get("next", [])] or [
            Next("dp-instance resume", "carries on from where the run stopped")
        ]
    if steps:
        screen.continuing(steps, session.relative(run.folder) + "/" if run is not None else None)
    return Result("status", "done", None, tuple(steps), detail)
