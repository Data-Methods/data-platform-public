"""The read-only commands over the sequence: `plan` and `verify`.

`plan` is an optional preview of the checked-out code: it starts no run and changes
nothing, walks the steps in order, ends at the first step with a change and lists the steps
after it unplanned. `verify` is the completion check: every step's read-only verify, by phase,
six phases at a time, each step shown with its own time; a step that isn't converged says
what's left in its own words. Either takes one step by its full name,
`repository/component/phase/step`, and plans or verifies that step alone. Each writes only its
own record in `.dp-instance/runs/`.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import groupby

from rich.text import Text

from . import targets
from .components import StepReports
from .manifest import Manifest
from .outcome import WAITS, Next, Result, Stop, failed
from .record import Record, runs_folder
from .screen import Block
from .sequence import Entry, parse_step
from .session import Session
from .walk import Run

# How many phases `verify` reads at once.
PHASES_AT_ONCE = 6


def _record(session: Session, kind: str, goal: str, manifest: Manifest) -> Record:
    found = Record.new(runs_folder(session.instance.state), kind, None, goal, manifest_origin=manifest.origin)
    found.manifest_path.write_text(manifest.text, encoding="utf-8")
    return found


class View(Run):
    """A read-only walk over the sequence, or one step of it."""

    def single(self, name: str) -> Entry:
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
            entry = self.plan(self.declarations(only=found)).step(name)
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


class Preview(View):
    """`plan`: the run's steps planned in order, read only, up to the first change."""

    def walk(self, step: str | None = None) -> Result:
        self.header()
        try:
            return self._one(step) if step else self._walk()
        except KeyboardInterrupt:
            return self.stopped(Stop("failed", "Interrupted.", reason="interrupted"))
        except Stop as stop:
            return self.stopped(stop)

    def _walk(self) -> Result:
        self.show_part("before")
        self.build()
        entries = self.record.entries
        for index, entry in enumerate(entries, 1):
            if entry.kind != "preflight":
                break
            self._position = index
            self.enter(entry)
            self.count(entry)
            block = self.block(entry)
            self._entry, self._block = entry, block
            self.preflight(entry, block)
        self.close_part("before")
        self._entry = self._block = None
        groups = [
            (key, list(group))
            for key, group in groupby(
                [entry for entry in entries if entry.kind == "step"],
                key=lambda entry: (entry.repository, entry.component, entry.command, entry.phase),
            )
        ]
        sections: list[dict] = []
        for (repository, component, command, phase), members in groups:
            plan_file = self.record.file(
                "plans", f"{entries.index(members[0]) + 1:03d}-{component}-{phase}.txt"
            )
            change = self.planned(members, plan_file, sections, self.cli(repository, command))
            if change is not None:
                self.later(entries[entries.index(change) + 1 :])
                return self.done(change, sections)
        return self.done(None, sections)

    def _one(self, name: str) -> Result:
        self.show_part("platform")
        entry = self.single(name)
        plan_file = self.record.file("plans", f"001-{entry.component}-{entry.step}.txt")
        sections: list[dict] = []
        change = self.planned(
            [entry], plan_file, sections, self.cli(entry.repository, entry.command), step=True
        )
        return self.done(change, sections, single=entry)

    def planned(
        self, members: list[Entry], plan_file, sections: list[dict], cli, *, step: bool = False
    ) -> Entry | None:
        """Plan one phase's steps, or one step, in one process, and show each with its own time;
        the first with a change, if any."""
        first = members[0]
        block = Block("phase", "", first.phase, "", self.footer)
        block.message = (
            f"Planning {first.component}'s {first.phase} {'step ' + first.step if step else 'phase'}"
        )
        self.count(first)
        reports = StepReports()
        arguments = ["plan", "--phase", first.phase, *(["--step", first.step] if step else [])]
        call = self.screen.run_live(
            block,
            cli.run,
            *arguments,
            "--json",
            "--plan-file",
            str(plan_file),
            *self.version,
            on_event=reports,
        )
        if call.status != "done":
            self.enter(first)
            self._pulls = {}
            failing = self.block(first)
            self._entry, self._block = first, failing
            if call.status in WAITS:
                self.wait(first, failing, call)
            self.failure(first, failing, call)
        events = {event["step"]: event for event in call.finished() if event.get("phase") == first.phase}
        for entry in members:
            self._position = self.record.entries.index(entry) + 1
            event = events.get(entry.step)
            self.enter(entry)
            block = self.block(entry)
            took = reports.seconds.get((entry.phase, entry.step))
            pulls = self.reported(entry, block, reports, event)
            if event is None:
                self.finish(entry, block, "skipped", compact=True, took=took)
                continue
            for item in event.get("handoffs") or []:
                sections.append(item)
            self.optional(event)
            status = event.get("status")
            if status == "change":
                block.add("Step Plan:", event.get("summary") or "a change")
                if plan_file.exists() and plan_file.stat().st_size:
                    block.add("Plan File:", self.relative(plan_file))
                self.finish(
                    entry, block, "change", took=took, summary=event.get("summary"), pull_requests=pulls
                )
                return entry
            if status == "pending":
                for item in event.get("handoffs") or []:
                    if item.get("required"):
                        block.add("Step Handoff:", item.get("title", item.get("id")))
                self.finish(entry, block, "pending", took=took, pull_requests=pulls)
                continue
            self.finish(entry, block, "no change", compact=True, took=took, pull_requests=pulls)
        return None

    def later(self, rest) -> None:
        for entry in rest:
            self.enter(entry)
            block = self.block(entry)
            block.finish("planned later")
            block.seconds = None
            self.screen.show(block, compact=True)

    def done(self, change, sections: list[dict], *, single: Entry | None = None) -> Result:
        """The preview's record and result; its platform part ends with the steps it planned."""
        self.close_part("platform")
        pages = self.pages([item for item in sections if item.get("required")]) if sections else {}
        entries = self.record.entries
        summary = {
            "change": change.id if change is not None else None,
            "steps": [
                {"entry": entry.id, **(self.record.outcome(entry.id) or {"outcome": "planned later"})}
                for entry in entries
            ],
            "handoffs": sections,
            "pages": [self.relative(path) for path in pages.values()],
            "merges": [entry.id for entry in entries if entry.phase == "merge"],
        }
        self.record.file("plan.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        lines = [f"# {self.record.data['goal']}", "", f"Instance `{self.instance.key}`.", ""]
        for entry in entries:
            noted = self.record.outcome(entry.id) or {"outcome": "planned later"}
            line = f"- {entry.where}: {noted['outcome']}"
            if noted.get("summary"):
                line += f" ({noted['summary']})"
            lines.append(line)
        if sections:
            lines += ["", "## Handoffs", ""]
            for item in sections:
                need = "required" if item.get("required") else "optional"
                lines.append(f"- {item.get('title')} ({item.get('system')}, {need})")
        self.record.file("plan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.record.complete()
        if change is None:
            self.screen.blank(2)
            if single is not None:
                message = f"No change at {single.where}."
                self.screen.print(Text(message, style="bold green"))
            else:
                message = "No changes."
                self.screen.print(
                    Text.assemble(
                        ("No changes.", "bold green"), " The platform matches the code and configuration."
                    )
                )
            return Result("plan", "done", message, detail={"run": self.record.id, "change": None})
        steps = (
            Next(
                "dp-instance apply",
                "once the change is merged to main, starts a run, which reviews each step with a change "
                "before applying it",
            ),
        )
        self.screen.next_block(steps, self.relative(self.record.folder) + "/")
        return Result(
            "plan",
            "done",
            f"The first change is at {change.where}." if single is None else f"{change.where} has a change.",
            steps,
            {"run": self.record.id, "change": change.id},
        )


def plan(session: Session, step: str | None = None) -> Result:
    manifest = targets.running(session.instance, session.releases)
    manifest.check_install(session.own_version())
    what = f"the step {step}" if step else "the checked-out code"
    goal = f"plan: a preview of Platform {manifest.platform_version}, {what}; starts no run"
    return Preview(session, _record(session, "plan", goal, manifest), manifest).walk(step)


class Verification(View):
    """`verify`: every step's read-only verify, or one step's, by phase, six phases at a time."""

    def walk(self, step: str | None = None) -> Result:
        self.header()
        started = time.monotonic()
        self.show_part("platform")
        try:
            if step:
                entries = [self.single(step)]
            else:
                self.build()
                entries = [entry for entry in self.record.entries if entry.kind == "step"]
        except Stop as stop:
            return Result("verify", stop.status, stop.message, stop.next, {"run": self.record.id})
        results = self.verify_phases(entries, step is not None)
        problems = []
        failed_phases: set[tuple[str, str, str, str]] = set()
        for entry in entries:
            call, reports = results[self.phase_of(entry)]
            found = self.verified(entry, call, reports, failed_phases)
            if found is not None:
                problems.append(found)
        self.record.complete()
        self.close_part("platform", time.monotonic() - started)
        self.screen.blank(2)
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
        again = f"dp-instance verify --step {step}" if step else "dp-instance verify"
        steps = (
            Next(
                "dp-instance apply",
                "applies what a step has left to change; outside work a step names, such as a pull "
                "request to merge and release, is done where it says, and no run does it",
            ),
            Next(again, "checks again once each is done"),
        )
        self.screen.next_block(steps, self.relative(self.record.folder) + "/")
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

        say()
        with ThreadPoolExecutor(max_workers=PHASES_AT_ONCE) as pool:
            return dict(self.screen.run_live(block, lambda: list(pool.map(verify, phases))))

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
    manifest = targets.running(session.instance, session.releases)
    manifest.check_install(session.own_version())
    what = f"the step {step}" if step else "every step"
    goal = f"verify: {what} of Platform {manifest.platform_version}, read only"
    return Verification(session, _record(session, "verify", goal, manifest), manifest).walk(step)
