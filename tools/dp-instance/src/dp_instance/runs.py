"""The commands that walk: `prepare`, `apply` and `resume`.

`prepare` turns a change into pull requests: a Configuration Change, the commits on your branch,
or, with `--to`, an upgrade to the next required stop, which it never starts without `--to`. An
instance has one open change, recorded in its shared run record: while its pull requests are
open, `prepare` adds to it, keeping its kind and target, an upgrade's re-run where its branch
hasn't it, and `prepare --to` refuses unless it re-prepares that upgrade. `prepare --to` also
refuses while `main` is ahead of what the platform applied, naming `apply`, and while any
Managed Repository's Adoption PR from the previous upgrade is unreleased, read now, or can't be
read; where no component reads them, it asks at a terminal. A change whose pull requests were
all closed without merging is no longer the open change; one partly merged, with the rest
closed, waits for those to be reopened and merged. A merged change no apply has applied doesn't
block a Configuration Change's `prepare`, which builds on `main` and names it. `apply` brings
the platform to `main`, as merged on GitHub, refusing while any repository the open change
touches has no merged pull request, naming each, or while the repositories on `main` record
different Platform Versions, naming `resume` when a stopped prepare can finish the upgrade.
`prepare --to` or `apply` replaces a stopped run, a prepare or an apply, only after one question
naming who started it, where it stopped and what was approved, whoever started it: without a
terminal, a decision its ID approves, the only ID either takes. Nothing is replaced silently: a
plain `prepare` runs beside a stopped run and names it. `resume` carries on the instance's
stopped run: an apply from where it stopped, wherever it started, finishing the commits it
planned; a prepare again from its start, with the target and the commits of your branch it took.
It marks where it picks up in the run's log; the time the run stood still counts as its waiting.
A run another person walked last it resumes only after a question naming who started it, where
it stopped, who approved what and who cleared its lock; without a terminal the question is a
decision its ID approves. Before the state account exists, as at a first deployment, `prepare`
refuses: the open change lives in the shared run record, so the first configuration merges by
hand and `apply` applies it.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import adoption, applied, github, gitops, installed, places, record, shared, targets
from .components import Log
from .manifest import Manifest, parse, published, version_key
from .outcome import Next, Result, Stop, failed
from .record import Record, now
from .session import Session
from .walk import Apply, Prepare, holds
from .walk.stops import held_on

# The stop a resume ends rule C after, by the reason the run stopped; any other failed stop is a
# failure.
RESUMED_AFTER = {
    "declined": "a decline",
    "sign-in": "a failure",
    "drift-check": "a stop in the drift check",
    "handoff": "outside work",
    "pending": "outside work",
    "runs": "outside work",
    "values": "a stop for values",
}
CHANGE = "Platform change"
CONFIGURATION = "Configuration change"


def _start(
    session: Session, run: Record, replaces: Record | None, *, at_once: bool, named: bool = True
) -> None:
    """Upload the new run to the instance's shared run record and name it on the run lock. It's
    the instance's unfinished run at once when it replaces nothing, or replaces the stopped run
    ``at_once``; a run that isn't ``named`` runs beside the stopped run and never stands for the
    instance's. A run that can't be uploaded or named is marked as never started, then removed.
    Before the state account exists, it stays here."""
    if not _walked_here(session, run):
        if replaces is not None and at_once:
            session.mark_replaced(replaces.id, run)
        return
    try:
        session.shared.publish(run)
        if replaces is None and named:
            session.shared.name(run)
        elif replaces is not None and at_once:
            session.mark_replaced(replaces.id, run)
    except Stop as stop:
        # Whatever a removal leaves, such as a file Windows held open, is never a run.
        run.data["status"] = record.NOT_STARTED
        run.save()
        shutil.rmtree(run.folder, ignore_errors=True)
        raise Stop(
            "failed",
            f"{stop.message} The run didn't start; nothing has changed.",
            reason=stop.detail.get("reason", "shared-record"),
        ) from None


def _walked_here(session: Session, run: Record) -> bool:
    """The run is walked by this command: the record names who walked each step, and it uploads
    at every step boundary while the command holds the run lock; before the state account
    exists, it joins the shared run record once a step has created the account. Whether it holds
    the lock."""
    run.by = session.operator["who"]
    if session.lock is None:
        run.join = session.join
        return False
    run.shared = session.shared
    session.name(run.id)
    return True


def _check_names_every_repository(session: Session, manifest: Manifest) -> None:
    """Stop when a mapped repository's template isn't in the release `main` records: a run never
    leaves it out. A later release that includes it deploys it: when one is published, preparing
    it is the next step."""
    unnamed = sorted(key for key in session.instance.components if manifest.repository(key) is None)
    if not unnamed:
        return
    stable = published(manifest.origin)
    release = f"{'' if stable else 'candidate '}Platform {manifest.platform_version}"
    later = "A later Platform Version" if stable else "A later candidate"
    templates, it, they = ("template", "it", "it") if len(unnamed) == 1 else ("templates", "them", "they")
    including = [
        version
        for version, document in (session.releases.documents() if stable else [])
        if version_key(version) > version_key(manifest.platform_version)
        and isinstance(document.get("sources"), dict)
        and any(key in document["sources"] for key in unnamed)
    ]
    steps = ()
    if including:
        # Newest first: the last is the first to include it.
        first, upcoming = including[-1], targets.next_release(session.releases, manifest.platform_version)
        steps = (
            Next(
                f"dp-instance prepare --to {upcoming}",
                f"prepares the next release; Platform {first} is the first to include the {templates}",
            ),
        )
    raise Stop(
        "failed",
        f"{release}, which this instance's main records, doesn't include the {templates} of "
        f"{', '.join(unnamed)}, which the map holds; nothing has changed. {later} that includes {it} "
        f"deploys {it}; until then, {they} can't be applied.",
        reason="not-in-release",
        unnamed=unnamed,
        next=steps,
    )


# The open change -------------------------------------------------------------------------


def _open_change(session: Session) -> tuple[dict | None, dict[str, str]]:
    """The instance's open change and each of its pull requests' state, as GitHub answers."""
    found = session.shared.change() if session.shared is not None else None
    if found is None:
        return None, {}
    states = {
        item["url"]: github.state(session.instance.root, item["url"])
        for item in found.get("pull_requests") or []
    }
    return found, states


def _naming(found: dict, states: dict[str, str], wanted: str) -> list[str]:
    return [
        f"{item['repository']}'s {item['url']}"
        for item in found.get("pull_requests") or []
        if states.get(item["url"]) == wanted
    ]


def _settled(
    session: Session, found: dict | None, states: dict[str, str]
) -> tuple[dict | None, dict[str, str]]:
    """The open change once no pull request of it is open: one whose pull requests were all
    closed is no longer the open change; in a Configuration Change, a closed pull request drops
    out, so what merged applies. An upgrade keeps them: the Platform Version needs every part."""
    if found is None or "open" in states.values() or "closed" not in states.values():
        return found, states
    if _closed(found, states):
        session.shared.record_change(None)
        return None, {}
    if found.get("kind") == "upgrade":
        return found, states
    kept = [item for item in found.get("pull_requests") or [] if states.get(item["url"]) != "closed"]
    dropped = {item["repository"] for item in found.get("pull_requests") or []} - {
        item["repository"] for item in kept
    }
    found = {
        **found,
        "pull_requests": kept,
        "repositories": [name for name in found.get("repositories") or [] if name not in dropped],
    }
    session.shared.record_change(found)
    return found, {url: state for url, state in states.items() if state != "closed"}


def _closed(found: dict | None, states: dict[str, str]) -> bool:
    """Whether every pull request of the open change was closed without merging."""
    return found is not None and bool(states) and all(state == "closed" for state in states.values())


def _partly_merged(found: dict | None, states: dict[str, str]) -> bool:
    """Whether some of the open change's pull requests merged and the rest were closed."""
    return (
        found is not None
        and "open" not in states.values()
        and "merged" in states.values()
        and "closed" in states.values()
    )


