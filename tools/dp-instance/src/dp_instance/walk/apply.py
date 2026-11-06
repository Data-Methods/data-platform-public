"""`apply`: the platform brought to `main`, as merged on GitHub, from each repository's own copy
at `origin/main`: your checkout is only an input, and `apply` commits to no repository.

Before anything changes, every component's preflight and the drift check, against the commit
each component's walk last applied. Then every platform step, in the manifest's order, each
planned again: a change the reviewed preview recorded applies as approved by review, any other
asks (rule 5), and a step that writes the repository finds that `prepare` already did. A step
whose rendered files would change at apply, as a provisional one's can, stops before it runs,
naming them: `prepare` renders them for review. A component's first applied step records its
walk as part way in the shared run record, with the commit it applies; when its last step has
finished, `outputs mark-applied` records the commit as the one its whole walk applied, and the
walk is no longer part way. A stopped apply finishes the commits it planned; `status` names
`apply` again when `main` moved since. A completed apply names each Managed Repository's
Adoption PR a step left open, whose merge and release come first, then `dp-instance verify`."""

from __future__ import annotations

import time

from .. import gitops, targets
from ..outcome import WAITS, Next, Result, Stop
from ..record import now
from ..screen import Block
from ..sequence import Entry
from .common import DRIFT_TEXT, VERIFY, adoption_left, adoption_prs
from .graph import reads
from .holds import HELD, HOLDING
from .reading import refuse_earlier_release, walks
from .run import Run

MARK_TEXT = "Records the commit this apply brought the component to as the one its whole walk applied."


