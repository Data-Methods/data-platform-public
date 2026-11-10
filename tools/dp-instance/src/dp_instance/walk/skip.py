"""The walk's plan skip, rule C: while it holds, what an apply's step may skip planning."""

from __future__ import annotations

from ..record import FINISHED
from ..screen import Block
from ..sequence import Entry
from .base import Base
from .graph import downstream


class Skip(Base):
    def skipping(self) -> bool:
        """Whether rule C holds: in an apply, since its drift check, nothing has applied a real
        change but steps declared to write only their repositories or to change nothing a
        Terraform root reads, and the run hasn't waited on outside work or resumed after any stop but a
        decision, nor found a decision's step with nothing left to apply on resuming. A native
        step's real change also ends native steps' reuse (`end_reuse`)."""
        data = self.record.data
        return "drift_plans" in data and not data.get("plan_skip_off")

    def end_plan_skip(self, block: Block | None, reason: str) -> None:
        """Rule C no longer holds, for the rest of the run, which the record keeps with why."""
        if not self.skipping():
            return
        self.record.data["plan_skip_off"] = reason
        self.record.save()
        line = f"off from here: {reason}; every later step plans as usual"
        if block is not None:
            block.add("Plan Skip:", line)
        else:
            self.screen.labelled("Plan Skip:", line)

    def end_reuse(self, block: Block, reason: str) -> None:
        """No later native step reuses its drift check, for the rest of the run, which the record
        keeps with why, as a resume does; Terraform steps keep rule C's own rule."""
        if self.record.data.get("reuse_off"):
            return
        self.record.data["reuse_off"] = reason
        self.record.save()
        if self.skipping():
            block.add(
                "Plan Skip:", f"off for native steps from here: {reason}; Terraform steps skip as before"
            )

    def applied_change(self, entry: Entry, block: Block, *, real: bool = True) -> None:
        """A real change ends rule C, unless the step writes only its repositories or changes
        nothing a Terraform root reads, as its declaration says: a Terraform step's for the steps
        that read from it, by the graph of declared inputs; any other step's, or one in a run
        without that graph, for every later step. A native step's real change, a hold's included,
        also ends every later native step's reuse, whatever its declaration says; recording a
        component's applied commit is no step's. Output changes, forgets, moves and imports aren't
        real changes."""
        if not real:
            return
        if not entry.affects_no_root:
            self.reach_readers(entry, block)
        if entry.kind == "step" and not entry.terraform:
            self.end_reuse(block, f"{entry.where} applied a change")

    def reach_readers(self, entry: Entry, block: Block) -> None:
        """A Terraform step's real change reaches only the steps that read from it; any other's,
        or one in a run without that graph, every later step."""
        graph = self.record.data.get("reads")
        if not entry.terraform or graph is None:
            self.end_plan_skip(block, f"{entry.where} applied a change")
            return
        reached = downstream(graph, entry.id) - set(self.record.data.get("reached") or ())
        if not reached or not self.skipping():
            return
        self.record.data["reached"] = sorted({*(self.record.data.get("reached") or ()), *reached})
        self.record.save()
        block.add(
            "Plan Skip:",
            f"off for the {len(reached)} later step{'s' if len(reached) != 1 else ''} that read from "
            f"{entry.where}; every other step skips as before",
        )

    def decision_resumed(self, entry: Entry, block: Block, outcome: str) -> None:
        """A decision's resume carries on at the step the run stopped at. Should it finish
        without applying, its plan changed while the run stood still, as when a person applied
        it alone, so rule C ends, whether or not the step is declared."""
        if entry.id != self.record.data.get("decision_resumed") or outcome not in FINISHED:
            return
        del self.record.data["decision_resumed"]
        if outcome != "applied":
            self.end_plan_skip(block, f"{entry.where}'s plan changed while the run stood still")

    def kept_hashes(self) -> dict[str, str]:
        """Each step's inputs hash as an apply last left it."""
        if self._hashes is None:
            shared = self.session.shared
            self._hashes = shared.step_hashes() if shared is not None else {}
        return self._hashes

    def keep_hash(self, entry: Entry, event: dict) -> None:
        """Keep the hash an apply left a step that skips across runs at, which a later `prepare`'s
        preview compares; a step that only reuses its drift check within a run needs none kept."""
        digest = event.get("inputs_hash")
        shared = self.session.shared
        if shared is None or not entry.plan_skip or not digest or self.kept_hashes().get(entry.id) == digest:
            return
        self.kept_hashes()[entry.id] = digest
        shared.record_step_hashes(self.kept_hashes())

    def skip_ahead(self, entry: Entry, block: Block) -> None:
        """Ask once, in one process, for the steps of this phase that skip from here on, so a run
        of skips doesn't start a process for each. Whatever it doesn't report, each step finds
        out in its own turn."""
        if entry.id in self._skipped:
            return
        entries = self.record.entries
        group: list[Entry] = []
        for item in entries[entries.index(entry) :]:
            same = (item.repository, item.component, item.phase) == (
                entry.repository,
                entry.component,
                entry.phase,
            )
            if not same or item.kind != "step" or self.record.finished(item.id) or not self.planned(item):
                break
            group.append(item)
        if len(group) < 2:
            return
        plan_file = self.record.file("plans", f"{self._position:03d}-{entry.component}-skips.txt")
        planned = [
            part for item in group for pair in self.planned(item) for part in ("--planned", "=".join(pair))
        ]
        call = self.screen.run_live(
            block,
            self.cli_for(entry).run,
            "apply",
            "--phase",
            entry.phase,
            "--json",
            "--plan-file",
            str(plan_file),
            *self.version,
            *self.handoff_folder(),
            *planned,
            "--unchanged-since-planned",
            "--skips-only",
        )
        by_step = {item.step: item for item in group}
        for event in call.finished():
            item = by_step.get(event.get("step"))
            if item is not None and event.get("skip_rule"):
                self._skipped[item.id] = event

    def reuses(self, entry: Entry) -> bool:
        """Whether the step reuses its drift check within a run: one that skips across runs, or one
        the run's declarations say reuses, as its record keeps them."""
        return entry.plan_skip or entry.id in (self.record.data.get("reuses") or ())

    def planned(self, entry: Entry) -> list[tuple[str, str]]:
        """What a step's apply may skip planning, from the drift check: the step itself, when it
        reuses its drift check, nothing applied earlier in the run reaches it, and its drift plan
        had no change and named its inputs hash. Nothing while rule C doesn't hold, nor, for a
        native step, once a native step has applied a real change."""
        data = self.record.data
        if not self.skipping() or not self.reuses(entry) or entry.id in (data.get("reached") or ()):
            return []
        if not entry.terraform and data.get("reuse_off"):
            return []
        planned = data["drift_plans"].get(entry.id) or {}
        if planned.get("status") != "no-change" or not planned.get("inputs_hash"):
            return []
        return [(entry.step, planned["inputs_hash"])]