def _reopen(found: dict, states: dict[str, str]) -> tuple[Next, ...]:
    return tuple(
        Next(
            f"gh pr reopen {item['url']}",
            f"reopens {item['repository']}'s pull request, closed while the change's others merged: "
            "merge it in GitHub, then dp-instance apply",
        )
        for item in found.get("pull_requests") or []
        if states.get(item["url"]) == "closed"
    )


def _refuse_partly_merged(found: dict, states: dict[str, str], command: str) -> Stop:
    """An upgrade partly merged, the rest closed, waits for those to be reopened and merged."""
    return Stop(
        "failed",
        f"The instance's open upgrade, {found['title']}, is partly merged: "
        + "; ".join(_naming(found, states, "merged"))
        + " merged, and "
        + "; ".join(_naming(found, states, "closed"))
        + f" closed without merging. Reopen and merge each closed one before {command}; nothing has "
        "changed.",
        reason="partly-merged",
        next=_reopen(found, states),
    )


def _unopened(found: dict | None) -> list[str]:
    """Each repository the open change touches that has no pull request yet, as when its push
    failed."""
    if found is None:
        return []
    opened = {item.get("repository") for item in [*found.get("pull_requests", []), *found.get("merged", [])]}
    return [name for name in found.get("repositories") or [] if name not in opened]


