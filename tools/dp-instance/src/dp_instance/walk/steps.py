"""A step: its plan, the decision it stops for, its apply and verify, and the pull requests it
reports. A prepare's code steps commit on the change's branch; an apply's steps run on `main`.

Rule 5: an apply's step with a change asks unless its decision is one the reviewed preview
recorded, by its digest, and `--auto-approve` would cover, which applies as approved by review;
destroys and replacements ask even then, by a Terraform decision's counts, and so does a native
decision that doesn't say it destroys nothing. A change merged outside `prepare` has no preview,
so it asks, as does every step of a component the drift check left out. A preview holds
decisions where its component's plan reports them (`plan_decisions`); otherwise every change
asks, showing the reviewed summary beside its own."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text

from ..components import Call
from ..outcome import WAITS, Next, Stop, failed
from ..screen import Block, link
from ..sequence import Entry
from .base import Base
from .checks import real_change
from .common import DECISION_LABELS, PLAN_HEAD
from .holds import HELD


class Steps(Base):
    def step(self, entry: Entry, block: Block, hold: Entry | None = None) -> None:
        """Plan and apply one step. A step a ``hold`` guards, while the hold stands by, is planned
        with no approval: at a change, it asks once for the step and the hold, then applies the
        hold, then the step. The step that lifts a hold the run applied applies with its approval."""
        self.skip_ahead(entry, block)
        event = self._skipped.pop(entry.id, None)
        if event is not None:
            self.no_change(entry, block, event)
            return
        cli = self.cli_for(entry)
        approvals = [*self.options.approvals, *self.held_approvals(entry)]
        lifting = self.lifting(entry)
        supplied: dict | None = None
        plan_file = self.record.file("plans", f"{self._position:03d}-{entry.component}-{entry.step}.txt")
        while True:
            # A step's plan is each attempt's own.
            plan_file.unlink(missing_ok=True)
            standing = hold is not None and self.holds().get(hold.id, {}).get("state") != HELD
            # A component the drift check left out asks at each change, `--auto-approve` or not.
            arguments = self.arguments(
                entry,
                plan_file,
                [] if standing else approvals,
                supplied is not None,
                automatic=not standing and not self.unchecked(entry),
            )
            block.outcome = "applying" if len(approvals) > len(self.options.approvals) else "planning"
            call = self.screen.run_live(
                block,
                cli.run,
                *arguments,
                stdin=supplied,
                on_line=self.messages(block),
                on_event=self.pull_request_events(block),
            )
            block.message = ""
            if call.status == "done":
                self.applied(entry, block, call, plan_file, approvals)
                return
            if call.status == "decision" and standing:
                self.approve_with_hold(entry, block, call, approvals, plan_file, hold)
                self.apply_hold(hold, entry, block, approvals)
                continue
            if call.status == "decision" and lifting is not None and self.lift(entry, call, approvals):
                continue
            if call.status == "decision":
                self.decide(entry, block, call, approvals, plan_file)
                continue
            if call.status == "handoff":
                self.handoff(entry, block, call, cli)
                continue
            if call.status == "values":
                supply = ((call.result or {}).get("values") or {}).get("supply")
                if supply and self.session.interactive and supplied is None:
                    supplied = self.supply(entry, block, call)
                    continue
            if call.status in WAITS:
                self.wait(entry, block, call)
            self.failure(entry, block, call)

    # Pieces of a step -----------------------------------------------------------------

    def approval_arguments(self, approvals: list[str], *, auto: bool = False) -> list[str]:
        found = [part for identifier in dict.fromkeys(approvals) for part in ("--approve", identifier)]
        return found + (["--auto-approve"] if auto or self.options.auto_approve else [])

    def arguments(
        self, entry: Entry, plan_file: Path, approvals: list[str], supplied: bool, *, automatic: bool = True
    ) -> list[str]:
        """A step's command: ``automatic`` passes `--auto-approve` when the run was given it."""
        if entry.kind in ("upgrade", "migrate"):
            # A code step changes only the change's branch, which its pull request shows, so
            # --auto-approve, which leaves restoring an edited template file or removing a file
            # to a decision.
            return [
                *self.code_command(entry),
                "--json",
                "--plan-file",
                str(plan_file),
                *self.version,
                "--change",
                self.change,
                *self.approval_arguments(approvals, auto=True),
            ]
        arguments = [
            "apply",
            "--phase",
            entry.phase,
            "--step",
            entry.step,
            "--json",
            "--plan-file",
            str(plan_file),
            *self.version,
        ]
        if supplied:
            arguments.append("--values-from-stdin")
        # The run's handoff documents, in its folder of the shared run record.
        folder = self.record.folder / "handoffs"
        folder.mkdir(exist_ok=True)
        arguments += ["--handoffs", str(folder)]
        terraform = self.planned(entry)
        # Rule C alone: never --planned without --unchanged-since-planned.
        arguments += [part for step, value in terraform for part in ("--planned", f"{step}={value}")]
        if terraform:
            arguments.append("--unchanged-since-planned")
        if not automatic:
            return arguments + [
                part for identifier in dict.fromkeys(approvals) for part in ("--approve", identifier)
            ]
        return arguments + self.approval_arguments(approvals)

    def reviewed(self, entry: Entry) -> dict | None:
        """The step's reviewed preview, from the change an apply applies; None for another run."""
        return (self.record.data.get("reviewed") or {}).get(entry.id)

    def unchecked(self, entry: Entry) -> bool:
        """Whether the drift check left the step's component out, so each of its changes asks."""
        return entry.kind == "step" and entry.component in (
            self.record.data.get("unchecked_components") or []
        )

    def approved_by_review(self, entry: Entry, decision: dict) -> bool:
        """Whether the decision is one the reviewed preview recorded, by its full digest, that
        `--auto-approve` would cover and that destroys nothing: a Terraform decision whose counts
        hold no destroy or replacement, a forget never asking, or a native decision that says it
        destroys nothing. Any other asks even when it matches, as does one with no digest, from an
        engine before it, and every step of a component the drift check couldn't check."""
        if self.unchecked(entry):
            return False
        digest = decision.get("digest")
        reviewed = {item.get("digest") for item in (self.reviewed(entry) or {}).get("decisions") or []}
        if not digest or digest not in reviewed or decision.get("auto") is not True:
            return False
        counts = decision.get("counts")
        if counts is None:
            return decision.get("destroys") is False
        return not (counts.get("destroy") or counts.get("replace"))

    def approve_with_hold(
        self, entry: Entry, block: Block, call: Call, approvals: list[str], plan_file: Path, hold: Entry
    ) -> None:
        """A guarded step's change, approved once for the step and the hold: by `--approve`, by
        `--auto-approve` when its decision is one it covers, or by the reviewed preview; or else
        asked."""
        decision = (call.result or {}).get("decision") or {}
        identifier = decision.get("id")
        automatic = (
            self.options.auto_approve and decision.get("auto") is not False and not self.unchecked(entry)
        )
        reviewed = self.approved_by_review(entry, decision)
        if identifier and (identifier in approvals or automatic or reviewed):
            seen = self.record.data.setdefault("decisions", {}).setdefault(entry.id, [])
            if identifier not in seen:
                seen.append(identifier)
            self.keep_counts(decision)
            if identifier not in approvals:
                self.record.data.setdefault("by_review" if reviewed else "by_auto", []).append(identifier)
            approvals.append(identifier)
            self.record.save()
            return
        self.decide(entry, block, call, approvals, plan_file, hold=hold)

    def lift(self, entry: Entry, call: Call, approvals: list[str]) -> bool:
        """The change of a step that lifts a hold this run applied, approved with the hold when its
        decision says it destroys nothing; whether it was."""
        decision = (call.result or {}).get("decision") or {}
        identifier = decision.get("id")
        if decision.get("destroys") is not False:
            return False
        if not identifier:
            raise failed(f"{entry.command} stopped for a decision it didn't name.")
        if identifier in approvals:
            raise failed(f"{entry.command} asked again for decision {identifier}, which this run approved.")
        seen = self.record.data.setdefault("decisions", {}).setdefault(entry.id, [])
        if identifier not in seen:
            seen.append(identifier)
        self.keep_counts(decision)
        self.record.data.setdefault("with_hold", []).append(identifier)
        self.record.save()
        approvals.append(identifier)
        return True

    def keep_counts(self, decision: dict) -> None:
        """Keep a decision's counts, which say whether applying it is a real change; a native
        decision has none."""
        self.record.data.setdefault("decision_counts", {})[decision["id"]] = decision.get("counts")

    def real(self, entry: Entry) -> bool:
        """Whether the step's applied decisions were a real change: a create, an update in place, a
        replacement or a destroy, or a decision without counts. A step applied without a decision
        this run saw counts as real."""
        counts = self.record.data.get("decision_counts") or {}
        decisions = self.record.data.get("decisions", {}).get(entry.id, [])
        if not decisions:
            return True
        return real_change(
            {
                "status": "change",
                "decisions": [{"counts": counts.get(identifier)} for identifier in decisions],
            }
        )

    def decide(
        self,
        entry: Entry,
        block: Block,
        call: Call,
        approvals: list[str],
        plan_file: Path | None = None,
        hold: Entry | None = None,
    ) -> None:
        decision = (call.result or {}).get("decision") or {}
        identifier = decision.get("id")
        if not identifier:
            raise failed(f"{entry.command} stopped for a decision it didn't name.")
        if identifier in approvals:
            raise failed(f"{entry.command} asked again for decision {identifier}, which this run approved.")
        seen = self.record.data.setdefault("decisions", {}).setdefault(entry.id, [])
        if identifier not in seen:
            seen.append(identifier)
        self.keep_counts(decision)
        self.record.save()
        if hold is None and self.approved_by_review(entry, decision):
            approvals.append(identifier)
            self.record.data.setdefault("by_review", []).append(identifier)
            self.record.save()
            return
        block.outcome = "change"
        block.lines = [line for line in block.lines if line.label not in DECISION_LABELS]
        block.add("Step Plan:", decision.get("summary") or "a change")
        reviewed = self.reviewed(entry)
        if reviewed is not None:
            said = reviewed.get("summary") or reviewed.get("outcome") or "no change"
            block.add("Reviewed:", said + (" (provisional)" if reviewed.get("provisional") else ""))
        elif self.record.kind == "apply":
            block.add("Reviewed:", "no preview: the change merged outside dp-instance prepare")
        shown_plan = plan_file is not None and plan_file.exists() and plan_file.stat().st_size > 0
        if shown_plan:
            block.add("Plan File:", self.relative(plan_file))
        block.add("Decision:", identifier)
        if hold is not None:
            block.add(
                "Hold:",
                f"approving it first applies {hold.component}'s {hold.step}: {self.hold_note(hold)}",
            )
        notes = self.notes_line()
        if notes:
            block.add("Release Notes:", notes)
        if self.session.interactive:
            asked = (
                f"{self.hold_note(hold)}. Apply {hold.step}, then this step?"
                if hold is not None
                else "Apply this step?"
            )
            key = self.screen.ask(block, [("y", "apply"), ("n", "stop the run here")], asked)
            if key == "y":
                approvals.append(identifier)
                block.lines = [line for line in block.lines if line.label != "Release Notes:"]
                block.outcome = "applying"
                return
            raise Stop(
                "decision",
                f"{entry.where}: not approved; nothing was applied.",
                reason="declined",
                decision={**decision, "entry": entry.id},
                next=(
                    Next("dp-instance resume", f"shows {entry.where}'s plan for review again"),
                    *self.again(),
                ),
                **self.notes_detail(),
            )
        # The steps before this one have finished, so a resume needs only the decisions this
        # entry has asked for.
        command = "dp-instance resume " + " ".join(f"--approve {item}" for item in seen)
        lines = plan_file.read_text(encoding="utf-8", errors="replace").splitlines() if shown_plan else []
        raise Stop(
            "decision",
            f"{entry.where} has a change to review: {decision.get('summary') or 'a change'}.",
            reason="decision",
            decision={
                **decision,
                "entry": entry.id,
                "plan": self.relative(plan_file) if shown_plan else None,
                "plan_head": lines[:PLAN_HEAD],
                "plan_lines": len(lines),
                "resume": command,
            },
            next=(
                Next(
                    command,
                    (f"applies {hold.component}'s {hold.step}, then " if hold is not None else "applies ")
                    + "exactly the plan that was reviewed, and carries on",
                ),
                Next("dp-instance resume", "at a terminal, shows the plan and asks"),
            ),
            **self.notes_detail(),
        )

    def no_change(self, entry: Entry, block: Block, event: dict) -> None:
        """Finish a step that applied nothing, as its plan or a skip found."""
        status = event.get("status")
        outcome = "no change" if status == "no-change" else str(status)
        pulls = self.pull_requests(block, event)
        skipped = event.get("skip_rule")
        if skipped:
            block.add(
                "Plan Skip:",
                f"no change, as the drift check planned it (rule {skipped}); it didn't plan",
            )
        if status == "no-change":
            self.keep_hash(entry, event)
        self.finish(entry, block, outcome, compact=True, pull_requests=pulls, skip_rule=skipped)

    def applied(self, entry: Entry, block: Block, call: Call, plan_file: Path, approvals: list[str]) -> None:
        events = call.finished(entry.step)
        if not events:
            raise failed(f"{entry.command} reported no step {entry.step}; its log has what it ran.")
        event = events[-1]
        self.optional(event)
        status = event.get("status")
        decisions = self.record.data.get("decisions", {}).get(entry.id, [])
        if status != "applied":
            self.no_change(entry, block, event)
            return
        self.keep_hash(entry, event)
        by_review = self.record.data.get("by_review") or []
        with_hold = self.record.data.get("with_hold") or []
        by_auto = self.record.data.get("by_auto") or []
        how = (
            "approved by review"
            if any(identifier in by_review for identifier in decisions)
            else "approved with the hold"
            if any(identifier in with_hold for identifier in decisions)
            else "approved by --auto-approve"
            if any(identifier in by_auto for identifier in decisions)
            else "approved"
            if any(
                identifier in approvals and identifier not in self.options.approvals
                for identifier in decisions
            )
            else "approved by --approve"
            if any(identifier in self.options.approvals for identifier in decisions)
            else "approved by --auto-approve"
            if self.options.auto_approve
            else "applied"
        )
        plan_line = next((line for line in block.lines if line.label == "Step Plan:"), None)
        if plan_line is None:
            block.add("Step Plan:", event.get("summary") or "a change")
            if plan_file.exists() and plan_file.stat().st_size:
                block.add("Plan File:", self.relative(plan_file))
        block.lines = [line for line in block.lines if line.label != "Decision:"]
        plan_line = next(line for line in block.lines if line.label == "Step Plan:")
        plan_line.text.append(f"   {how}", style="green")
        verification = event.get("verification") or {"passed": True}
        if verification.get("passed", True):
            text = Text("passed: nothing left to change, and the step's own checks pass", style="green")
        else:
            text = Text("failed: " + "; ".join(verification.get("failures", [])), style="red")
        block.add("Step Verification:", text)
        pulls = self.pull_requests(block, event)
        self.finish(
            entry,
            block,
            "applied",
            real=self.real(entry),
            summary=event.get("summary"),
            plan=self.relative(plan_file) if plan_file.exists() and plan_file.stat().st_size else None,
            decisions=decisions,
            pull_requests=pulls,
        )
        self.walk_started(entry)

    def pull_request_events(self, block: Block):
        """Show each pull request the step reports as it happens, the moment its event arrives:
        on its live block at a terminal, as a line of its own elsewhere, and as a JSON event."""

        def seen(event: dict) -> None:
            if event.get("event") == "pull_request" and event.get("url"):
                self.show_pull_request(block, event["url"], event.get("state"), at_once=True)

        return seen

    def show_pull_request(self, block: Block, url: str, state: str | None, *, at_once: bool) -> None:
        """Keep a pull request's last state for the step, on one line of its block."""
        if url in self._pulls and self._pulls[url] == state:
            return
        self._pulls[url] = state
        text = Text.assemble(link(url, url), f" ({state})" if state else "")
        line = next(
            (
                line
                for line in block.lines
                if line.label == "Pull Request:" and line.plain.split(" ")[0] == url
            ),
            None,
        )
        if line is None:
            block.add("Pull Request:", text)
        else:
            line.text = text
        if at_once:
            if not self.screen.interactive and (self._entry is None or self._entry.kind != "pull-request"):
                self.screen.labelled("Pull Request:", text)
            self.screen.emit(
                {
                    "event": "pull_request",
                    "run": self.record.id,
                    "entry": self._entry.id if self._entry is not None else None,
                    "url": url,
                    "state": state,
                }
            )

    def pull_requests(self, block: Block, event: dict | None = None) -> list[dict]:
        """The pull requests the step reported, each in its last state: as each happened, then
        as its finished event, when it has one, lists them; the step's outcome in the record
        keeps them. An engine before 0.15 reports none, and one before 0.16 only when the step
        finishes."""
        for item in (event or {}).get("pull_requests") or []:
            if isinstance(item, dict) and item.get("url"):
                self.show_pull_request(block, item["url"], item.get("state"), at_once=False)
        return [{"url": url, "state": state} for url, state in self._pulls.items()]
