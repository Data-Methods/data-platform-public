"""The commands that run the sequence: `upgrade`, `apply` and `resume`.

`upgrade` starts one run from the code into the platform: to the release `--to` names or, with
none named, the next stop, which it names first. `apply` always starts a new run on `main`'s
code, a Configuration Change, its finished steps passing as no changes. `resume` carries on from
where the active run stopped, the one place a run's record decides where to start. It refuses if
the commits it's tied to have moved, except forward on the run's own branch, as a step applied by
hand leaves them.
"""

from __future__ import annotations

from . import gitops, record, targets
from .manifest import Manifest, parse, published, version_key
from .outcome import Next, Result, Stop, failed
from .record import Record
from .session import Session
from .walk import Run


def _commits(session: Session, manifest: Manifest) -> dict[str, str]:
    mapped = session.instance.components
    return {item.key: gitops.head(mapped[item.key].path) for item in manifest.order if item.key in mapped}


def _goal(session: Session, target: Manifest, channel: str) -> str:
    try:
        found = targets.current(session.instance, session.releases, applied=True)
        start = found.describe()
    except Stop:
        start = "its current release"
    if channel == "canary":
        return f"upgrade: {start} → candidate Platform {target.platform_version}"
    return f"upgrade: {start} → Platform {target.platform_version}"


def _nothing_to_upgrade(session: Session, version: str) -> Result:
    """On the newest release: nothing to upgrade, though a run that stopped after its merges
    may still have steps to finish."""
    message = f"Nothing to upgrade: this instance runs Platform {version}, the newest release."
    steps = (
        (Next("dp-instance resume", "carries on the run under way, which hasn't finished"),)
        if record.active(session.instance.state) is not None
        else ()
    )
    session.screen.header("upgrade", ("instance", session.instance.key))
    session.screen.blank()
    session.screen.note(message, "bold green")
    if steps:
        session.screen.next_block(steps)
    return Result("upgrade", "done", message, steps, {"platform_version": version})


def upgrade(session: Session, version: str | None, manifest_file: str | None) -> Result:
    releases = session.releases
    chosen = None
    if version is None and manifest_file is None:
        start, version = targets.next_stop(session.instance, releases)
        if version is None:
            return _nothing_to_upgrade(session, start)
        chosen = f"Upgrading to Platform {version}, {targets.describe_next(start, version)}."
    target, channel = targets.target(releases, version, manifest_file)
    # Whether the instance may take the target comes before the install check, so an older
    # target never asks for an older dp-instance.
    if channel == "stable":
        targets.check_may_take(session.instance, releases, target)
    target.check_install(session.own_version())
    goal = _goal(session, target, channel)
    state = session.instance.state
    record.replace_active(state)
    run = Record.new(
        record.runs_folder(state),
        "upgrade",
        target.platform_version,
        goal,
        channel=channel,
        change=target.change,
        manifest_origin=manifest_file or f"platform-release/{target.platform_version}",
        chosen=chosen,
    )
    run.manifest_path.write_text(target.text, encoding="utf-8")
    run.data["commits"] = _commits(session, target)
    run.save()
    return Run(session, run, target).walk()


def _check_on_merged_main(session: Session, manifest: Manifest) -> None:
    """Stop unless every repository the run walks is on `main`, and its `main` is `origin/main`
    as just fetched: what a configuration change applies is merged on GitHub."""
    mapped = session.instance.components
    keys = [item.key for item in manifest.order if item.key in mapped]
    elsewhere = {key: gitops.branch(mapped[key].path) for key in keys}
    elsewhere = {key: branch for key, branch in elsewhere.items() if branch != "main"}
    if elsewhere:
        shown = "; ".join(f"{key} is on {branch or 'a detached HEAD'}" for key, branch in elsewhere.items())
        raise failed(
            f"A configuration change applies main, and {shown}: merge your change to main first.",
            Next(
                "dp-instance apply", "applies main once the change is merged and each repository is on main"
            ),
        )
    behind, ahead = [], []
    for key in keys:
        path = mapped[key].path
        gitops.remote_git("fetch", "--quiet", "origin", "main", cwd=path)
        merged = gitops.main_commit(path)
        if gitops.head(path) != merged:
            (behind if gitops.contains(path, "HEAD", merged) else ahead).append(key)
    if not behind and not ahead:
        return
    shown = [f"{key}'s main is behind origin/main" for key in behind] + [
        f"{key}'s main has commits origin/main doesn't" for key in ahead
    ]
    raise failed(
        f"A configuration change applies main as merged on GitHub, and {'; '.join(shown)}"
        + (": merge your change through its pull request first." if ahead else "."),
        *(
            Next(f"git -C {mapped[key].stored} pull --ff-only", "brings its main to origin/main")
            for key in behind
        ),
    )