def _main_manifest(session: Session, recorded: dict) -> Manifest:
    """The release `main` records; repositories that record different Platform Versions name
    `resume` when a stopped prepare can finish the upgrade they're part way through."""
    try:
        return targets.manifest_of(
            session.instance, session.releases, recorded, given=tuple(session.candidates())
        )
    except Stop as stop:
        previous = session.stopped()
        if stop.detail.get("reason") != "mixed" or previous is None or previous.kind != "prepare":
            raise
        target = previous.data.get("target")
        if not target:
            raise
        raise Stop(
            stop.status,
            stop.message,
            next=(
                Next(
                    "dp-instance resume",
                    f"finishes run {previous.id}, the stopped prepare of Platform {target}: merge the pull "
                    "requests it opens, then dp-instance apply",
                ),
                *stop.next,
            ),
            **stop.detail,
        ) from None


def _check_applied(
    session: Session, manifest: Manifest, commits: dict[str, str], copies: dict, command: str
) -> None:
    """The next stop is measured from what the platform applied, read as the drift check reads
    it, failing closed: while `main` is ahead of it, `apply` comes first."""
    first = next((item for item in manifest.order if item.key in copies), None)
    if first is None:
        return
    included = {
        component.key
        for repository in manifest.order
        if repository.key in copies
        for component in repository.components
    }
    found, last = applied.checked(
        session.instance,
        manifest,
        copies[first.key],
        included,
        Next(command, "once what the platform applied can be read"),
    )
    ahead = applied.pending(session.instance, found, last, commits, copies)
    if not ahead:
        return
    raise Stop(
        "failed",
        f"main is ahead of what the platform applied for {', '.join(ahead)}: the next required stop is "
        "taken from what the platform runs, so dp-instance apply comes first; nothing has changed.",
        reason="apply-pending",
        pending=ahead,
        next=targets.apply_steps(
            targets.records_of(session.instance),
            f"brings {', '.join(ahead)} to main, reviewing each change",
        ),
    )


def _check_branches(session: Session, found: dict | None, states: dict, version: str) -> None:
    """An upgrade starts from what the platform applied: its migrate units read it, and an edit
    would go in before them, against the old schema. Your branch's commits join it afterwards."""
    prepared = session.prepared(found.get("run")) if found is not None else None
    sources = (prepared.data.get("sources") or {}) if prepared is not None else {}
    merged = {
        item["repository"]: (sources[item["repository"]]["commit"],)
        for item in (found or {}).get("pull_requests") or []
        if states.get(item["url"]) == "merged" and item["repository"] in sources
    }
    ahead = []
    for key, mapped in session.instance.components.items():
        targets.fetch_main(mapped.path)
        kept = tuple(
            commit
            for commit in merged.get(key, ())
            if gitops.git("cat-file", "-e", f"{commit}^{{commit}}", cwd=mapped.path, check=False).ok
        )
        if gitops.unmerged(mapped.path, "refs/remotes/origin/main", "HEAD", kept):
            ahead.append(mapped.stored)
    if not ahead:
        return
    raise Stop(
        "failed",
        f"{', '.join(ahead)} {'has' if len(ahead) == 1 else 'have'} commits main doesn't, and an upgrade "
        "starts from what the platform applied; nothing has changed.",
        reason="branch-ahead",
        branches=ahead,
        next=(
            Next(
                "dp-instance prepare",
                "turns your commits into pull requests; merge them, then dp-instance apply",
            ),
            Next(
                f"git switch main, then dp-instance prepare --to {version}",
                "upgrades first; a plain dp-instance prepare on your branch then adds your commits",
            ),
        ),
    )


ADOPTION_QUESTION = "Is each Managed Repository's Adoption PR merged and released?"


def _ask_adoption(session: Session, command: str) -> None:
    """Where no component reads its Managed Repositories' Adoption PRs, as before the
    capability, a person says each is merged and released, at a terminal; without one, or with
    `n`, `prepare --to` refuses."""
    if not session.instance.managed:
        return
    said = (
        "No component of the release this instance runs reads its Managed Repositories' Adoption PRs, "
        "and an upgrade is prepared only once each is merged and released."
    )
    if session.interactive:
        session.screen.blank()
        session.screen.note(said, "bold")
        if session.screen.choose([("y", "yes"), ("n", "no")], ADOPTION_QUESTION) == "y":
            return
        raise Stop(
            "failed",
            f"{said} Nothing has changed.",
            reason="adoption",
            next=(Next(command, "once each is merged and released"),),
        )
    raise Stop(
        "failed",
        f"{said} Without a terminal, nothing asks whether they are; nothing has changed.",
        reason="adoption-unread",
        next=(Next(command, "at a terminal: asks whether each is merged and released"),),
    )