class Apply(Run):
    def go(self) -> None:
        if not self.record.entries:
            self.show_part("before")
            self.build()
        self.walk_entries()

    def build(self) -> None:
        """The entries of a new apply, from the steps `main` declares: the preflights, the drift
        check, every platform step and, after each component's last, its applied commit."""
        self._progress = None
        started = time.monotonic()
        declarations = self.declarations()
        if not walks(declarations):
            raise refuse_earlier_release("The templates on main")
        plan = self.plan(declarations)
        entries = plan.preflights()
        entries.append(Entry(id="drift-check", kind="drift", description=DRIFT_TEXT))
        platform = plan.platform()
        last = {(entry.repository, entry.component): index for index, entry in enumerate(platform)}
        for index, entry in enumerate(platform):
            entries.append(entry)
            key = (entry.repository, entry.component)
            if last[key] == index:
                entries.append(
                    Entry(
                        id=f"{entry.repository}/{entry.component}/applied-commit",
                        kind="mark",
                        description=MARK_TEXT,
                        repository=entry.repository,
                        component=entry.component,
                        command=entry.command,
                        owner=entry.owner,
                        repository_counter=entry.repository_counter,
                        component_counter=entry.component_counter,
                        repository_description=entry.repository_description,
                        component_description=entry.component_description,
                        affects_no_root=True,
                    )
                )
        self.record.entries = entries
        self.record.data["reads"] = reads(platform, declarations)
        self.seed_handoffs()
        self.timed("read", f"the steps of {len(declarations)} components", started)

    def seed_handoffs(self) -> None:
        """Start the run's handoff documents from the last completed apply's, so a step this run
        doesn't evaluate keeps its standing actions; a step that applies writes its own over them."""
        folder = self.record.folder / "handoffs"
        folder.mkdir(exist_ok=True)
        found = self.session.shared.handoffs(self.record.id) if self.session.shared is not None else {}
        for name, body in found.items():
            (folder / name).write_bytes(body)

    def handlers(self) -> dict:
        return {**super().handlers(), "mark": self.mark, "step": self.guarded_step}

    def guarded_step(self, entry: Entry, block: Block) -> None:
        """A hold stands by in its turn; a step it guards is planned in its turn and, at a change,
        holds first. A hold that stopped applies again before the step it was applied for."""
        if entry.holds_until:
            self.stand_by(entry, block)
            return
        hold = self.guarding(entry)
        state = self.holds().get(hold.id, {}).get("state") if hold is not None else None
        if state == HOLDING:
            self.apply_hold(hold, entry, block, self.held_approvals(entry))
            state = HELD
        self.rendered_step(entry, block, hold if hold is not None and state != HELD else None)

    def rendered_step(self, entry: Entry, block: Block, hold: Entry | None = None) -> None:
        """A step whose rendered files would change at apply stops before it runs: an apply never
        writes a committed file. Any other step plans and applies."""
        if entry.renders:
            self.check_rendered(entry, block)
        self.step(entry, block, hold=hold)

    def check_rendered(self, entry: Entry, block: Block) -> None:
        """Render the step's files in the copy, read only, then put the copy back as it was; a file
        that would change stops the run before the step, naming it."""
        copy = self.checkout(entry.repository)
        call = self.screen.run_live(
            block,
            self.cli_for(entry).run,
            "render",
            "--phase",
            entry.phase,
            "--step",
            entry.step,
            "--json",
            *self.version,
        )
        written = [str(name) for event in call.finished() for name in event.get("files_written") or []]
        files = sorted({*gitops.uncommitted(copy, untracked=True), *written})
        gitops.git("reset", "--quiet", "--hard", cwd=copy)
        gitops.git("clean", "--quiet", "-fd", cwd=copy)
        if call.status == "done" and not files:
            return
        decision = (call.result or {}).get("decision") or {}
        if call.status == "decision" and not files and decision.get("summary"):
            # A render that asks first, as to restore a file edited on main, names what it would change.
            files = [str(decision["summary"])]
        if call.status not in ("done", "decision"):
            if call.status in WAITS:
                self.wait(entry, block, call)
            self.failure(entry, block, call)
        for name in files:
            block.add("Would Change:", name)
        raise Stop(
            "failed",
            f"{entry.where} would change files main holds: {', '.join(files) or 'its rendered files'}. "
            "An apply never writes a committed file, so nothing was applied at this step: prepare "
            "renders them for review.",
            reason="rendered-changes",
            files=files,
            next=(
                Next(
                    "dp-instance prepare", "renders them in a pull request; merge it, then dp-instance apply"
                ),
            ),
        )

    def walk_started(self, entry: Entry) -> None:
        """Record the component's walk as part way, with the commit this apply brings it to, at
        its first applied step."""
        shared = self.session.shared
        started = self.record.data.setdefault("walks_started", [])
        if shared is None or entry.kind != "step" or entry.component in started:
            return
        walks = shared.unfinished_walks()
        if entry.component not in walks:
            walks[entry.component] = {
                "commit": (self.record.data.get("main") or {}).get(entry.repository),
                "run": self.record.id,
                "step": entry.id,
                "at": now(),
            }
            shared.record_unfinished_walks(walks)
        started.append(entry.component)
        self.record.save()

    def entry_done(self, entry: Entry) -> None:
        """Once a component's last entry has finished, its walk is complete: no longer part way."""
        shared = self.session.shared
        if shared is None or not entry.component or entry.kind not in ("step", "mark"):
            return
        last = [
            item
            for item in self.record.entries
            if item.component == entry.component and item.kind in ("step", "mark")
        ]
        if not last or last[-1].id != entry.id:
            return
        walks = shared.unfinished_walks()
        if walks.pop(entry.component, None) is not None:
            shared.record_unfinished_walks(walks)

    def mark(self, entry: Entry, block: Block) -> None:
        """Record the commit this apply brought the component to as its applied commit."""
        cli = self.cli(entry.repository, entry.command)
        call = self.screen.run_live(block, cli.run, "outputs", "mark-applied", "--json")
        if call.status != "done":
            if call.status in WAITS:
                self.wait(entry, block, call)
            self.failure(entry, block, call)
        events = call.finished("mark-applied")
        commit = events[-1].get("commit") if events else None
        applied = bool(events) and events[-1].get("status") == "applied"
        if commit:
            block.add("Applied Commit:", commit)
        self.finish(entry, block, "applied" if applied else "no change", compact=not applied, commit=commit)

    def end(self) -> Result:
        adoption = adoption_prs(self.instance, self.record)
        self.record.data["adoption_prs"] = adoption
        applied = self.record.data.get("applies")
        if applied and self.session.shared is not None:
            found = self.session.shared.change()
            if found is not None and found.get("run") == applied:
                self.session.shared.record_change(None)
        # So the shared run record holds each page too.
        if self.record.data.get("optional"):
            self.pages([])
        self.record.complete()
        took = self.run_time()
        self.screen.complete(
            took["total"],
            took["waiting"],
            "Every changed step passed its verify, and every other step planned no change.",
        )
        recommended = self.recommend()
        message = "Complete."
        if adoption:
            self.show_adoption(adoption)
            message = (
                "Complete; "
                + (
                    f"Adoption PR {adoption[0]['url']} is left open: {adoption_left(adoption[0])} it"
                    if len(adoption) == 1
                    else "Adoption PRs are left open: "
                    + "; ".join(f"{adoption_left(item)} {item['url']}" for item in adoption)
                )
                + ", then run dp-instance verify."
            )
            left: tuple[Next, ...] = (VERIFY,)
        else:
            left = self.left()
        if adoption:
            self.screen.then(
                "Merge and release each Adoption PR above, then run dp-instance verify.",
                self.relative(self.record.folder) + "/",
            )
        else:
            self.screen.run_record(self.relative(self.record.folder) + "/")
        return Result(
            "apply",
            "done",
            message,
            left,
            {"run": self.record.id, "handoffs": recommended, "adoption_prs": adoption, "run_time": took},
        )

    def left(self) -> tuple[Next, ...]:
        """What's left once an apply of a published release completes: the next stop when one is
        published; otherwise it says nothing is."""
        version = self.manifest.platform_version
        if version not in self.session.releases.published():
            return ()
        try:
            upcoming = targets.next_release(self.session.releases, version)
        except Stop:
            return ()
        if upcoming is None:
            self.screen.blank()
            self.screen.note(f"Nothing is left to upgrade: Platform {version} is the newest release.")
            return ()
        return (
            Next(
                f"dp-instance prepare --to {upcoming}",
                f"prepares Platform {upcoming}, {targets.describe_next(version, upcoming)}",
            ),
        )