def _check_names_every_repository(session: Session, manifest: Manifest) -> None:
    """Stop when a mapped repository's template isn't in the release the instance runs: apply
    never leaves it out. A later release that includes it deploys it: when one is published, the
    upgrade is the next step."""
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
        steps = (
            Next(
                "dp-instance upgrade",
                f"takes the next release; Platform {including[-1]} is the first to include the {templates}",
            ),
        )
    raise Stop(
        "failed",
        f"{release}, which this instance runs, doesn't include the {templates} of {', '.join(unnamed)}, "
        f"which the map holds; nothing has changed. {later} that includes {it} deploys {it}; until "
        f"then, {they} can't be applied.",
        reason="not-in-release",
        unnamed=unnamed,
        next=steps,
    )


def apply(session: Session) -> Result:
    """A Configuration Change: the platform brought to the configuration on `main`, where its
    pull request merged first. A step that commits does so on a branch named from the run, and
    the run's pull requests are titled with the components whose configuration it applies."""
    manifest = targets.running(session.instance, session.releases)
    manifest.check_install(session.own_version())
    _check_names_every_repository(session, manifest)
    _check_on_merged_main(session, manifest)
    state = session.instance.state
    record.replace_active(state)
    run = Record.new(
        record.runs_folder(state),
        "apply",
        None,
        f"apply: Platform {manifest.platform_version}, main's configuration",
        manifest_origin=manifest.origin,
    )
    run.data["change"] = f"Configuration {run.id}"
    run.manifest_path.write_text(manifest.text, encoding="utf-8")
    # The `main` commits the run applies; its commits follow each repository as steps commit.
    run.data["main"] = _commits(session, manifest)
    run.data["commits"] = dict(run.data["main"])
    run.save()
    return Run(session, run, manifest).walk()


def _moved(run: Record, mapped: dict) -> tuple[list[str], dict[str, str]]:
    """The repositories that moved since the run stopped, as the run refuses them, and the one it
    carries on in, with its new commit, as the step it stopped at, applied by hand, leaves it: that
    step's repository, on the run's own branch, moved only forward from the commit the run
    recorded, by commits and no merge, with nothing uncommitted. A merge could bring in what the
    drift check never saw, such as a configuration change merged to `main`."""
    branch = gitops.run_branch(run.data["change"]) if run.data.get("change") else None
    stopped = (run.data.get("stop") or {}).get("entry")
    repository = next((entry.repository for entry in run.entries if entry.id == stopped), None)
    moved, forward = [], {}
    for name, commit in run.data.get("commits", {}).items():
        if name not in mapped:
            continue
        path = mapped[name].path
        head = gitops.head(path)
        if head == commit:
            continue
        if (
            branch is not None
            and name == repository
            and gitops.branch(path) == branch
            and gitops.contains(path, commit, head)
            and not gitops.git("rev-list", "--merges", f"{commit}..{head}", cwd=path).stdout.strip()
            and not gitops.uncommitted(path)
        ):
            forward[name] = head
        else:
            moved.append(name)
    return moved, forward


def resume(session: Session) -> Result:
    state = session.instance.state
    run = record.active(state)
    if run is None:
        raise failed(
            "No run is under way on this workstation.",
            Next("dp-instance apply", "starts a new run on main's code"),
        )
    manifest = parse(
        run.manifest_path.read_text(encoding="utf-8"), run.data.get("manifest_origin", "the run's")
    )
    manifest.check_install(session.own_version())
    mapped = session.instance.components
    moved, forward = _moved(run, mapped)
    if moved:
        # A new run applies only `main`.
        on_main = all(
            gitops.branch(mapped[name].path) == "main" for name in run.data["commits"] if name in mapped
        )
        raise failed(
            f"The run can't resume: {', '.join(moved)} moved to other commits since it stopped.",
            *(
                (
                    Next(
                        "dp-instance apply",
                        "starts a new run on main's code; finished steps pass as no changes",
                    ),
                )
                if on_main
                else ()
            ),
        )
    # A step applied by hand committed on the run's own branch: the run carries on from there, and
    # the step it stopped at plans again.
    run.data["commits"].update(forward)
    for name in run.data.get("commits", {}):
        files = gitops.uncommitted(mapped[name].path) if name in mapped else []
        if files:
            shown = ", ".join(files[:3]) + (f" and {len(files) - 3} more" if len(files) > 3 else "")
            raise failed(
                f"The run can't resume: {name} has uncommitted changes to tracked files ({shown}). "
                "A run commits everything it writes, so these are a step's unfinished output or "
                "someone's edits.",
                Next(
                    f"git -C {mapped[name].stored} restore --staged --worktree -- .",
                    "discards them; the step writes its files again when the run resumes",
                ),
                Next("dp-instance resume", "carries on once the checkout is clean"),
            )
    run.data["status"], run.data["stop"] = "running", None
    # A resumed run never skips a plan: whatever happened while it stood still, every step plans.
    run.data.setdefault("plan_skip_off", "the run resumed")
    run.save()
    return Run(session, run, manifest).walk()