def _check_adoption(session: Session, manifest: Manifest, copies: dict, command: str) -> None:
    """An upgrade is prepared only once every Managed Repository's Adoption PR from the previous
    one is released, read now; a read that fails refuses too, and where no component reads
    them, a person says so at a terminal."""
    try:
        found = adoption.read(session.instance, manifest, copies)
    except Stop as stop:
        raise Stop(
            "failed",
            f"Each Managed Repository's Adoption PR couldn't be read now: {stop.message} An upgrade is "
            "prepared only once each is released; nothing has changed.",
            reason="adoption-unread",
            next=(Next(command, "once each can be read: prepares this upgrade"),),
        ) from None
    if found is None:
        _ask_adoption(session, command)
        return
    left = adoption.outstanding(found)
    if not left:
        return
    raise Stop(
        "failed",
        "An upgrade is prepared only once each Managed Repository's Adoption PR from the previous one is "
        "released, and "
        + "; ".join(f"{item['repository']}'s {item['url']} is {item['state']}" for item in left)
        + ". Nothing has changed; Configuration Changes go on meanwhile.",
        reason="adoption",
        adoption_prs=left,
        next=(Next("dp-instance status", "names each Adoption PR until it's released"),),
    )


# The commands ----------------------------------------------------------------------------


def _reading(session: Session, command: str) -> None:
    """The command's opening lines, then a spinner while a run reads what it needs before it
    starts, its open change, `main` and the releases, so the screen is never blank meanwhile."""
    session.screen.header(command, ("instance", session.instance.key))
    session.screen.reading("Reading the instance's repositories")


NOTHING = "Nothing to prepare: no edits, and no newer Platform Version is published."
CANCEL = ("q", "cancel")


def _edits_said(found: dict[str, tuple[list[str], int]]) -> str:
    files = sum(len(names) for names, _ in found.values())
    commits = sum(ahead for _, ahead in found.values())
    return ", ".join(
        part
        for part in (
            f"{files} file{'s' if files != 1 else ''}" if files else "",
            f"{commits} commit{'s' if commits != 1 else ''}" if commits else "",
        )
        if part
    )


def _pick_upgrade(session: Session, current: targets.Current) -> str | None:
    """The Platform Version a person picks: the latest next one, the newest fix, or any other
    the required stops allow."""
    first, others = targets.upgrades(session.releases, current)
    shown = [(version, f"{version} ({said})") for version, said in first]
    if others:
        shown.append(("", "Other"))
    keys = {str(index): item for index, item in enumerate(shown, 1)}
    choices = [*((key, label) for key, (_, label) in keys.items()), CANCEL]
    answer = session.screen.menu("Upgrade to which Platform Version?", choices)
    if answer == CANCEL[0]:
        return None
    if keys[answer][0]:
        return keys[answer][0]
    keys = {str(index): version for index, version in enumerate(others, 1)}
    answer = session.screen.menu("Which Platform Version?", [*keys.items(), CANCEL])
    return None if answer == CANCEL[0] else keys[answer]


def _ask_intent(
    session: Session, current: targets.Current | None, commits: dict[str, str]
) -> tuple[bool, str | None]:
    """What a bare `prepare` at a terminal is for, asked before any run starts: whether to go on,
    and the Platform Version when it's an upgrade."""
    found = places.edits(session.instance, commits, None)
    first, others = targets.upgrades(session.releases, current) if current is not None else ([], [])
    choices = []
    if found:
        choices.append(("edits", f"Config Edits ({_edits_said(found)})"))
    if first or others:
        choices.append(("upgrade", "Upgrade Platform"))
    if not choices:
        session.screen.read()
        session.screen.blank()
        session.screen.note(NOTHING, "bold green")
        return False, None
    keys = {str(index): item[0] for index, item in enumerate(choices, 1)}
    answer = session.screen.menu(
        "What do you want to prepare?",
        [*((str(index), item[1]) for index, item in enumerate(choices, 1)), CANCEL],
    )
    if answer == CANCEL[0]:
        return False, None
    if keys[answer] == "edits":
        return True, None
    version = _pick_upgrade(session, current)
    return version is not None, version


