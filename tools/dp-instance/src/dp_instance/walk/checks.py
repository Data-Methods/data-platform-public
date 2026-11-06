"""The checks before anything changes: every component's preflight, all at once, and an apply's
drift check, against the commit each component's walk last applied."""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.text import Text

from .. import applied, places, targets
from ..components import Call, Cli
from ..instance import SourceRecord, registrations
from ..outcome import WAITING_FOR, WAITS, Next, Stop
from ..processes import PHASES_AT_ONCE
from ..screen import Block
from ..sequence import Entry, drift_phases
from .base import Base
from .common import UNKNOWN_VERSION


class Checks(Base):
    def preflights(self, entries: list[Entry]) -> None:
        """Every preflight not yet passed, all at once, each in its own process and shown as it
        finishes, with its own time. Once every one has finished, the run stops if any didn't
        pass, naming each together: for a sign-in to renew, or anything else a component waits
        on, when one does."""
        entries = [entry for entry in entries if entry.id not in self._walked]
        if not entries:
            return
        self.enter(entries[0])
        self._walked.update(entry.id for entry in entries)
        self.count(entries[0], entries[-1])
        clis = {entry.id: self.cli_for(entry) for entry in entries}
        blocks = {entry.id: self.block(entry) for entry in entries}
        group = Block("preflights", "", "", "", self.footer)
        group.outcome = "checking"
        finished: dict[str, Call] = {}

        def say() -> None:
            group.message = (
                f"Running {len(entries)} preflight{'s' if len(entries) != 1 else ''} at once: "
                f"{len(finished)} done"
            )

        def run(entry: Entry) -> tuple[Entry, Call]:
            return entry, clis[entry.id].run("preflight", "--json", *self.version)

        def work() -> None:
            with ThreadPoolExecutor(max_workers=len(entries)) as pool:
                for future in as_completed([pool.submit(run, entry) for entry in entries]):
                    entry, call = future.result()
                    finished[entry.id] = call
                    self.preflighted(entry, blocks[entry.id], call)
                    say()

        say()
        self.screen.run_live(group, work)
        failures = [(entry, finished[entry.id]) for entry in entries if finished[entry.id].status != "done"]
        if not failures:
            return
        waiting = next(((entry, call) for entry, call in failures if call.status in WAITS), None)
        # The stop names the preflight it waits on, or the first that failed.
        stopped, call = waiting or failures[0]
        self._entry, self._block = stopped, blocks[stopped.id]
        detail: dict = {"reason": call.status if waiting else "preflight"}
        if call.status == "sign-in":
            detail["sign_in"] = {"entry": stopped.id, "command": self.sign_in_next(call)[0].command}
        raise Stop(
            call.status if waiting else "failed",
            " ".join(self.preflight_failure(entry, call) for entry, call in failures),
            failures=[
                {
                    "entry": entry.id,
                    "component": entry.component,
                    "status": call.status,
                    "message": call.message,
                }
                for entry, call in failures
            ],
            next=(
                *self.sign_in_next(call),
                Next(
                    "dp-instance resume",
                    "runs each preflight that didn't pass and every check after them, then carries on, "
                    "once that's done",
                ),
            ),
            **detail,
        )

    def preflighted(self, entry: Entry, block: Block, call: Call) -> None:
        """Show and record one preflight as it finishes: passed, or each failure it names, the
        sign-in it waits for or an excerpt of its output, and its log."""
        if call.status == "done":
            self.finish(entry, block, "passed", compact=True)
            return
        for event in call.finished("preflight"):
            for failure in event.get("failures", []):
                block.add("Failure:", failure)
        if call.status == "sign-in":
            block.add("Sign-In:", call.message)
        if not block.lines:
            self.excerpt(block, call)
        self.finish(entry, block, WAITING_FOR.get(call.status, "failed"))

    @staticmethod
    def preflight_failure(entry: Entry, call: Call) -> str:
        """One preflight that didn't pass, in a sentence: what it waits for, or that it failed and,
        when it looked temporary and its retries are spent, why, and that nothing changed."""
        if call.status in WAITS or call.temporary:
            said = call.message if call.message.endswith(".") else f"{call.message}."
            return f"{entry.component}'s preflight {'waits' if call.status in WAITS else 'failed'}: {said}"
        return f"{entry.component}'s preflight failed."

    def drift_check(self, entry: Entry, block: Block) -> None:
        """Plan every component's phases on the commit its walk last applied, as its applied
        commit records it, or, before any walk recorded one, as its last publication's `source`
        stamp names it: only a real change made outside a run is drift, and work merged to `main`
        that no walk has applied is pending, which the walk applies. It fails closed: Component
        Outputs it can't read whole stop it, naming what. Only a component registered and never
        published, or not registered yet, or one whose walk an apply left part way, is left out,
        and each of its steps asks. A step that drifted is planned again on `main` when `main` is
        ahead of what it applied: with no real change there, the change made outside a run is
        written into the configuration, and the walk goes on. On drift it stops before anything
        changes, naming the steps."""
        # Only this drift check's plans count: an earlier attempt's, before a stop, never do.
        self.record.data.pop("drift_plans", None)
        aggregate, last = self.read_applied(block)
        self.check_mapped(block, aggregate)
        left_part_way = self.session.shared.unfinished_walks() if self.session.shared is not None else {}
        walked, unchecked = [], []
        for repository in self.repositories:
            for component in repository.components:
                part_way = left_part_way.get(component.key)
                commit = last.get(component.key)
                if part_way is not None:
                    block.add(
                        "Left Out:",
                        f"{component.key}: an apply left its walk part way, at "
                        f"{str(part_way.get('commit'))[:12]}; each of its steps asks",
                    )
                elif commit is None:
                    block.add(
                        "Left Out:", f"{component.key}: no walk has applied it yet; each of its steps asks"
                    )
                else:
                    walked.append((repository, component.key, commit))
                    continue
                unchecked.append(component.key)
        self.record.data["unchecked_components"] = unchecked
        # One copy per repository, at one commit after another when its components last applied
        # different ones.
        commits: dict[str, list[str]] = {}
        for repository, _, commit in walked:
            commits.setdefault(repository.key, [])
            if commit not in commits[repository.key]:
                commits[repository.key].append(commit)
        results: list = []
        for turn in range(max((len(found) for found in commits.values()), default=0)):
            copies = {
                key: self.applied_place(block, key, found[turn], walked)
                for key, found in commits.items()
                if turn < len(found)
            }
            versions = self.applied_versions(copies)
            phases = []
            for repository, component, commit in walked:
                if commits[repository.key].index(commit) != turn:
                    continue
                found = self.declarations(
                    {repository.key: copies[repository.key]}, only=(repository.key, component)
                )
                declared = found[(repository.key, component)]
                for phase in drift_phases(repository, component, declared):
                    phases.append((repository, component, phase, declared.command))
            results += self.plan_phases(block, phases, copies, versions)
        self.judge_drift(
            entry,
            block,
            results,
            {(repository.key, component): commit for repository, component, commit in walked},
        )

    def applied_place(self, block: Block, repository: str, commit: str, walked: list) -> Path:
        """The repository's applied copy at the commit a walk last applied, fetched from its
        origin when it's not here; one GitHub no longer has stops, naming what records it again."""
        mapped = self.instance.components[repository]
        try:
            return places.checkout(places.clone(self.instance, places.APPLIED, mapped), commit)
        except places.Missing as missing:
            named = [
                (item.key, component)
                for item, component, found in walked
                if item.key == repository and found == commit
            ]
            block.add(
                "Not Found:", f"{commit}, which {', '.join(component for _, component in named)} last applied"
            )
            raise Stop(
                "failed",
                f"{missing}, which the drift check plans {', '.join(component for _, component in named)} "
                "on; nothing has changed.",
                reason="applied-commit-missing",
                next=tuple(
                    places.missing_commit(
                        self.instance,
                        repository,
                        component,
                        self.command_of(repository, component),
                        commit,
                    )
                    for _, component in named
                ),
            ) from None

    def command_of(self, repository: str, component: str) -> str:
        """The command a component of the release runs as."""
        listed = self.manifest.repository(repository)
        return next(item.command for item in listed.components if item.key == component)

    def applied_versions(self, copies: dict[str, Path]) -> dict[str, list[str]]:
        """The Platform Version each repository's applied commit records, which the drift check
        plans its code with: the one its records name together or, after a part of an upgrade
        applied, each repository's own, from its record or the release that holds its commit.
        One none of them names stops before any plan."""
        recorded = {key: SourceRecord.read(place) for key, place in copies.items()}
        given = tuple(self.session.candidates())
        together = targets.standing(
            self.instance,
            self.session.releases,
            {key: value for key, value in recorded.items() if value is not None},
            given=given,
        )
        found = {}
        for key, value in recorded.items():
            version = together.version or together.candidate
            if version is None and value is not None:
                version = value.platform_version
                if version is None:
                    alone = targets.standing(self.instance, self.session.releases, {key: value}, given=given)
                    version = alone.version or alone.candidate
            if version is None:
                raise Stop(
                    "failed",
                    UNKNOWN_VERSION,
                    reason="unknown-platform-version",
                    next=(
                        Next(
                            self.restart(),
                            "on the workstation that prepared the candidate this instance runs",
                        ),
                    ),
                )
            found[key] = ["--platform-version", version]
        return found

    def read_applied(self, block: Block) -> tuple[dict, dict[str, str | None]]:
        """The instance's Component Outputs, read by the first repository's owner from `main`'s
        code, and each component's applied commit, read whole for the components this walk
        includes: whatever can't be read stops, naming it."""
        copy = self.checkout(self.repositories[0].key)
        found, warnings = self.screen.run_live(
            block, applied.read, self.instance, self.manifest, copy, self.log
        )
        included = {component.key for repository in self.repositories for component in repository.components}
        problems = applied.problems(found, warnings, included)
        commits, missing = applied.applied_commits(found)
        problems = problems or missing
        if problems:
            for problem in problems:
                block.add("Not Read:", problem)
            raise applied.unread(
                "The drift check can't read what the platform last applied",
                problems,
                Next("dp-instance resume", "checks again once it can be read"),
            )
        return found, commits

    def check_mapped(self, block: Block, aggregate: dict) -> None:
        """An apply never leaves one of the instance's repositories behind. A repository the
        release lists and the map doesn't hold is skipped only when the instance has none: no
        component of it is registered in the instance's Component Outputs."""
        if not self.absent:
            return
        listed = {
            component.key
            for repository in self.manifest.order
            if repository.key in self.absent
            for component in repository.components
        }
        missing = sorted({url for key, url in registrations(aggregate) if key in listed})
        if not missing:
            return
        for url in missing:
            block.add("Not Mapped:", f"{url}: the instance has it, and the repository map doesn't")
        raise Stop(
            "failed",
            "This instance has repositories the map doesn't hold, which an apply without them would leave "
            "behind. Nothing has changed.",
            reason="unmapped",
            missing=missing,
            next=(
                *(Next(f"git clone {url}", "clones it into the instance folder") for url in missing),
                Next("dp-instance setup", "maps it with the instance's other repositories"),
                Next(self.restart(), "starts the apply again with every repository"),
            ),
        )

    def plan_phases(
        self, block: Block, phases: list, copies: dict[str, Path], versions: dict[str, list[str]]
    ) -> list:
        """Plan the phases, each on its repository's copy, in parallel."""

        def plan_one(target_phase):
            repository, component, phase, command = target_phase
            file = self.record.file("plans", f"drift-{component}-{phase}.txt")
            file.unlink(missing_ok=True)
            call = Cli(command, copies[repository.key], self.log).run(
                "plan", "--phase", phase, "--json", "--plan-file", str(file), *versions[repository.key]
            )
            return repository.key, component, phase, file, call

        if not phases:
            return []
        # The longest first, as the last drift check measured them, so the slowest phase never
        # starts last; one never measured goes first.
        times = self.drift_times()
        phases = sorted(phases, key=lambda item: -times.get(f"{item[1]}/{item[2]}", float("inf")))
        block.message = f"Drift Check: planning {len(phases)} phases, {PHASES_AT_ONCE} at a time"
        with ThreadPoolExecutor(max_workers=PHASES_AT_ONCE) as pool:
            results = self.screen.run_live(block, lambda: list(pool.map(plan_one, phases)))
        block.message = ""
        self.keep_drift_times(
            {f"{component}/{phase}": round(call.seconds, 1) for _, component, phase, _, call in results}
        )
        return results

    def drift_times(self) -> dict[str, float]:
        """Each phase's time at the last drift check on this workstation; none when it has none."""
        try:
            found = json.loads((self.instance.state / DRIFT_TIMES).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {key: value for key, value in found.items() if isinstance(value, (int, float))}

    def keep_drift_times(self, times: dict[str, float]) -> None:
        try:
            text = json.dumps(times, sort_keys=True)
            (self.instance.state / DRIFT_TIMES).write_text(text, encoding="utf-8")
        except OSError:
            pass

    def judge_drift(
        self, entry: Entry, block: Block, results: list, applied: dict[tuple[str, str], str]
    ) -> None:
        """Every phase's plan: a failure stops or is one decision to go on without it; a real
        change is drift, unless `main`, ahead of what the component applied, plans none there."""
        failures = [
            (repository, component, phase, call)
            for repository, component, phase, _, call in results
            if call.status != "done"
        ]
        # A plan that failed the way a temporary failure does, its retries spent, stops for a
        # resume, unless another phase waits on a person.
        if any(call.temporary for *_, call in failures) and not any(
            call.status in WAITS for *_, call in failures
        ):
            for _, component, phase, call in failures:
                block.add("Failure:", f"{component} {phase}: {call.message}")
            block.add("Log:", self.relative(self.record.log_path))
            raise Stop(
                "failed",
                "The drift check couldn't plan every phase, and the failure looked temporary; nothing "
                "has changed.",
                reason="transient",
                next=(
                    Next(
                        "dp-instance resume",
                        "plans the drift check again and carries on, once the service answers",
                    ),
                ),
            )
        if failures:
            self.planning_failed(block, failures)
        drifted = False
        # Each step's plan, by its entry: its status and, for a Terraform step, the hash of its
        # inputs, so a later walk can tell when the drift check already planned exactly what it
        # would plan.
        planned = self.record.data["drift_plans"] = {}
        for repository, component, phase, file, call in results:
            for event in call.finished():
                step = event["step"]
                real = real_change(event)
                planned[f"{repository}/{component}/{phase}/{step}"] = {
                    "status": event.get("status"),
                    **({"inputs_hash": event["inputs_hash"]} if event.get("inputs_hash") else {}),
                }
                self.optional(event)
                if real and self.written_into_main(repository, component, phase, step, applied):
                    block.add(
                        "Drift Accepted:",
                        f"{component} {phase}/{step}: main plans no change there, so the configuration "
                        "holds it",
                    )
                elif real:
                    drifted = True
                    block.add("Drift:", f"{component} {phase}/{step}: {event.get('summary') or 'a change'}")
                    if file.exists():
                        block.add("Plan File:", self.relative(file))
                elif event.get("status") == "pending":
                    block.add(
                        "Pending:",
                        f"{component} {phase}/{step}: waits on outside work or an earlier change",
                    )
        self.record.save()
        if drifted:
            raise Stop(
                "failed",
                "The platform differs from the code and configuration it last applied: a change made "
                "outside a run; nothing has changed. Report the drift and its plan files to the person "
                "accountable for the instance: a run never fixes drift. They decide whether to write "
                "it into the configuration, with dp-instance prepare, or revert it.",
                reason="drift",
                next=(Next(self.restart(), "starts the apply again once nothing drifts"),),
            )
        if failures:
            self.go_on_unchecked(entry, block, failures)
            self.record.data["unchecked_components"] = sorted(
                {
                    *self.record.data.get("unchecked_components", []),
                    *(component for _, component, _, _ in failures),
                }
            )
            self.finish(
                entry,
                block,
                "approved",
                unchecked=[
                    f"{repository}/{component}/{phase}" for repository, component, phase, _ in failures
                ],
            )
            return
        self.finish(entry, block, "no drift", compact=True)

    def written_into_main(
        self, repository: str, component: str, phase: str, step: str, applied: dict[tuple[str, str], str]
    ) -> bool:
        """Whether a step that drifted plans no real change on `main`, when `main` is ahead of
        the commit the component last applied: the change made outside a run is written into
        the configuration, as the drift stop advises."""
        main = (self.record.data.get("main") or {}).get(repository)
        if main is None or main == applied.get((repository, component)):
            return False
        file = self.record.file("plans", f"drift-{component}-{phase}-{step}-main.txt")
        call = self.cli(repository, self.command_of(repository, component)).run(
            "plan", "--phase", phase, "--step", step, "--json", "--plan-file", str(file), *self.version
        )
        if call.status != "done":
            return False
        events = call.finished(step)
        if not events:
            return False
        # Only no change, or a change that isn't a real one; pending or anything else isn't proof.
        status = events[-1].get("status")
        return status == "no-change" or (status == "change" and not real_change(events[-1]))

    def planning_failed(self, block: Block, failures: list[tuple[str, str, str, Call]]) -> None:
        """Show each phase the drift check couldn't plan. A sign-in to renew, or anything else a
        component waits on, stops the run for it; any other failure is left to the decision to
        go on without those phases, once the phases that planned show no drift."""
        for _, component, phase, call in failures:
            label = "Sign-In:" if call.status == "sign-in" else "Failure:"
            block.add(label, f"{component} {phase}: {call.message}")
        block.add("Log:", self.relative(self.record.log_path))
        waiting = next((call for *_, call in failures if call.status in WAITS), None)
        if waiting is not None:
            raise Stop(
                waiting.status,
                "The drift check couldn't plan every phase; nothing has changed.",
                reason="drift-check",
                next=(
                    *self.sign_in_next(waiting),
                    Next(
                        "dp-instance resume", "plans the drift check again and carries on, once that's done"
                    ),
                ),
            )

    def go_on_unchecked(self, entry: Entry, block: Block, failures: list[tuple[str, str, str, Call]]) -> None:
        """Going on without the phases the drift check couldn't plan is one decision, with an ID
        drawn from which phases they are, never from their errors, which name requests and
        times. `dp-instance resume --approve` gives it; `--auto-approve` doesn't cover it."""
        phases = sorted({(repository, component, phase) for repository, component, phase, _ in failures})
        content = json.dumps({"kind": "unplanned", "phases": phases}, sort_keys=True)
        identifier = hashlib.sha256(content.encode()).hexdigest()[:12]
        count = len(phases)
        summary = (
            f"{count} phase{'s' if count != 1 else ''} couldn't be planned on the code the platform "
            f"runs; going on leaves {'them' if count != 1 else 'it'} unchecked for drift"
        )
        if identifier in self.options.approvals:
            block.add("Step Plan:", Text(f"{summary}   approved by --approve", style="green"))
            return
        block.outcome = "change"
        block.add("Step Plan:", summary)
        block.add("Decision:", identifier)
        notes = self.notes_line()
        if notes:
            block.add("Release Notes:", notes)
        if self.session.interactive:
            key = self.screen.ask(
                block, [("y", "go on"), ("n", "stop the run here")], "Go on without checking them?"
            )
            if key == "y":
                block.lines = [
                    line for line in block.lines if line.label not in ("Decision:", "Release Notes:")
                ]
                block.lines[-1].text.append("   approved", style="green")
                return
        command = f"dp-instance resume --approve {identifier}"
        raise Stop(
            "decision",
            f"{summary}.",
            reason="decision",
            decision={
                "id": identifier,
                "summary": summary,
                "entry": entry.id,
                "phases": [
                    {"repository": repository, "component": component, "phase": phase, "error": call.message}
                    for repository, component, phase, call in failures
                ],
                "resume": command,
            },
            next=(
                Next(
                    command,
                    "goes on with those phases unchecked for drift: approve only when the release's notes "
                    "say it fixes them, and otherwise send this list to Data Methods",
                ),
            ),
            **self.notes_detail(),
        )


# A Terraform step's summary, before the engine counted each decision: what it adds, changes and
# destroys, a replacement counting as both an add and a destroy.
TERRAFORM_COUNTS = re.compile(r"(\d+) to (add|change|destroy)\b")
# What an engine before decisions reports as a change that isn't one on the platform: a handoff
# document it would rewrite, or outputs it would publish again.
NOT_REAL = ("its handoff document changes", "its published outputs are out of date")
# Each phase's time at the last drift check, kept to plan the longest first.
DRIFT_TIMES = "drift-times.json"
# A real change, as the design counts one: create, update in place, replace or destroy.
REAL = ("create", "update", "replace", "destroy")


def real_change(event: dict) -> bool:
    """Whether a plan's finished event is a real change, as the design counts drift: a create,
    an update in place, a replacement or a destroy. Output changes, handoff document rewrites,
    forgets, moves and imports aren't. Each decision's counts say it, a native decision counting
    as real; before the engine counted decisions, a Terraform step's summary does, and the text
    of a change that's only a handoff rewrite or outputs to publish again."""
    if event.get("status") != "change":
        return False
    decisions = event.get("decisions")
    if isinstance(decisions, list):
        return any(
            not isinstance(item.get("counts"), dict) or any(item["counts"].get(kind) for kind in REAL)
            for item in decisions
            if isinstance(item, dict)
        )
    summary = str(event.get("summary") or "")
    counted = TERRAFORM_COUNTS.findall(summary)
    if counted:
        return any(int(number) for number, _ in counted)
    return not summary.startswith(NOT_REAL)
