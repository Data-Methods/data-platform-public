"""The read-only commands over the whole sequence: `plan` and `verify`.

`plan` is an optional preview of the checked-out code: it starts no run and changes
nothing, walks the steps in order, ends at the first step with a change and lists the steps
after it unplanned. `verify` is the completion check: every step's read-only verify across
the whole sequence. Each writes only its own record in `.dp-instance/runs/`.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from itertools import groupby

from rich.text import Text

from . import targets
from .manifest import Manifest
from .outcome import WAITS, Next, Result, Stop
from .record import Record, runs_folder
from .screen import Block
from .session import Session
from .walk import Run


def _record(session: Session, kind: str, goal: str, manifest: Manifest) -> Record:
    found = Record.new(runs_folder(session.instance.state), kind, None, goal, manifest_origin=manifest.origin)
    found.manifest_path.write_text(manifest.text, encoding="utf-8")
    return found


class Preview(Run):
    """`plan`: the run's steps planned in order, read only, up to the first change."""

    def walk(self) -> Result:
        self.header()
        try:
            return self._walk()
        except KeyboardInterrupt:
            return self.stopped(Stop("failed", "Interrupted.", reason="interrupted"))
        except Stop as stop:
            return self.stopped(stop)

    def _walk(self) -> Result:
        self.build()
        entries = self.record.entries
        for index, entry in enumerate(entries, 1):
            if entry.kind != "preflight":
                break
            self._position = index
            self.enter(entry)
            block = self.block(entry)
            self._entry, self._block = entry, block
            self.preflight(entry, block)
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
            block = Block("phase", "", phase, "", self.footer)
            block.message = f"Planning {component}'s {phase} phase"
            cli = self.cli(repository, command)
            call = self.screen.run_live(
                block,
                cli.run,
                "plan",
                "--phase",
                phase,
                "--json",
                "--plan-file",
                str(plan_file),
                *self.version,
            )
            if call.status != "done":
                entry = members[0]
                self.enter(entry)
                step = self.block(entry)
                self._entry, self._block = entry, step
                if call.status in WAITS:
                    self.wait(entry, step, call)
                self.failure(entry, step, call)
            events = {event["step"]: event for event in call.finished()}
            for entry in members:
                self._position = entries.index(entry) + 1
                event = events.get(entry.step)
                self.enter(entry)
                step = self.block(entry)
                if event is None:
                    self.finish(entry, step, "skipped", compact=True)
                    continue
                for item in event.get("handoffs") or []:
                    sections.append(item)
                self.optional(event)
                status = event.get("status")
                if status == "change":
                    step.add("Step Plan:", event.get("summary") or "a change")
                    if plan_file.exists() and plan_file.stat().st_size:
                        step.add("Plan File:", self.relative(plan_file))
                    self.finish(entry, step, "change", summary=event.get("summary"))
                    self.later(entries[entries.index(entry) + 1 :])
                    return self.done(entry, sections)
                if status == "pending":
                    for item in event.get("handoffs") or []:
                        if item.get("required"):
                            step.add("Step Handoff:", item.get("title", item.get("id")))
                    self.finish(entry, step, "pending")
                    continue
                self.finish(entry, step, "no change", compact=True)
        return self.done(None, sections)

    def later(self, rest) -> None:
        for entry in rest:
            self.enter(entry)
            block = self.block(entry)
            block.finish("planned later")
            block.seconds = None
            self.screen.show(block, compact=True)

    def done(self, change, sections: list[dict]) -> Result:
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
            self.screen.print(
                Text.assemble(
                    ("No changes.", "bold green"), " The platform matches the code and configuration."
                )
            )
            return Result("plan", "done", "No changes.", detail={"run": self.record.id, "change": None})
        steps = (
            Next(
                "dp-instance apply", "starts a run, which reviews each step with a change before applying it"
            ),
        )
        self.screen.next_block(steps, self.relative(self.record.folder) + "/")
        return Result(
            "plan",
            "done",
            f"The first change is at {change.where}.",
            steps,
            {"run": self.record.id, "change": change.id},
        )


def plan(session: Session) -> Result:
    manifest = targets.running(session.instance, session.releases)
    manifest.check_install(session.own_commit())
    goal = f"plan: a preview of Platform {manifest.platform_version}, the checked-out code; starts no run"
    return Preview(session, _record(session, "plan", goal, manifest), manifest).walk()


class Verification(Run):
    """`verify`: every step's read-only verify, each component's in one process, in parallel."""

    def walk(self) -> Result:
        self.header()
        try:
            self.build()
        except Stop as stop:
            return Result("verify", stop.status, stop.message, stop.next, {"run": self.record.id})
        entries = [entry for entry in self.record.entries if entry.kind == "step"]
        components = list(
            dict.fromkeys((entry.repository, entry.component, entry.command) for entry in entries)
        )
        block = Block("verify", "", "", "", self.footer)
        block.message = f"Verifying {len(components)} components in parallel"

        def verify(component):
            repository, key, command = component
            return component, self.cli(repository, command).run("verify", "--json", *self.version)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = dict(self.screen.run_live(block, lambda: list(pool.map(verify, components))))
        converged = True
        problems = []
        reported: set[tuple[str, str]] = set()
        for index, entry in enumerate(entries, 1):
            self._position = index
            call = results[(entry.repository, entry.component, entry.command)]
            self.enter(entry)
            step = self.block(entry)
            events = [event for event in call.finished(entry.step) if event.get("phase") == entry.phase]
            if not events:
                if call.status == "done":
                    self.finish(entry, step, "skipped", compact=True)
                    continue
                key = (entry.repository, entry.component)
                if key not in reported:
                    reported.add(key)
                    step.add("Failure:", call.message)
                    step.add("Log:", self.relative(self.record.log_path))
                converged = False
                problems.append(entry.where)
                self.finish(entry, step, "not verified")
                continue
            event = events[-1]
            self.optional(event)
            status = event.get("status")
            if status == "converged":
                self.finish(entry, step, "converged", compact=True)
                continue
            converged = False
            problems.append(entry.where)
            for failure in (event.get("verification") or {}).get("failures", []):
                step.add("Failure:", failure)
            for item in event.get("handoffs") or []:
                if item.get("required"):
                    step.add("Step Handoff:", item.get("title", item.get("id")))
            self.finish(entry, step, "pending" if status == "pending" else "not converged")
        self.record.complete()
        self.screen.blank(2)
        if converged:
            self.screen.print(
                Text.assemble(
                    ("Converged.", "bold green"),
                    " Nothing is left to change, and every step's own checks pass.",
                )
            )
            return Result("verify", "done", "Converged.", detail={"run": self.record.id})
        self.screen.print(Text.assemble(("Not converged.", "bold red"), f" {len(problems)} steps aren't."))
        steps = (Next("dp-instance apply", "starts a run, which applies what's left"),)
        self.screen.next_block(steps, self.relative(self.record.folder) + "/")
        return Result(
            "verify", "failed", "Not converged.", steps, {"run": self.record.id, "not_converged": problems}
        )


def verify(session: Session) -> Result:
    manifest = targets.running(session.instance, session.releases)
    manifest.check_install(session.own_commit())
    goal = f"verify: every step of Platform {manifest.platform_version}, read only"
    return Verification(session, _record(session, "verify", goal, manifest), manifest).walk()