def prepare(session: Session, version: str | None, manifest_file: str | None) -> Result:
    _reading(session, "prepare")
    if session.lock is None:
        raise failed(
            "prepare records the open change in the instance's shared run record, in its state account, "
            "which doesn't exist yet. On a first deployment, merge the configuration by hand, then run "
            "dp-instance apply; nothing has changed.",
            Next("dp-instance apply", "applies main's configuration"),
        )
    instance, releases = session.instance, session.releases
    found, states = _settled(session, *_open_change(session))
    if _partly_merged(found, states):
        raise _refuse_partly_merged(found, states, "preparing another change")
    open_now = found is not None and "open" in states.values()
    merged = found is not None and bool(states) and all(state == "merged" for state in states.values())
    commits, copies = places.mains(instance)
    recorded = places.records(copies)
    given = tuple(session.candidates())
    try:
        current = targets.standing(instance, releases, recorded, given=given)
    except Stop:
        current = None
    if current is not None:
        session.screen.header(
            "prepare", ("Current Platform Version", targets.current_version(current, releases))
        )
    if version is None and manifest_file is None and session.interactive and not open_now:
        going, version = _ask_intent(session, current, commits)
        if not going:
            return Result("prepare", "done", "Nothing prepared.")
    upgrade = version is not None or manifest_file is not None
    if upgrade:
        target, channel = targets.target(releases, version, manifest_file)
        if open_now and (found.get("target") != target.platform_version or found.get("kind") != "upgrade"):
            raise Stop(
                "failed",
                f"The instance's open change, {found['title']}, has open pull requests: "
                + "; ".join(_naming(found, states, "open"))
                + f". An instance has one open change: merge or close them before preparing Platform "
                f"{target.platform_version}; nothing has changed.",
                reason="open-change",
                next=(Next("dp-instance prepare", "adds your edits to the open change instead"),),
            )
        option = f"--candidate {manifest_file}" if manifest_file else f"--to {target.platform_version}"
        command = f"dp-instance prepare {option}"
        _approving_only_the_replace(session, session.stopped(), command)
        running = targets.manifest_of(instance, releases, recorded, given=given)
        if not open_now:
            _check_applied(session, running, commits, copies, command)
            _check_branches(session, found, states, target.platform_version)
        start = targets.standing(instance, releases, recorded, given=given, verified=True)
        targets.check_next_stop(start, releases, target, pinned=installed.pinned(instance.state) is not None)
        _check_adoption(session, running, copies, command)
        change, title = target.change, _title(target, channel, manifest_file)
        candidate = "candidate " if channel == "canary" else ""
        manifest, goal = (
            target,
            f"prepare: {start.describe()} → {candidate}Platform {target.platform_version}",
        )
        start_version = start.version or start.candidate
        kind, change_target = "upgrade", target.platform_version
    elif open_now:
        # Edits added to the open change keep it what it is: an upgrade's prepare carries its
        # target, whose upgrade of each repository finds the branch already has it.
        change, title = found["change"], found["title"]
        kind, change_target = found.get("kind") or "configuration", found.get("target")
        manifest = _change_manifest(session, found)
        if manifest is None and kind == "upgrade":
            if change_target not in releases.published():
                raise Stop(
                    "failed",
                    f"The instance's open change, {title}, was prepared from a candidate manifest this "
                    "workstation can't read, so its edits can't be added here; nothing has changed.",
                    reason="open-change",
                    next=(Next("dp-instance prepare --candidate <candidate manifest>", "adds them with it"),),
                )
            manifest = releases.manifest(change_target)
        manifest = manifest or targets.manifest_of(instance, releases, recorded, given=given)
        channel, goal = found.get("channel") or "stable", f"prepare: your edits added to {title}"
        command, start_version = "dp-instance prepare", None
    else:
        manifest = targets.manifest_of(instance, releases, recorded, given=given)
        channel = "stable" if published(manifest.origin) else "canary"
        change, title = CHANGE, CONFIGURATION
        goal = f"prepare: a Configuration Change of Platform {manifest.platform_version}"
        command, start_version = "dp-instance prepare", None
        kind, change_target = "configuration", None
    previous = session.stopped()
    # `prepare --to` replaces a stopped run after one question; a plain prepare runs beside it,
    # naming it.
    replaces = previous if upgrade else None
    if upgrade:
        _replacing(session, previous, command)
    elif previous is not None:
        session.show_stopped()
    run = Record.new(
        record.runs_folder(instance.state),
        "prepare",
        manifest.platform_version if kind == "upgrade" else None,
        goal,
        channel=channel,
        change=change,
        branch=gitops.run_branch(change),
        title=title,
        start=start_version,
        command=command,
        manifest_origin=manifest_file or manifest.origin,
        change_kind=kind,
        change_target=change_target,
        # The first build of an upgrade takes no edits: its migrate units come first.
        starts_upgrade=upgrade and not open_now,
        # A plain prepare beside a stopped run, which `resume` carries on instead of this one.
        beside=previous is not None and not upgrade,
        open={"merged": [*found.get("pull_requests", []), *found.get("merged", [])]} if merged else {},
        started_by={**session.operator, "at": now()},
    )
    run.manifest_path.write_text(manifest.text, encoding="utf-8")
    run.save()
    _start(session, run, replaces, at_once=True, named=previous is None or replaces is not None)
    places.tidy(instance)
    prepared = {
        item.key: places.clone(instance, places.PREPARE, instance.components[item.key])
        for item in manifest.order
        if item.key in instance.components
    }
    return Prepare(session, run, manifest, prepared).walk()


def _title(target: Manifest, channel: str, manifest_file: str | None) -> str:
    """A prepared upgrade's pull requests' title, which their squash merge keeps: `Platform V`,
    or on canary `Platform V from candidate N`, N from the candidate manifest's file name."""
    title = f"Platform {target.platform_version}"
    if channel != "canary":
        return title
    name = Path(manifest_file or "").name
    match = re.fullmatch(r"candidate-(\d+)\.json", name)
    return f"{title} from {f'candidate {match.group(1)}' if match else name}"


