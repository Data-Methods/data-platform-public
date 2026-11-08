"""The read-only commands over the sequence: `preflight`, `plan` and `verify`.

`preflight` runs every component's own preflight, all at once, from `main`'s code or, with
`--to`, the target's, against copies of `origin/main` in `.dp-instance/preflight/`, its own
folder: it holds no run lock, starts no run and works signed out and before the state account
exists, each preflight saying what it needs. Like `plan`, it stops when the release `main`
records can't be named.

`plan` previews what `apply` would do once the change merges: `origin/main`, the open change's
branch while its pull requests are open, then your branch's commits, built in copies of its own
in `.dp-instance/plan/` as `prepare` builds them, edits that aren't committed left out and named.
It renders first, then plans every enabled platform step of every component in the release,
the phases after a merge included, read only, each with its outcome and whether it's
provisional, and with `--step`, one step alone. It runs no upgrade or migrate, pushes nothing and
opens no pull request, and its record never feeds `apply`. A step that skips across runs and
that nothing in the change reaches reports no change without planning, so `plan` can't find
drift there, which `apply`'s drift check finds. `verify` is the Completion Check, from
`origin/main`: when `main` is ahead of what each component's walk last applied, it says `apply`
is pending and runs the drift check, against the commits the walks applied; otherwise every
step's read-only verify, by phase, six phases at a time, each step shown with its own time; a
step that isn't converged says what's left in its own words, and a phase that waits for a
sign-in stops it for the sign-in, since its steps verified nothing. Either takes one step by its
full name, `repository/component/phase/step`. Each writes only its own record in
`.dp-instance/runs/`, which stays on this workstation. While a run is stopped, `plan` names it,
and `verify` refuses: the platform is part way through that run.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rich.text import Text

from . import gitops, places, targets
from .components import StepReports, built
from .manifest import Manifest, read_file
from .outcome import PREFLIGHT, WAITING_FOR, Next, Result, Stop, failed, naming_preflight
from .processes import PHASES_AT_ONCE
from .record import Record, runs_folder
from .screen import Block
from .sequence import Entry, parse_step
from .session import Session
from .walk import APPLY, Prepare, Run
from .walk.common import DRIFT_TEXT
from .walk.reading import refuse_earlier_release, walks


def _record(
    session: Session, kind: str, goal: str, manifest: Manifest, target: str | None = None, **extra
) -> Record:
    found = Record.new(
        runs_folder(session.instance.state), kind, target, goal, manifest_origin=manifest.origin, **extra
    )
    found.manifest_path.write_text(manifest.text, encoding="utf-8")
    session.name(found.id)
    return found


class View(Run):
    """A read-only walk over the sequence, or one step of it."""

    def single(self, name: str, kind: str = "step") -> Entry:
        """The step a full name, `repository/component/phase/step`, names, from its component's
        declaration alone, which the record holds as its only entry."""
        found = parse_step(name)
        known = {
            (repository.key, component.key)
            for repository in self.repositories
            for component in repository.components
        }
        entry = None
        if found in known:
            started = time.monotonic()
            entry = self.plan(self.declarations(only=found)).step(name, kind)
            self.timed("read", f"the steps of {found[1]}", started)
        if entry is None:
            raise failed(
                f"{name} names no step this instance runs: name one as repository/component/phase/step, "
                "the `entry` a run's JSON events give each step."
            )
        self.record.entries = [entry]
        return entry

    def reported(
        self, entry: Entry, block: Block, reports: StepReports, event: dict | None = None
    ) -> list[dict]:
        """The pull requests a step reported in a process that ran others, on its block."""
        self._pulls = {}
        for url, state in reports.pull_requests.get((entry.phase, entry.step), {}).items():
            self.show_pull_request(block, url, state, at_once=False)
        return self.pull_requests(block, event)


class Preview(Prepare):
    """`plan`: the change as `prepare` builds it, planned read only."""

    previewing_only = True

    def header(self) -> None:
        super().header()
        self.session.show_stopped()

    def walk(self, step: str | None = None) -> Result:
        self.header()
        try:
            if step:
                self.show_part("code")
                self.build()
                self.record.entries = []
                self.show_part("preview")
                entry = View.single(self, step, "preview")
                self.preview_group(entry)
            else:
                self.go()
        except KeyboardInterrupt:
            return self.stopped(Stop("failed", "Interrupted.", reason="interrupted"))
        except Stop as stop:
            return self.stopped(stop)
        return self.done(step)

    def done(self, step: str | None) -> Result:
        """The preview's record and result: each step's outcome, and the first change."""
        self.close_part()
        preview = self.record.data.get("preview") or {}
        entries = [entry for entry in self.record.entries if entry.kind == "preview"]
        change = next(
            (entry for entry in entries if (preview.get(entry.id) or {}).get("outcome") == "change"), None
        )
        summary = {
            "change": change.id if change is not None else None,
            "steps": [
                {"entry": entry.id, **(preview.get(entry.id) or {"outcome": "not planned"})}
                for entry in entries
            ],
            "left_out": self.record.data.get("left_out") or {},
        }
        self.record.file("plan.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        self.record.complete()
        self.screen.blank(2)
        left_out = self.record.data.get("left_out") or {}
        if change is None:
            message = f"No change at {entries[0].where}." if step and entries else "No changes."
            self.screen.print(Text(message, style="bold green"))
            return Result(
                "plan", "done", message, detail={"run": self.record.id, "change": None, "left_out": left_out}
            )
        steps = (Next("dp-instance prepare", "turns the change into pull requests to review and merge"),)
        self.screen.then(
            "Run dp-instance prepare to turn it into pull requests.", self.relative(self.record.folder) + "/"
        )
        message = f"{change.where} has a change." if step else f"The first change is at {change.where}."
        return Result(
            "plan", "done", message, steps, {"run": self.record.id, "change": change.id, "left_out": left_out}
        )


def plan(session: Session, step: str | None = None) -> Result:
    instance = session.instance
    open_change = session.shared.change() if session.shared is not None else None
    prepared = session.prepared((open_change or {}).get("run"))
    if prepared is not None and prepared.manifest_path.is_file():
        manifest = read_file(prepared.manifest_path)
    else:
        _, copies = places.mains(instance)
        manifest = targets.manifest_of(
            instance, session.releases, places.records(copies), given=tuple(session.candidates())
        )
    what = f"the step {step}" if step else "main, the open change and your branch"
    goal = f"plan: a preview of Platform {manifest.platform_version}, {what}; starts no run"
    change = (open_change or {}).get("change") or "Platform change"
    record = _record(
        session, "plan", goal, manifest, change=change, branch=gitops.run_branch(change), title="Preview"
    )
    copies = {
        item.key: places.clone(instance, places.PLAN, instance.components[item.key])
        for item in manifest.order
        if item.key in instance.components
    }
    return Preview(session, record, manifest, copies).walk(step)


class Preflights(View):
    """`preflight`: every component's own preflight, all at once, from `main`'s code or the
    target's, against copies of `origin/main` of its own."""

    def source_folder(self, repository: str) -> Path:
        return places.preflight_source(self.instance, repository)

    def walk(self) -> Result:
        self.header()
        self.show_part("preflight")
        target = self.record.data.get("target")
        try:
            if target:
                # The target's own steps, so a component new in it is asked for in its code.
                self.fetch_sources()
                sources = {key: self.source(key) for key in self.places}
                declarations = self.declarations(sources, project=sources)
            else:
                declarations = self.declarations()
            entries = self.plan(declarations).preflights()
            for entry in entries:
                entry.fetched = bool(target)
            self.record.entries = entries
            self.record.save()
            self.preflights(entries)
        except KeyboardInterrupt:
            return self.stopped(Stop("failed", "Interrupted.", reason="interrupted"))
        except Stop as stop:
            return self.stopped(stop)
        self.record.complete()
        self.close_part("preflight")
        message = f"Every preflight passed: {len(entries)} component{'s' if len(entries) != 1 else ''}."
        self.screen.blank(2)
        self.screen.print(Text(message, style="bold green"))
        return Result("preflight", "done", message, detail={"run": self.record.id})

    def stopped(self, stop: Stop) -> Result:
        """A stop names this command again where a run's names `dp-instance resume`: a preflight
        is no run to carry on."""
        reason = stop.detail.get("reason", stop.status)
        self.pause()
        again = Next(self.restart(), "runs every preflight again, once that's done")
        steps = (
            *(
                item
                for item in stop.next
                if not item.command.startswith(("dp-instance resume", "dp-instance preflight"))
            ),
            again,
        )
        detail = {key: value for key, value in stop.detail.items() if key != "reason"}
        self.record.stop(
            stop.status,
            reason,
            stop.message,
            entry=self._entry.id if self._entry is not None else None,
            next=[item.describe() for item in steps],
            **detail,
        )
        self.screen.blank()
        self.screen.note(stop.message, {"failed": "bold red"}.get(stop.status, "bold orange1"))
        self.screen.continuing(steps, self.relative(self.record.folder) + "/")
        return Result(
            "preflight", stop.status, stop.message, steps, {"run": self.record.id, "reason": reason, **detail}
        )


