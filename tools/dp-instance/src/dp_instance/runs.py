"""The commands that run the sequence: `upgrade`, `apply` and `resume`.

`upgrade --to V` starts one run from the code into the platform. `apply` always starts a new
run on the checked-out code, its finished steps passing as no changes. `resume` carries on
from where the active run stopped, the one place a run's record decides where to start, and
refuses if the commits it's tied to have moved.
"""

from __future__ import annotations

import json

from . import gitops, record, targets
from .manifest import Manifest, Releases, family, parse
from .outcome import Next, Result, Stop, failed
from .record import Record
from .session import Session
from .walk import Run, at_first_hop


def _commits(session: Session, manifest: Manifest) -> dict[str, str]:
    mapped = session.instance.components
    return {item.key: gitops.head(mapped[item.key].path) for item in manifest.order if item.key in mapped}


def _previous_manifest(releases: Releases, target: Manifest) -> str | None:
    """The previous Platform Version's manifest that maps release snapshots to the development
    commits 2026.2's records name, which the first move reads; None once there's none."""
    previous = releases.previous_family(target.platform_version)
    if previous is None:
        return None
    for version in reversed(list(releases.published())):
        if family(version) != previous:
            continue
        text = releases.text(version)
        try:
            if "snapshots" in json.loads(text):
                return text
        except ValueError:
            continue
    return None


def _goal(session: Session, target: Manifest, channel: str) -> str:
    try:
        found = targets.current(session.instance, session.releases, applied=True)
        start = found.describe()
    except Stop:
        start = "its current release"
    if channel == "canary":
        return f"upgrade: {start} → candidate Platform {target.platform_version}"
    return f"upgrade: {start} → Platform {target.platform_version}"


def upgrade(session: Session, version: str | None, manifest_file: str | None) -> Result:
    releases = session.releases
    target, channel = targets.target(releases, version, manifest_file)
    target.check_install(session.own_commit())
    if channel == "stable":
        targets.check_may_take(session.instance, releases, target)
    mapped = session.instance.components
    first_hop = [
        item.key for item in target.order if item.key in mapped and at_first_hop(mapped[item.key].path)
    ]
    # The first move onto the release repositories reads it for a repository whose `main`
    # records `stable`, whatever the target's channel. Deleted at the 2026.4 stop.
    on_stable = any(
        session.instance.record(item.key, applied=True).channel == "stable"
        for item in target.order
        if item.key in mapped
    )
    previous = _previous_manifest(releases, target) if on_stable else None
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
        first_hop=first_hop,
        previous_manifest="previous-manifest.json" if previous else None,
    )
    run.manifest_path.write_text(target.text, encoding="utf-8")
    if previous:
        run.file("previous-manifest.json").write_text(previous, encoding="utf-8")
    run.data["commits"] = _commits(session, target)
    run.save()
    return Run(session, run, target).walk()


def apply(session: Session) -> Result:
    manifest = targets.running(session.instance, session.releases)
    manifest.check_install(session.own_commit())
    state = session.instance.state
    record.replace_active(state)
    change = session.options.change
    goal = f"apply: Platform {manifest.platform_version}, the checked-out code" + (
        f", {change}" if change else ""
    )
    run = Record.new(
        record.runs_folder(state),
        "apply",
        None,
        goal,
        change=change,
        manifest_origin=manifest.origin,
    )
    run.manifest_path.write_text(manifest.text, encoding="utf-8")
    run.data["commits"] = _commits(session, manifest)
    run.save()
    return Run(session, run, manifest).walk()


def resume(session: Session) -> Result:
    state = session.instance.state
    run = record.active(state)
    if run is None:
        raise failed(
            "No run is under way on this workstation.",
            Next("dp-instance apply", "starts a new run on the checked-out code"),
        )
    manifest = parse(
        run.manifest_path.read_text(encoding="utf-8"), run.data.get("manifest_origin", "the run's")
    )
    manifest.check_install(session.own_commit())
    mapped = session.instance.components
    moved = [
        name
        for name, commit in run.data.get("commits", {}).items()
        if name in mapped and gitops.head(mapped[name].path) != commit
    ]
    if moved:
        raise failed(
            f"The run can't resume: {', '.join(moved)} moved to other commits since it stopped.",
            Next(
                "dp-instance apply",
                "starts a new run on the checked-out code; finished steps pass as no changes",
            ),
        )
    run.data["status"], run.data["stop"] = "running", None
    run.save()
    return Run(session, run, manifest).walk()