def _change_manifest(session: Session, found: dict) -> Manifest | None:
    """The manifest the open change's prepare ran, from the shared run record."""
    run = session.prepared(found.get("run"))
    if run is None or not run.manifest_path.is_file():
        return None
    return parse(run.manifest_path.read_text(encoding="utf-8"), run.data.get("manifest_origin", "the run's"))


def apply(session: Session) -> Result:
    """The platform brought to `main`, as merged on GitHub: a Configuration Change or an
    upgrade, once its pull requests are merged."""
    _reading(session, "apply")
    instance = session.instance
    found, states = _settled(session, *_open_change(session))
    if _partly_merged(found, states):
        raise _refuse_partly_merged(found, states, "dp-instance apply")
    unopened = _unopened(found)
    if unopened:
        stopped = session.stopped()
        raise Stop(
            "failed",
            f"The instance's open change, {found['title']}, touches {', '.join(unopened)}, which has no "
            "pull request yet, so main holds only part of it; nothing has changed.",
            reason="unopened",
            unopened=unopened,
            next=(
                Next("dp-instance resume", "finishes the stopped prepare, which opens it")
                if stopped is not None and stopped.kind == "prepare"
                else Next("dp-instance prepare", "prepares the open change again, opening it"),
            ),
        )
    if found is not None and "open" in states.values():
        raise Stop(
            "failed",
            f"The instance's open change, {found['title']}, has pull requests that aren't merged: "
            + "; ".join(
                [
                    *(f"{item} (open)" for item in _naming(found, states, "open")),
                    *(f"{item} (closed without merging)" for item in _naming(found, states, "closed")),
                ]
            )
            + ". Review and merge each in GitHub first; nothing has changed.",
            reason="unmerged",
            next=(*_reopen(found, states), Next("dp-instance apply", "once each is merged")),
        )
    applies = (
        found["run"]
        if found is not None and states and all(state == "merged" for state in states.values())
        else None
    )
    commits, copies = places.mains(instance)
    recorded = places.records(copies)
    manifest = _main_manifest(session, recorded)
    _check_names_every_repository(session, manifest)
    previous = session.stopped()
    _replacing(session, previous, "dp-instance apply")
    reviewed = (
        (session.prepared(applies).data.get("preview") or {}) if applies and session.prepared(applies) else {}
    )
    run = Record.new(
        record.runs_folder(instance.state),
        "apply",
        None,
        f"apply: Platform {manifest.platform_version}, main",
        command="dp-instance apply",
        manifest_origin=manifest.origin,
        applies=applies,
        reviewed=reviewed,
        started_by={**session.operator, "at": now()},
    )
    run.manifest_path.write_text(manifest.text, encoding="utf-8")
    # The `main` commits the run applies, which a resume finishes.
    run.data["main"] = commits
    run.save()
    _start(session, run, previous, at_once=True)
    places.tidy(instance)
    return Apply(session, run, manifest, copies).walk()


def _resumed_after(run: Record) -> str | None:
    """Why resuming ends the walk's plan skip, rule C, by the stop the run resumes; None after a
    decision, a step's plan or the drift check's going on without the phases it couldn't plan,
    whether `--approve` or a person at a terminal approves it. A step applied by hand shows only
    as a decision's step with nothing left to apply, which the walk judges: `decision_resumed`."""
    stop = run.data.get("stop") or {}
    reason = stop.get("reason")
    if reason == "decision":
        return None
    after = RESUMED_AFTER.get(reason) or ("a failure" if run.status == "failed" else None)
    return f"the run resumed after {after}" if after else "the run resumed"


def _picks_up(run: Record) -> str:
    """Where a resume picks the run up, as its log says: the step it stopped at or, when it
    stopped outside a step or its process ended without a stop, after the last step it finished."""
    stop = run.data.get("stop") or {}
    why = f"it stopped ({stop.get('reason')})" if stop else "its process ended without saying why"
    stopped = next((entry for entry in run.entries if entry.id == stop.get("entry")), None)
    if stopped is not None:
        return f"carries on from {stopped.where}, where {why}"
    finished = [entry for entry in run.entries if run.finished(entry.id)]
    if not finished:
        return f"carries on from its start; {why}"
    return f"carries on after {finished[-1].where}, the last step it finished; {why}"


def _who(item: dict) -> str:
    return f"{item.get('who') or 'someone'} on {item.get('host') or 'another workstation'}"


def _same(one: dict, other: dict) -> bool:
    """Whether two records name the same account: by its object ID when both have one."""
    if one.get("oid") and other.get("oid"):
        return one["oid"] == other["oid"]
    return one.get("who") == other.get("who")