def preflight(session: Session, version: str | None, file: str | None) -> Result:
    """Every component's preflight, from `main`'s code or, with ``version`` or a candidate
    ``file``, the target's; it takes no run lock and never reaches the shared run record."""
    instance = session.instance
    _, copies = places.mains(instance, places.PREFLIGHT)
    if version is None and file is None:
        manifest = targets.manifest_of(instance, session.releases, places.records(copies))
        goal = f"preflight: every component's preflight, from Platform {manifest.platform_version}'s main"
        record = _record(session, "preflight", goal, manifest, command="dp-instance preflight")
    else:
        manifest, channel = targets.target(session.releases, version, file)
        candidate = "candidate " if channel == "canary" else ""
        option = f"--candidate {file}" if file else f"--to {manifest.platform_version}"
        goal = (
            f"preflight: every component's preflight, from {candidate}Platform "
            f"{manifest.platform_version}'s templates"
        )
        record = _record(
            session,
            "preflight",
            goal,
            manifest,
            target=manifest.platform_version,
            command=f"dp-instance preflight {option}",
            channel=channel,
        )
    listed = {item.key: copies[item.key] for item in manifest.order if item.key in copies}
    return Preflights(session, record, manifest, listed).walk()


class Verification(View):
    """`verify`: every step's read-only verify, or one step's, by phase, six phases at a time."""

    def walk(self, step: str | None = None) -> Result:
        self.header()
        self.show_part("platform")
        again = f"dp-instance verify --step {step}" if step else "dp-instance verify"
        try:
            if step:
                entries = [self.single(step)]
            else:
                declarations = self.declarations()
                if not walks(declarations):
                    raise refuse_earlier_release("The templates on main")
                pending = self.pending()
                if pending:
                    return self.apply_pending(pending)
                entries = self.plan(declarations).platform()
                self.record.entries = entries
        except Stop as stop:
            steps = stop.next
            if stop.status == "sign-in":
                steps = (*steps, Next(again, "checks again once signed in"))
            steps = naming_preflight(stop.status, stop.detail.get("reason"), steps)
            self.record.complete()
            self.screen.blank(2)
            self.screen.note(stop.message, "bold orange1")
            self.screen.continuing(steps, self.relative(self.record.folder) + "/")
            return Result("verify", stop.status, stop.message, steps, {"run": self.record.id})
        results = self.verify_phases(entries, step is not None)
        problems = []
        failed_phases: set[tuple[str, str, str, str]] = set()
        for entry in entries:
            call, reports = results[self.phase_of(entry)]
            found = self.verified(entry, call, reports, failed_phases)
            if found is not None:
                problems.append(found)
        signed_out = next((call for call, _ in results.values() if call.status == "sign-in"), None)
        # The Completion Check passed: what `status` reads to stop naming the Adoption PRs a run
        # left open.
        self.record.data["converged"] = step is None and signed_out is None and not problems
        self.record.complete()
        self.close_part("platform")
        self.screen.blank(2)
        if signed_out is not None:
            # A phase that couldn't sign in verified nothing: what's left can't be told yet.
            message = f"Not every step could be verified; a sign-in to renew: {signed_out.message}"
            self.screen.print(Text.assemble(("Not verified.", "bold orange1"), f" {message}"))
            steps = (*self.sign_in_next(signed_out), Next(again, "checks again once signed in"))
            self.screen.continuing(steps, self.relative(self.record.folder) + "/")
            return Result(
                "verify",
                "sign-in",
                message,
                steps,
                {"run": self.record.id, "sign_in": {"command": steps[0].command}},
            )
        if not problems:
            self.screen.print(
                Text.assemble(
                    ("Converged.", "bold green"),
                    " Nothing is left to change, and every step's own checks pass."
                    if step is None
                    else f" {entries[0].where} has nothing left to change, and its own checks pass.",
                )
            )
            return Result("verify", "done", "Converged.", detail={"run": self.record.id})
        count = "1 step isn't" if len(problems) == 1 else f"{len(problems)} steps aren't"
        self.screen.print(
            Text.assemble(("Not converged.", "bold red"), f" {count}, each saying what's left:")
        )
        for entry, messages in problems:
            self.screen.blank()
            self.screen.print(Text(entry.where, style="bold"))
            for message in messages:
                self.screen.print(Text(message))
        steps = (
            Next(
                "dp-instance apply",
                "applies what a step has left to change; outside work a step names, such as a pull "
                "request to merge and release, is done where it says, and no run does it",
            ),
            Next(again, "checks again once each is done"),
            PREFLIGHT,
        )
        self.screen.continuing(steps, self.relative(self.record.folder) + "/")
        return Result(
            "verify",
            "failed",
            "Not converged.",
            steps,
            {
                "run": self.record.id,
                "not_converged": [
                    {"entry": entry.id, "where": entry.where, "messages": messages}
                    for entry, messages in problems
                ],
            },
        )

    def pending(self) -> list[str]:
        """The components whose walk last applied another commit than `main`'s: `apply` is
        pending for them."""
        block = Block("verify", "", "", "", self.footer)
        _, applied = self.read_applied(block)
        main = self.record.data.get("main") or {}
        return [
            component.key
            for repository in self.repositories
            for component in repository.components
            if applied.get(component.key) != main.get(repository.key)
        ]

    def apply_pending(self, pending: list[str]) -> Result:
        """`main` is ahead of what's applied: the drift check, against the commits the walks last
        applied, and `apply`, which brings the platform to `main`."""
        entry = Entry(id="drift-check", kind="drift", description=DRIFT_TEXT)
        self.record.entries = [entry]
        self.show_part("before")
        block = self.block(entry)
        self._entry, self._block = entry, block
        self.enter(entry)
        self.drift_check(entry, block)
        self._entry = self._block = None
        self.record.data["converged"] = False
        self.record.complete()
        self.close_part("before")
        message = f"Apply pending: main is ahead of what {', '.join(pending)} last applied; nothing drifts."
        self.screen.blank(2)
        self.screen.print(
            Text.assemble(("Apply pending.", "bold orange1"), message.removeprefix("Apply pending."))
        )
        steps = (APPLY,)
        self.screen.then("Run dp-instance apply.", self.relative(self.record.folder) + "/")
        return Result(
            "verify", "done", message, steps, {"run": self.record.id, "pending": pending, "drift": False}
        )

    @staticmethod
    def phase_of(entry: Entry) -> tuple[str, str, str, str]:
        return entry.repository, entry.component, entry.command, entry.phase

    def verify_phases(self, entries: list[Entry], step: bool) -> dict:
        """Each phase's verify, or the one step's, six phases at a time, each step's events
        timed as they arrive."""
        phases = list(dict.fromkeys(self.phase_of(entry) for entry in entries))
        block = Block("verify", "", "", "", self.footer)
        self._progress = None
        done: list[tuple] = []

        def say() -> None:
            block.message = (
                f"Verifying {len(phases)} phase{'s' if len(phases) != 1 else ''}, "
                f"{PHASES_AT_ONCE} at a time: {len(done)} done"
            )

        def verify(phase):
            repository, _, command, name = phase
            reports = StepReports()
            only = ["--step", entries[0].step] if step else []
            call = self.cli(repository, command).run(
                "verify", "--phase", name, *only, "--json", *self.version, on_event=reports
            )
            done.append(phase)
            say()
            return phase, (call, reports)

        def work():
            built([self.cli(repository, command) for repository, _, command, _ in phases])
            return list(pool.map(verify, phases))

        say()
        with ThreadPoolExecutor(max_workers=PHASES_AT_ONCE) as pool:
            return dict(self.screen.run_live(block, work))

    def verified(
        self, entry: Entry, call, reports: StepReports, failed_phases: set[tuple[str, str, str, str]]
    ) -> tuple[Entry, list[str]] | None:
        """Show one step's verify with its own time; the step and what's left, in its own
        words, when it isn't converged. A phase whose process failed shows why once, on its
        first step."""
        self.enter(entry)
        block = self.block(entry)
        took = reports.seconds.get((entry.phase, entry.step))
        events = [event for event in call.finished(entry.step) if event.get("phase") == entry.phase]
        pulls = self.reported(entry, block, reports, events[-1] if events else None)
        if not events:
            if call.status == "done":
                self.finish(entry, block, "skipped", compact=True, took=took)
                return None
            if call.status == "sign-in":
                if self.phase_of(entry) not in failed_phases:
                    failed_phases.add(self.phase_of(entry))
                    block.add("Sign-In:", call.message)
                self.finish(entry, block, WAITING_FOR["sign-in"], took=took, pull_requests=pulls)
                return None
            message = f"not verified: its {entry.phase} phase's verify failed, as its first step says"
            if self.phase_of(entry) not in failed_phases:
                failed_phases.add(self.phase_of(entry))
                block.add("Failure:", call.message)
                block.add("Log:", self.relative(self.record.log_path))
                message = call.message
            self.finish(entry, block, "not verified", took=took, pull_requests=pulls)
            return entry, [message]
        event = events[-1]
        self.optional(event)
        status = event.get("status")
        if status == "converged":
            self.finish(entry, block, "converged", compact=True, took=took, pull_requests=pulls)
            return None
        messages = [str(item) for item in (event.get("verification") or {}).get("failures", [])]
        for failure in messages:
            block.add("Failure:", failure)
        for item in event.get("handoffs") or []:
            if item.get("required"):
                title = item.get("title", item.get("id"))
                block.add("Step Handoff:", title)
                messages.append(title)
        if status == "pending" and not messages:
            waits = next(
                (
                    line.split(": ", 1)[1]
                    for line in call.stderr
                    if line.startswith(f"Pending {entry.step}: ") and ": " in line
                ),
                "it waits on an earlier change or outside work",
            )
            block.add("Still Waiting:", waits)
            messages.append(waits)
        self.finish(
            entry,
            block,
            "pending" if status == "pending" else "not converged",
            took=took,
            pull_requests=pulls,
        )
        return entry, messages


def verify(session: Session, step: str | None = None) -> Result:
    session.refuse_while_stopped("verify")
    commits, copies = places.mains(session.instance)
    manifest = targets.manifest_of(
        session.instance, session.releases, places.records(copies), given=tuple(session.candidates())
    )
    what = f"the step {step}" if step else "every step"
    goal = f"verify: {what} of Platform {manifest.platform_version}, read only"
    record = _record(session, "verify", goal, manifest, main=commits)
    return Verification(session, record, manifest, copies).walk(step)