def _facts(run: Record) -> list[tuple[str, str]]:
    """What a question about a stopped run names: who started it, who walked it last when that
    was someone else, where it stopped, who approved what in an apply, and who cleared its lock. A
    prepare approves nothing: its preview's decisions are what review approves later."""
    walked = _walked(run)
    started = run.data.get("started_by") or {}
    approved = [
        f"{entry.where} by {noted.get('by') or 'someone'}"
        for entry in run.entries
        if (noted := run.outcome(entry.id)) and (noted.get("decisions") or noted.get("outcome") == "approved")
    ]
    # A step a hold was applied for keeps its approval, though it hasn't applied yet.
    kept = {state.get("step") for state in (run.data.get("holds") or {}).values() if state.get("approved")}
    approved += [f"{entry.where}, with its hold" for entry in run.entries if entry.id in kept]
    return [
        ("Started By:", f"{_who(started)}, {started.get('at')}" if started else "unknown"),
        *(
            [("Last Walked By:", f"{_who(walked[-1])}, {walked[-1].get('at')}")]
            if walked and walked[-1] is not started
            else []
        ),
        ("Stopped:", run.stopped_at().removeprefix("stopped ")),
        *([] if run.kind == "prepare" else [("Approved:", "; ".join(approved) or "no change yet")]),
        *(
            ("Hold On:", f"{entry.component}'s {entry.step}, until {entry.holds_until}; `{command}` lifts it")
            for entry, command in held_on(run, _ran(run).platform_version)
        ),
        *(("Lock Cleared By:", f"{_who(item)}, {item.get('at')}") for item in shared.unlocks(run)),
    ]


def _ran(run: Record) -> Manifest:
    """The manifest a run ran."""
    return parse(run.manifest_path.read_text(encoding="utf-8"), run.data.get("manifest_origin", "the run's"))


def _walked(run: Record) -> list[dict]:
    """Who started the run, then each who resumed it."""
    return [item for item in (run.data.get("started_by"), *run.data.get("resumed_by", [])) if item]


def _identifier(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _answered(session: Session, identifier: str) -> bool:
    """Whether `--approve` answered the question with this ID; the answer never reaches a step."""
    if identifier not in session.options.approvals:
        return False
    session.options = replace(
        session.options, approvals=tuple(item for item in session.options.approvals if item != identifier)
    )
    return True


def _ask(
    session: Session,
    run: Record,
    identifier: str,
    said: str,
    question: str,
    choice: str,
    *,
    reason: str,
    summary: str,
    declined: tuple[str, Next],
    next: tuple[Next, ...],
) -> None:
    """Ask a question about the stopped run, naming its facts: answered by `--approve` with its
    ID, at a terminal by a keypress, and otherwise a decision stop naming ``next``; ``declined``
    is what a `n` says and names."""
    if _answered(session, identifier):
        return
    facts = _facts(run)
    screen = session.screen
    screen.blank()
    screen.note(said, "bold")
    for label, text in facts:
        screen.labelled(label, text)
    if session.interactive:
        if screen.choose([("y", choice), ("n", "leave it")], question) == "y":
            return
        raise Stop("decision", declined[0], reason="declined", next=(declined[1],))
    told = "; ".join(f"{label.rstrip(':').lower()} {text}" for label, text in facts)
    raise Stop(
        "decision",
        f"{said} {told[0].upper()}{told[1:]}. {question}",
        reason=reason,
        decision={"id": identifier, "summary": summary, "run": run.id},
        next=next,
    )


def _take_over(session: Session, run: Record) -> None:
    """Carry on a run another account walked last only after a question."""
    walked = _walked(run)
    if not walked or _same(walked[-1], session.operator):
        return
    identifier = _identifier(run.id, run.data.get("stop"), walked[-1])
    _ask(
        session,
        run,
        identifier,
        f"Run {run.id} ({run.data['goal']}) is {walked[-1].get('who') or 'someone else'}'s.",
        f"Take run {run.id} over and carry it on?",
        "take it over",
        reason="take-over",
        summary=f"take over run {run.id}",
        declined=(
            f"Run {run.id} wasn't taken over; nothing has changed.",
            Next("dp-instance resume", "asks again"),
        ),
        next=(Next(f"dp-instance resume --approve {identifier}", "takes the run over and carries it on"),),
    )


def _replace_id(run: Record | None, command: str) -> str | None:
    """The ID that answers the question about replacing the stopped run: drawn from the facts it
    shows, as the record last stood, and this command and its target, so it never replaces a run
    that changed since, nor answers another."""
    if run is None:
        return None
    return _identifier("replace", command, run.id, _facts(run), run.data.get("updated"))


def _approving_only_the_replace(session: Session, run: Record | None, command: str) -> None:
    """`--approve` answers only the question about replacing a stopped run here, and any other ID
    is refused, so no step's decision rides in on a new run."""
    identifier = _replace_id(run, command)
    others = [item for item in session.options.approvals if item != identifier]
    if others:
        raise failed(
            f"{command.split(' --')[0]} takes --approve only to answer its question about replacing a "
            f"stopped run, and {', '.join(others)} answers none; nothing has changed."
        )


def _replacing(session: Session, run: Record | None, command: str) -> None:
    """A new run replaces the stopped run, a prepare or an apply, only after a question, whoever
    started it: nothing is replaced silently."""
    _approving_only_the_replace(session, run, command)
    identifier = _replace_id(run, command)
    if run is None:
        return
    _ask(
        session,
        run,
        identifier,
        f"Run {run.id} ({run.data['goal']}) is stopped, and a new run replaces it.",
        f"Replace run {run.id} with a new run?",
        "replace it",
        reason="replace",
        summary=f"replace run {run.id}",
        declined=(
            f"Run {run.id} wasn't replaced; nothing has changed.",
            Next("dp-instance resume", "carries the stopped run on"),
        ),
        next=(
            Next(f"{command} --approve {identifier}", "replaces the stopped run with a new one"),
            Next("dp-instance resume", "carries the stopped run on instead"),
        ),
    )


def resume(session: Session) -> Result:
    run = session.stopped()
    completed = session.shared.completed if session.lock is not None else None
    if run is None and completed is not None:
        # The run completed, and its last upload failed: this command finished uploading it.
        message = f"Run {completed.id} had completed; the instance's shared run record says so now."
        session.screen.header("resume", ("instance", session.instance.key), ("run", completed.id))
        session.screen.blank()
        session.screen.note(message, "bold green")
        return Result("resume", "done", message, (), {"run": completed.id})
    if run is None:
        raise failed(
            "No run is stopped on this instance.",
            Next("dp-instance apply", "brings the platform to main"),
        )
    _take_over(session, run)
    manifest = parse(
        run.manifest_path.read_text(encoding="utf-8"), run.data.get("manifest_origin", "the run's")
    )
    instance = session.instance
    mapped = {
        item.key: instance.components[item.key] for item in manifest.order if item.key in instance.components
    }
    if run.kind == "prepare":
        return _resume_prepare(session, run, manifest, mapped)
    holds.after_stop(run)
    # A stopped apply finishes the commits it planned.
    copies = {
        key: places.at(instance, places.APPLY, item, run.data["main"][key]) for key, item in mapped.items()
    }
    resumed_after = _resumed_after(run)
    # The step a decision's resume carries on at, which the walk checks still has its change to
    # apply; never the drift check, which plans every phase again.
    stopped = (run.data.get("stop") or {}).get("entry")
    at = next((entry for entry in run.entries if entry.id == stopped and entry.kind != "drift"), None)
    run.data.pop("decision_resumed", None)
    if resumed_after is None and at is not None:
        run.data["decision_resumed"] = at.id
    picks_up = _picks_up(run)
    # The time since the run last wrote its record, at its stop, is time it waited.
    stood = (datetime.now().astimezone() - datetime.fromisoformat(run.data["updated"])).total_seconds()
    run.data["waiting"] = round(run.data.get("waiting", 0) + max(stood, 0), 1)
    run.data["status"], run.data["stop"] = "running", None
    # A run resumed after any stop but a decision skips no plan: whatever happened while it stood
    # still, every step plans. Before the drift check finished, nothing can skip yet, and the
    # check it runs again is as fresh as a run's first.
    if resumed_after is not None and "drift_plans" in run.data:
        run.data.setdefault("plan_skip_off", resumed_after)
    run.data.setdefault("resumed_by", []).append({**session.operator, "at": now()})
    run.save()
    Log(run.log_path).write(f"== {record.now()} dp-instance resume: run {run.id} {picks_up}")
    if _walked_here(session, run):
        session.shared.publish(run)
        session.shared.name(run)
    return Apply(session, run, manifest, copies).walk()


def _resume_prepare(session: Session, run: Record, manifest: Manifest, mapped: dict) -> Result:
    """A stopped prepare starts again from its start, with its target and the commits of your
    branch it took: it changed nothing on the platform, so every step plans again."""
    stood = (datetime.now().astimezone() - datetime.fromisoformat(run.data["updated"])).total_seconds()
    Prepare.start_again(run)
    run.data["waiting"] = round(run.data.get("waiting", 0) + max(stood, 0), 1)
    run.data["status"], run.data["stop"] = "running", None
    run.data.setdefault("resumed_by", []).append({**session.operator, "at": now()})
    run.save()
    Log(run.log_path).write(
        f"== {record.now()} dp-instance resume: run {run.id} starts again from its start, with the "
        "commits it took"
    )
    if _walked_here(session, run):
        session.shared.publish(run)
        session.shared.name(run)
    copies = {key: places.clone(session.instance, places.PREPARE, item) for key, item in mapped.items()}
    return Prepare(session, run, manifest, copies).walk()
