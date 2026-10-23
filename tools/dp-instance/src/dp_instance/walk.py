"""A run: the sequencer walking the steps, one process per step.

Before anything changes, every component's `preflight` step runs and, in an upgrade run, the
drift check. Then each step is planned only after the steps before it have applied: a step
with no change passes, and at a step with a change the run stops for review and, on
approval, applies exactly that plan, which the component verifies before the next step
starts. At a terminal a person is asked with single keypresses; without one, or with
`--json`, every question is a stop naming the command that continues.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from itertools import takewhile
from pathlib import Path

from rich.text import Text

from . import github, gitops, handoffs, prompts, targets
from .components import Call, Cli, Log
from .instance import (
    INFRASTRUCTURE,
    INFRASTRUCTURE_COMMAND,
    SETUP,
    SOURCE_RECORD,
    SourceRecord,
    registrations,
)
from .manifest import Manifest
from .outcome import WAITING_FOR, WAITS, Next, Result, Stop, failed
from .record import Record
from .screen import Block, duration
from .sequence import MERGE, Declaration, Entry, Plan, declaration
from .session import Session

ENGINE_PROJECT = Path("tools/dp-template-engine/pyproject.toml")
# The first engine with the common interface; a checkout bundling an older one is at the
# 2026.2 to 2026.3 hop. Deleted at the 2026.4 stop.
INTERFACE = (0, 14, 0)
STAGES = {"before": "before anything changes", "run": "run sequence"}
# The checks before the run, as a resumed run names those that passed earlier.
CHECKS = {"preflight": "preflights", "drift": "the drift check", "published": "the published outputs check"}
# The owner's `upgrade` steps that change only the run's branch, which its pull request shows and
# the `merge` lands. They run with --auto-approve, so they stop only for what it never covers:
# restoring an edited template file or removing a file.
BRANCH_STEPS = ("source", "pull-request")
# How many of a decision's plan lines its JSON result carries.
PLAN_HEAD = 40
LEVELS = ("repository", "component", "phase", "step")
DRIFT_TEXT = (
    "Every component's phases planned, read only and in parallel, on the code the platform runs: "
    "the checkout, or its main while an upgrade is under way on its branch; any change is drift."
)
PUBLISHED_TEXT = (
    "At the 2026.3 hop, each component's published outputs, read only: each must be last "
    "published cleanly from the template commit its repository records."
)


def stage_of(entry: Entry) -> str:
    return "before" if entry.kind in ("preflight", "drift", "published") else "run"


def level_key(entry: Entry, level: str) -> tuple:
    """Which repository part, component or phase the entry is in. A repository has two parts in
    an upgrade: its code, the owner's `upgrade` phase and the `migrate` steps, then its platform."""
    part = (entry.kind in ("upgrade", "migrate"), entry.repository)
    return {
        "repository": (part,),
        "component": (part, entry.component),
        "phase": (part, entry.component, entry.phase),
    }[level]


def bundled_engine(checkout: Path) -> tuple[int, int, int] | None:
    """The engine version `main` bundles: the code last merged, which the platform runs, even
    while an upgrade under way has the checkout on its branch."""
    commit = gitops.main_commit(checkout)
    if commit is not None:
        text = gitops.show(checkout, commit, ENGINE_PROJECT.as_posix())
    else:
        try:
            text = (checkout / ENGINE_PROJECT).read_text(encoding="utf-8")
        except OSError:
            text = None
    if text is None:
        return None
    match = re.search(r'^version\s*=\s*"(\d+)\.(\d+)\.(\d+)', text, re.MULTILINE)
    return tuple(int(part) for part in match.groups()) if match else None


def at_first_hop(checkout: Path) -> bool:
    """Whether the repository's `main` predates the common interface: the 2026.2 to 2026.3
    hop."""
    version = bundled_engine(checkout)
    return version is not None and version < INTERFACE


def by_system(sections: list[dict]) -> dict[str, int]:
    """How many pieces of outside work each system holds, as its handoff page groups them, in
    the order of the systems' names."""
    found: dict[str, set] = {}
    for item in sections:
        found.setdefault(item.get("system", "other"), set()).add(item.get("id", item.get("title")))
    return {system: len(found[system]) for system in sorted(found, key=handoffs.system_name)}


class Run:
    def __init__(self, session: Session, record: Record, manifest: Manifest):
        self.session = session
        self.screen = session.screen
        self.instance = session.instance
        self.options = session.options
        self.record = record
        self.manifest = manifest
        self.log = Log(record.log_path)
        mapped = self.instance.components
        self.repositories = tuple(item for item in manifest.order if item.key in mapped)
        self.unlisted = sorted(key for key in mapped if manifest.repository(key) is None)
        self.absent = [item.key for item in manifest.order if item.key not in mapped]
        self.started = time.monotonic()
        self._shown: tuple | None = None
        self._asked_by: str | None = None
        self._position = 0
        self._walked: set[str] = set()
        self._fetching = threading.Lock()
        # The entry under way and its block, which a stop finishes.
        self._entry: Entry | None = None
        self._block: Block | None = None

    # What the run is ------------------------------------------------------------------

    @property
    def upgrading(self) -> bool:
        return self.record.kind == "upgrade"

    @property
    def channel(self) -> str | None:
        return self.record.data.get("channel")

    @property
    def change(self) -> str | None:
        return self.record.data.get("change")

    @property
    def first_hop(self) -> set[str]:
        return set(self.record.data.get("first_hop", []))

    @property
    def unchanged(self) -> set[str]:
        """The repositories the upgrade leaves as they are, which `build` records."""
        return set(self.record.data.get("unchanged", []))

    def checkout(self, repository: str) -> Path:
        return self.instance.components[repository].path

    def source(self, repository: str) -> Path:
        """The owner's source at the target, fetched with the instance's template access into
        `.dp-instance/src/`. An upgrade runs its CLI against the checkout for the
        preflights and the owner's `upgrade` phase, so a fix to either works in the upgrade
        that ships it. Only the target's source is kept: an earlier target's goes, environment
        and all."""
        commit = self.manifest.sources[repository]
        folder = self.instance.own("src", repository)
        with self._fetching:
            if not gitops.is_checkout_root(folder) or gitops.head(folder) != commit:
                gitops.remove(folder)
                record = SourceRecord.read(self.checkout(repository))
                url = record.release_url if self.channel == "stable" else record.development_url
                gitops.fetch_commit(folder, url, commit)
        return folder

    def cli(self, repository: str, command: str, *, fetched: bool = False) -> Cli:
        project = self.source(repository) if fetched else None
        return Cli(command, self.checkout(repository), self.log, project)

    def cli_for(self, entry: Entry) -> Cli:
        return self.cli(entry.repository, entry.command, fetched=entry.fetched)

    # What the platform runs ---------------------------------------------------------------

    def in_progress(self, repository: str) -> bool:
        """Whether an upgrade is under way in the repository: its checkout is on this run's
        branch, ahead of `main`, as a run that stopped leaves it. The platform still runs
        `main`'s code there, since no platform step merges before the repository's `merge`."""
        if not self.change:
            return False
        checkout = self.checkout(repository)
        main = gitops.main_commit(checkout)
        return (
            main is not None
            and gitops.branch(checkout) == gitops.run_branch(self.change)
            and gitops.head(checkout) != main
        )

    def applied_record(self, repository: str) -> SourceRecord:
        return self.instance.record(repository, applied=self.in_progress(repository))

    def branch_records(self, repository: str) -> set[str]:
        """The template commits an upgrade under way has recorded on its branch since `main`.
        Its steps that applied from the branch published from one of them, which is the
        upgrade's own work, not drift."""
        if not self.in_progress(repository):
            return set()
        checkout = self.checkout(repository)
        found = set()
        for commit in gitops.file_history(checkout, gitops.main_commit(checkout), SOURCE_RECORD.as_posix()):
            record = SourceRecord.read(checkout, commit)
            if record is not None:
                found.add(record.commit)
        return found

    @contextmanager
    def applied_code(self, repositories, block: Block):
        """Where each repository's applied code is: its checkout, or, for an upgrade under way,
        a throwaway checkout of its `main`, read only and removed afterwards."""
        places, made = {}, []
        try:
            for repository in repositories:
                checkout = self.checkout(repository.key)
                if not self.in_progress(repository.key):
                    places[repository.key] = checkout
                    continue
                main = gitops.main_commit(checkout)
                block.add(
                    "Under Way:",
                    f"{repository.key} is on {gitops.branch(checkout)}; it's judged from main at "
                    f"{main[:12]}, the code the platform runs, and the run carries on that branch",
                )
                folder = self.instance.own("main", repository.key)
                gitops.remove(folder)
                made.append(folder)
                places[repository.key] = gitops.applied_checkout(checkout, main, folder)
            yield places
        finally:
            for folder in made:
                try:
                    gitops.remove(folder)
                except OSError:
                    pass  # the next drift check removes it before checking out again

    # The sequence ---------------------------------------------------------------------

    def declarations(
        self, places: dict[str, Path] | None = None, *, target: bool = False
    ) -> dict[tuple[str, str], Declaration]:
        """Each component's `steps --json`, read in parallel: from each checkout, or from the
        code in ``places`` for the repositories it holds. With ``target``, an upgrade's code
        part reads the target's, from the owner's fetched CLI against the checkout."""
        if places is None:
            places = {repository.key: self.checkout(repository.key) for repository in self.repositories}
        jobs = [
            (repository.key, component)
            for repository in self.repositories
            if repository.key in places
            for component in repository.components
        ]

        def read(job):
            repository, component = job
            if target and repository in self.first_hop:
                # The target's declaration, read from its own source: the checkout's
                # configuration may still be in 2026.2's shape, which its conditions can't read.
                source = self.source(repository)
                cli = Cli(component.command, source, self.log, source)
            elif target:
                cli = self.cli(repository, component.command, fetched=True)
            else:
                cli = Cli(component.command, places[repository], self.log)
            found = declaration(cli.document("steps", "--json"), component.command)
            if found.key != component.key:
                raise failed(
                    f"{component.command} declares the component {found.key}; the manifest "
                    f"names {component.key}."
                )
            return (repository, component.key), found

        block = Block("sequence", "", "", "", self.footer)
        block.message = f"Reading the steps of {len(jobs)} components"
        with ThreadPoolExecutor(max_workers=4) as pool:
            return dict(self.screen.run_live(block, lambda: list(pool.map(read, jobs))))

    def plan(self, declarations) -> Plan:
        return Plan(self.manifest, self.repositories, declarations, frozenset(self.unchanged))

    def find_unchanged(self) -> list[str]:
        """The repositories that need no upgrade: each already records its target, a commit a
        release before the target names, as the Command Center reuses the release commit of a
        template that didn't change."""
        named = targets.named_before(self.session.releases, self.manifest.platform_version)
        return [
            repository.key
            for repository in self.repositories
            if self.applied_record(repository.key).commit == self.manifest.sources[repository.key]
            and self.manifest.sources[repository.key] in named.get(repository.key, set())
        ]

    def build(self) -> None:
        """The entries of a new run: before anything changes, then the code (an upgrade) or
        the platform's steps (an apply). An upgrade runs its preflights and each owner's
        `upgrade` phase from the target's fetched CLI, as the target declares them; the drift
        check stays on the code the platform runs. A repository the upgrade leaves unchanged
        takes no `upgrade` phase."""
        if self.upgrading:
            self.record.data["unchanged"] = self.find_unchanged()
        plan = self.plan(self.declarations(target=self.upgrading))
        entries = plan.preflights()
        if self.upgrading:
            for entry in entries:
                entry.fetched = True
            if any(item.key not in self.first_hop for item in self.repositories):
                entries.append(Entry(id="drift-check", kind="drift", description=DRIFT_TEXT))
            if self.first_hop:
                entries.append(Entry(id="published-outputs", kind="published", description=PUBLISHED_TEXT))
            code = plan.code(self.first_hop)
            for entry in code:
                entry.fetched = entry.kind == "upgrade"
            entries += code
        else:
            entries += plan.platform(upgrade=False)
            self.record.data["platform"] = True
        self.record.entries = entries
        self.record.save()

    def build_platform(self) -> None:
        """An upgrade's platform steps, read from the code its `upgrade` phases synced."""
        plan = self.plan(self.declarations())
        self.record.entries = self.record.entries + plan.platform(upgrade=True)
        self.record.data["platform"] = True
        self.record.save()

    # The screen -----------------------------------------------------------------------

    def footer(self) -> str:
        total = len(self.record.data.get("entries", [])) or 1
        return (
            f"run step {max(self._position, 1)} of {total} · {self.record.data['goal']} · "
            f"{duration(time.monotonic() - self.started)} so far"
        )

    def header(self) -> None:
        self.screen.header(
            self.record.kind,
            ("instance", self.instance.key),
            ("Platform", self.manifest.platform_version),
            ("run", self.record.id),
        )
        if self.unlisted:
            self.screen.note(f"Not in this release, left as they are: {', '.join(self.unlisted)}.")
        if self.record.data.get("chosen"):
            self.screen.note(self.record.data["chosen"])
        self.screen.emit(
            {
                "event": "run",
                "run": self.record.id,
                "kind": self.record.kind,
                "goal": self.record.data["goal"],
            }
        )

    def enter(self, entry: Entry, until: str = "step") -> None:
        """Name each level as it's entered: the stage, then repository, component and phase,
        down to ``until``, the level the caller shows itself."""
        stage = stage_of(entry)
        if self._shown is None or self._shown[0] != stage:
            self.screen.stage(STAGES[stage])
            self._shown = (stage, None, None, None)
        if stage == "before":
            return
        depth = LEVELS.index(until)
        _, repository, component, phase = self._shown
        (part,) = level_key(entry, "repository")
        if depth > 0 and part != repository:
            self.screen.blank()
            self.screen.level("repository", entry.repository_counter)
            self.screen.labelled("Repo Name:", Text(entry.repository, style="bold"))
            self.screen.labelled("Repo Desc:", entry.repository_description)
            branch = gitops.branch(self.checkout(entry.repository))
            if branch:
                self.screen.labelled("Repo Branch:", branch)
                if branch != "main":
                    pull = github.pull_request(self.checkout(entry.repository), branch)
                    if pull:
                        self.screen.labelled(
                            "Repo PR:", f"{github.describe(pull)} {pull.get('url', '')}".strip()
                        )
            repository, component, phase = part, None, None
        if depth > 1 and entry.component != component:
            self.screen.blank()
            self.screen.level(
                "component", entry.component_counter + (" (Repository Owner)" if entry.owner else "")
            )
            self.screen.labelled("Component Name:", Text(entry.component, style="bold"))
            self.screen.labelled("Component Desc:", entry.component_description)
            component, phase = entry.component, None
        if depth > 2 and entry.kind == "migrate":
            self.screen.blank()
            phase = None
        elif depth > 2 and entry.phase != phase:
            self.screen.blank()
            self.screen.level("phase", entry.phase_counter)
            self.screen.labelled("Phase Name:", Text(entry.phase, style="bold"))
            self.screen.labelled("Phase Desc:", entry.phase_description)
            self.screen.blank()
            phase = entry.phase
        self._shown = (stage, repository, component, phase)

    def entered(self, entry: Entry, level: str) -> bool:
        """Whether the screen already shows the entry's repository part, component or phase."""
        if self._shown is None or self._shown[0] != stage_of(entry):
            return False
        depth = LEVELS.index(level)
        return self._shown[1 : depth + 2] == level_key(entry, level)

    def replay(self, start: int) -> None:
        """Show what a resumed run finished earlier, from entry ``start`` on, in the fewest
        lines: the checks before the run that passed take one line, and in the run, the largest
        level not yet entered whose entries all finished takes one line, so a step done earlier
        keeps a line of its own only in a phase still under way."""
        entries = self.record.entries
        entry = entries[start]
        if stage_of(entry) == "before":
            self.replay_checks(start)
            return
        for depth, level in enumerate(LEVELS[:3]):
            if level == "phase" and entry.kind == "migrate":
                break
            if self.entered(entry, level):
                continue
            key = level_key(entry, level)
            group = []
            for item in entries[start:]:
                if level_key(item, level) != key:
                    break
                group.append(item)
            if not all(self.record.finished(item.id) for item in group):
                continue
            self.enter(entry, until=level)
            name, counter = {
                "repository": (entry.repository, entry.repository_counter),
                "component": (entry.component, entry.component_counter),
                "phase": (entry.phase, entry.phase_counter),
            }[level]
            seconds = sum(self.record.outcome(item.id).get("seconds") or 0 for item in group)
            self.screen.done(level, counter, name, len(group), seconds)
            self._walked.update(item.id for item in group)
            self._shown = self._shown[: depth + 1] + (None,) * (3 - depth)
            return
        self.enter(entry)
        self._walked.add(entry.id)
        noted = self.record.outcome(entry.id)
        block = self.block(entry)
        block.outcome, block.seconds, block.live = f"{noted['outcome']} earlier", noted["seconds"], False
        self.screen.show(block, compact=True)

    def replay_checks(self, start: int) -> None:
        """The checks before the run (preflights, the drift check, published outputs) are its
        start: those that passed earlier take one line and don't run again. The first that
        didn't pass runs, and every check after it."""
        entries = self.record.entries
        stage = [item for item in entries if stage_of(item) == "before"]
        passed = list(
            takewhile(
                lambda item: stage_of(item) == "before" and self.record.finished(item.id), entries[start:]
            )
        )
        names = list(dict.fromkeys(CHECKS[item.kind] for item in passed))
        named = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        seconds = sum(self.record.outcome(item.id).get("seconds") or 0 for item in passed)
        self.enter(passed[0])
        self.screen.done("checks", f"{len(passed)}/{len(stage)}", named, len(passed), seconds)
        self._walked.update(item.id for item in passed)

    def block(self, entry: Entry) -> Block:
        if entry.kind == "preflight":
            return Block(
                "preflight", entry.preflight_counter, entry.component, entry.description, self.footer
            )
        if entry.kind == "drift":
            return Block("drift check", "", "", entry.description, self.footer)
        if entry.kind == "published":
            return Block("published outputs", "", "", entry.description, self.footer)
        if entry.kind == "migrate":
            return Block("migrate step", "", "migrate", entry.description, self.footer)
        return Block("step", entry.step_counter, entry.step, entry.description, self.footer)

    def finish(self, entry: Entry, block: Block, outcome: str, *, compact: bool = False, **detail) -> None:
        block.finish(outcome)
        self.screen.show(block, compact=compact and not block.lines)
        if entry.repository in self.instance.components:
            self.record.data["commits"][entry.repository] = gitops.head(self.checkout(entry.repository))
        self.record.note(entry, outcome, block.elapsed(), details=block.details(), **detail)
        self.screen.emit(
            {
                "event": "step",
                "run": self.record.id,
                "entry": entry.id,
                "kind": entry.kind,
                "repository": entry.repository or None,
                "component": entry.component or None,
                "phase": entry.phase or None,
                "step": entry.step or None,
                "outcome": outcome,
                "seconds": round(block.elapsed(), 1),
                **{key: value for key, value in detail.items() if value not in (None, [], {})},
            }
        )

    # Walking --------------------------------------------------------------------------

    def walk(self) -> Result:
        self.header()
        try:
            if not self.record.entries:
                self.build()
            self.walk_entries()
            if self.upgrading and not self.record.data.get("platform"):
                self.build_platform()
                self.walk_entries()
        except KeyboardInterrupt:
            return self.stopped(Stop("failed", "Interrupted.", reason="interrupted"))
        except Stop as stop:
            return self.stopped(stop)
        self.record.complete()
        self.screen.complete(time.monotonic() - self.started)
        self.recommend()
        left = self.left()
        if left:
            self.screen.next_block(left, self.relative(self.record.folder) + "/")
        else:
            self.screen.run_record(self.relative(self.record.folder) + "/")
        return Result(self.record.kind, "done", "Complete.", left, {"run": self.record.id})

    def left(self) -> tuple[Next, ...]:
        """What's left once an upgrade on `stable` completes: the next stop when one is
        published; otherwise it says nothing is."""
        if not self.upgrading or self.channel != "stable":
            return ()
        version = self.manifest.platform_version
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
                "dp-instance upgrade",
                f"takes Platform {upcoming}, {targets.describe_next(version, upcoming)}",
            ),
        )

    def walk_entries(self) -> None:
        for index, entry in enumerate(self.record.entries, 1):
            if entry.id in self._walked:
                continue
            self._position = index
            if self.record.finished(entry.id):
                self.replay(index - 1)
                continue
            self.enter(entry)
            self._walked.add(entry.id)
            block = self.block(entry)
            self._entry, self._block = entry, block
            handler = {
                "preflight": self.preflight,
                "drift": self.drift_check,
                "published": self.published_check,
                "migrate": self.migrate,
                "upgrade": self.step,
                "step": self.step,
            }[entry.kind]
            handler(entry, block)
            self._entry = self._block = None

    # Each kind of entry -----------------------------------------------------------------

    def messages(self, block: Block):
        """Show a component's latest line for a person under its live block."""

        def show(line: str) -> None:
            block.message = line

        return show

    def relative(self, path: Path) -> str:
        return self.session.relative(path)

    @property
    def version(self) -> list[str]:
        """The Platform Version the run brings the instance to, from its manifest, which
        `preflight`, `upgrade`, `plan`, `apply` and `verify` take."""
        return ["--platform-version", self.manifest.platform_version]

    @property
    def applied_version(self) -> list[str]:
        """The Platform Version the platform runs, which the drift check plans its code with:
        the published release each repository's `main` records, or the candidate this
        workstation upgraded to. The target's, when neither is known, such as on a workstation
        that didn't run the candidate."""
        instance, releases = self.session.instance, self.session.releases
        version = targets.current(instance, releases, applied=True).version or targets.candidate_of(
            instance, applied=True
        )
        return ["--platform-version", version or self.manifest.platform_version]

    def restart(self) -> str:
        """The command that starts this upgrade again."""
        origin = self.record.data.get("manifest_origin")
        if self.channel == "canary" and origin:
            return f"dp-instance upgrade --manifest {origin}"
        return f"dp-instance upgrade --to {self.manifest.platform_version}"

    def preflight(self, entry: Entry, block: Block) -> None:
        cli = self.cli_for(entry)
        call = self.screen.run_live(
            block, cli.run, "preflight", "--json", *self.version, on_line=self.messages(block)
        )
        if call.status == "done":
            self.finish(entry, block, "passed", compact=True)
            return
        for event in call.finished("preflight"):
            for failure in event.get("failures", []):
                block.add("Failure:", failure)
        again = Next(
            "dp-instance resume",
            "runs this preflight and every check after it, then carries on, once that's done",
        )
        if call.status in WAITS:
            self.wait(entry, block, call)
        if not block.lines:
            self.excerpt(block, call)
        message = f"{entry.component}'s preflight failed."
        if call.temporary:
            # Its retries spent: say it looked temporary and that nothing changed.
            message = f"{entry.component}'s preflight failed: {call.message}"
        raise Stop("failed", message, reason="preflight", next=(again,))

    def drift_check(self, entry: Entry, block: Block) -> None:
        target = self.manifest.platform_version
        if self.channel == "stable":
            already = [
                repository.key
                for repository in self.repositories
                if repository.key not in self.first_hop | self.unchanged
                and self.applied_record(repository.key).commit == self.manifest.sources[repository.key]
            ]
            if already:
                for name in already:
                    block.add("Drift:", f"{name} is already at Platform {target}")
                raise Stop(
                    "failed",
                    f"{', '.join(already)} already at Platform {target}: an upgrade to it is under way.",
                    reason="already-at-target",
                    next=(
                        Next(
                            "dp-instance resume",
                            "carries on the upgrade under way, if this workstation started it",
                        ),
                        Next(
                            "dp-instance apply",
                            "starts a new run on the checked-out code once every repository is at the target",
                        ),
                    ),
                )
        repositories = tuple(item for item in self.repositories if item.key not in self.first_hop)
        with self.applied_code(repositories, block) as places:
            if self.absent and INFRASTRUCTURE not in self.first_hop:
                self.check_mapped(block, self.aggregate(block, places))
            self.plan_drift(entry, block, repositories, places)

    def aggregate(self, block: Block, places: dict[str, Path]) -> dict:
        """The instance's Component Outputs, read by the Client Infrastructure Repository's CLI
        from its applied code."""
        infrastructure = self.instance.infrastructure
        if infrastructure is None:
            raise failed("The repository map holds no Client Infrastructure Repository.", SETUP)
        listed = self.manifest.repository(INFRASTRUCTURE)
        command = listed.owner.command if listed is not None else INFRASTRUCTURE_COMMAND
        cli = Cli(command, places.get(INFRASTRUCTURE, infrastructure.path), self.log)
        return self.screen.run_live(block, cli.document, "outputs", "aggregate")

    def check_mapped(self, block: Block, aggregate: dict) -> None:
        """An upgrade never leaves one of the instance's repositories behind. A repository the
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
        if (aggregate.get("enumeration") or {}).get("status", "complete") != "complete":
            raise Stop(
                "failed",
                "Some of the instance's registered components couldn't be read, so dp-instance can't "
                f"tell whether it has {', '.join(self.absent)}, which the map doesn't hold. Nothing has "
                "changed.",
                reason="unmapped",
                next=(Next(self.restart(), "starts the upgrade again once they can be read"),),
            )
        missing = sorted({url for key, url in registrations(aggregate) if key in listed})
        if not missing:
            return
        for url in missing:
            block.add("Not Mapped:", f"{url}: the instance has it, and the repository map doesn't")
        raise Stop(
            "failed",
            "This instance has repositories the map doesn't hold, which an upgrade without them would "
            "leave on their old release. Nothing has changed.",
            reason="unmapped",
            missing=missing,
            next=(
                *(Next(f"git clone {url}", "clones it into the instance folder") for url in missing),
                Next("dp-instance setup", "maps it with the instance's other repositories"),
                Next(self.restart(), "starts the upgrade again with every repository"),
            ),
        )

    def plan_drift(self, entry: Entry, block: Block, repositories, places: dict[str, Path]) -> None:
        """Plan every phase the drift check covers, as each repository's applied code declares
        them, where that code is."""
        phases = Plan(self.manifest, repositories, self.declarations(places)).drift_targets()
        version = self.applied_version

        def plan_one(target_phase):
            repository, component, phase = target_phase
            command = next(item.command for item in repository.components if item.key == component)
            file = self.record.file("plans", f"drift-{component}-{phase}.txt")
            file.unlink(missing_ok=True)
            call = Cli(command, places[repository.key], self.log).run(
                "plan", "--phase", phase, "--json", "--plan-file", str(file), *version
            )
            return repository.key, component, phase, file, call

        block.message = f"Planning {len(phases)} phases in parallel"
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = self.screen.run_live(block, lambda: list(pool.map(plan_one, phases)))
        block.message = ""
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
        # Each Terraform step's plan, by its entry: its status and the hash of its inputs, so a
        # later walk can tell when the drift check already planned exactly what it would plan.
        planned = self.record.data.setdefault("drift_plans", {})
        for repository, component, phase, file, call in results:
            for event in call.finished():
                if event.get("inputs_hash"):
                    planned[f"{repository}/{component}/{phase}/{event['step']}"] = {
                        "status": event.get("status"),
                        "inputs_hash": event["inputs_hash"],
                    }
                self.optional(event)
                if event.get("status") == "change":
                    drifted = True
                    block.add(
                        "Drift:", f"{component} {phase}/{event['step']}: {event.get('summary') or 'a change'}"
                    )
                    if file.exists():
                        block.add("Plan File:", self.relative(file))
                elif event.get("status") == "pending":
                    block.add(
                        "Pending:",
                        f"{component} {phase}/{event['step']}: waits on outside work or an earlier change",
                    )
        self.record.save()
        if drifted:
            raise Stop(
                "failed",
                "The platform differs from its code and configuration; nothing has changed. Report the "
                "drift and its plan files to the person accountable for the instance: an upgrade never "
                "fixes drift. They decide whether to write it into the configuration or revert it with a "
                "configuration change of its own.",
                reason="drift",
                next=(Next(self.restart(), "starts the upgrade again once nothing drifts"),),
            )
        if failures:
            self.go_on_unchecked(entry, block, failures)
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

    def planning_failed(self, block: Block, failures: list[tuple[str, str, str, Call]]) -> None:
        """Show each phase the drift check couldn't plan. A sign-in to renew, or anything else a
        component waits on, stops the run for it; any other failure is left to the decision to
        go on without those phases, once the phases that planned show no drift."""
        for _, component, phase, call in failures:
            block.add("Failure:", f"{component} {phase}: {call.message}")
        block.add("Log:", self.relative(self.record.log_path))
        waiting = next((call for *_, call in failures if call.status in WAITS), None)
        if waiting is not None:
            raise Stop(
                waiting.status,
                "The drift check couldn't plan every phase; nothing has changed.",
                reason="drift-check",
                next=(
                    *self.sign_in_next(waiting),
                    Next(self.restart(), "starts the upgrade again once that's fixed"),
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
        if self.session.interactive:
            key = self.screen.ask(
                block, [("y", "go on"), ("n", "stop the run here")], "Go on without checking them?"
            )
            if key == "y":
                block.lines = [line for line in block.lines if line.label != "Decision:"]
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
        )

    def published_check(self, entry: Entry, block: Block) -> None:
        """At the 2026.3 hop, 2026.2's plan can't run read only, so the drift check reads each
        component's published outputs instead: each must be last published cleanly, from the
        template commit its repository records. A component that never published its outputs,
        as the templates on the older engine never did, can't be checked that way: it's a
        decision to go on, since its 2026.3 steps plan against its live resources and each plan
        is reviewed before it applies. Deleted at the 2026.4 stop."""
        first = self.repositories[0]
        with self.applied_code((first,), block) as places:
            cli = Cli(first.owner.command, places[first.key], self.log)
            aggregate = self.screen.run_live(block, cli.document, "outputs", "aggregate")
        published = {
            item["registration"]["component"]["key"]: item
            for item in aggregate.get("components", [])
            if isinstance(item, dict) and "registration" in item
        }
        self.check_mapped(block, aggregate)
        problems, unpublished = [], []
        for repository in self.repositories:
            if repository.key not in self.first_hop:
                continue
            recorded = self.applied_record(repository.key)
            accepted = {recorded.commit, *self.branch_records(repository.key)}
            for component in repository.components:
                found = published.get(component.key)
                if found is None:
                    unpublished.append((component.key, "not registered, so it never published its outputs"))
                    continue
                if found.get("status") != "available":
                    if found.get("reason") == "unpublished":
                        unpublished.append((component.key, "registered, but it never published its outputs"))
                    else:
                        problems.append(
                            f"{component.key}: its outputs are {found.get('reason') or 'unavailable'}"
                        )
                    continue
                source = (found.get("outputs") or {}).get("source") or {}
                if source.get("dirty"):
                    problems.append(f"{component.key}: last published from a checkout with uncommitted edits")
                commit = (source.get("template") or {}).get("commit")
                if commit not in accepted:
                    since = " or one this run's branch has recorded since" if len(accepted) > 1 else ""
                    problems.append(
                        f"{component.key}: last published from template commit {commit or 'unknown'}, "
                        f"not {recorded.commit}, which main records{since}"
                    )
        for name, why in unpublished:
            block.add("Never Published:", f"{name}: {why}")
        if problems:
            for problem in problems:
                block.add("Not Clean:", problem)
            raise Stop(
                "failed",
                "Some components weren't last published cleanly from the commit their repository records.",
                reason="not-published",
                next=(
                    Next(
                        self.restart(),
                        "starts the upgrade again once each component named not clean is applied and "
                        "published cleanly from the commit its repository records, with its current tooling",
                    ),
                ),
            )
        if not unpublished:
            self.finish(entry, block, "published cleanly", compact=True)
            return
        self.go_on_unpublished(entry, block, unpublished)
        self.finish(entry, block, "approved", unpublished=[name for name, _ in unpublished])

    def go_on_unpublished(self, entry: Entry, block: Block, unpublished: list[tuple[str, str]]) -> None:
        """Going on with components that never published their outputs is a decision, with an
        ID drawn from which ones and why; `--auto-approve` doesn't cover it."""
        content = json.dumps({"kind": "unpublished", "components": sorted(unpublished)}, sort_keys=True)
        identifier = hashlib.sha256(content.encode()).hexdigest()[:12]
        if identifier in self.options.approvals:
            block.add(
                "Step Plan:",
                Text("going on without their published outputs   approved by --approve", style="green"),
            )
            return
        summary = (
            f"{len(unpublished)} component{'s' if len(unpublished) != 1 else ''} never published "
            "outputs to check against: their 2026.3 steps plan against their live resources, and "
            "each plan is reviewed before it applies"
        )
        block.outcome = "change"
        block.add("Step Plan:", summary)
        block.add("Decision:", identifier)
        if self.session.interactive:
            key = self.screen.ask(block, [("y", "go on"), ("n", "stop the run here")], "Go on without them?")
            if key == "y":
                block.lines = [line for line in block.lines if line.label != "Decision:"]
                block.lines[-1].text.append("   approved", style="green")
                return
        command = f"{self.restart()} --approve {identifier}"
        raise Stop(
            "decision",
            f"{summary}.",
            reason="decision",
            decision={"id": identifier, "summary": summary, "entry": entry.id, "resume": command},
            next=(Next(command, "goes on without their published outputs, as reviewed here"),),
        )

    def migrate(self, entry: Entry, block: Block) -> None:
        checkout = self.checkout(entry.repository)
        before = gitops.head(checkout)
        cli = self.cli_for(entry)
        approvals = list(self.options.approvals)
        while True:
            arguments = ["migrate", "apply", "--json"]
            if self.change:
                arguments += ["--change", self.change]
            arguments += self.approval_arguments(approvals)
            call = self.screen.run_live(block, cli.run, *arguments, on_line=self.messages(block))
            if call.status == "done":
                break
            if call.status == "decision":
                # Every unit's preview, which the engine writes before it asks.
                file = self.record.file("plans", f"{self._position:03d}-{entry.component}-migrate.txt")
                file.write_text("\n".join(call.stderr) + "\n", encoding="utf-8")
                self.decide(entry, block, call, approvals, file)
                continue
            if call.status in WAITS:
                self.wait(entry, block, call)
            self.failure(entry, block, call)
        # What migrate applied: the units its step names, which may have changed only this
        # workstation's files, and their commit. An engine before 0.14.14 names no step, so
        # there only a commit shows it applied something.
        events = call.finished("migrate")
        if events:
            units, commit = list(events[-1].get("units") or []), events[-1].get("commit")
            changed = events[-1].get("status") == "applied"
        else:
            after = gitops.head(checkout)
            units, commit = [], after if after != before else None
            changed = commit is not None
        if not changed:
            self.finish(entry, block, "no change", compact=True)
            return
        file = self.record.file("plans", f"{self._position:03d}-{entry.component}-migrate.txt")
        file.write_text("\n".join(call.stderr) + "\n", encoding="utf-8")
        summary = f"applied {', '.join(units)}" if units else "configuration migrated"
        block.lines = [
            line for line in block.lines if line.label not in ("Step Plan:", "Plan File:", "Decision:")
        ]
        block.add(
            "Step Plan:",
            summary + (f", committed as {commit[:12]}" if commit else ", which changed no tracked file"),
        )
        block.add("Plan File:", self.relative(file))
        self.finish(
            entry,
            block,
            "applied",
            summary=summary,
            plan=self.relative(file),
            units=units,
            commit=commit,
            decisions=self.record.data.get("decisions", {}).get(entry.id, []),
        )

    def step(self, entry: Entry, block: Block) -> None:
        cli = self.cli_for(entry)
        approvals = list(self.options.approvals)
        supplied: dict | None = None
        plan_file = self.record.file("plans", f"{self._position:03d}-{entry.component}-{entry.step}.txt")
        while True:
            plan_file.unlink(missing_ok=True)
            arguments = self.arguments(entry, plan_file, approvals, supplied is not None)
            block.outcome = "applying" if len(approvals) > len(self.options.approvals) else "planning"
            call = self.screen.run_live(
                block, cli.run, *arguments, stdin=supplied, on_line=self.messages(block)
            )
            block.message = ""
            if call.status == "done":
                self.applied(entry, block, call, plan_file, approvals)
                return
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

    def arguments(self, entry: Entry, plan_file: Path, approvals: list[str], supplied: bool) -> list[str]:
        if entry.kind == "upgrade":
            arguments = [
                "upgrade",
                "--step",
                entry.step,
                "--json",
                "--plan-file",
                str(plan_file),
                *self.version,
            ]
            arguments += self.target_arguments(entry.repository)
        else:
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
            if entry.phase == MERGE and entry.owner:
                arguments += ["--run-summary", str(self.summary(entry.repository))]
            if supplied:
                arguments.append("--values-from-stdin")
        if self.change:
            arguments += ["--change", self.change]
        auto = entry.kind == "upgrade" and entry.step in BRANCH_STEPS
        return arguments + self.approval_arguments(approvals, auto=auto)

    def target_arguments(self, repository: str) -> list[str]:
        """The owner's `upgrade` target: on `stable`, the Template Version tagged on the
        manifest's commit; on `canary`, the commit itself."""
        targets = self.record.data.setdefault("targets", {})
        if repository not in targets:
            commit = self.manifest.sources[repository]
            if self.channel == "stable":
                arguments = ["--channel", "stable", "--to", self.template_version(repository, commit)]
            else:
                arguments = ["--channel", "canary", "--to", commit]
            targets[repository] = arguments
            self.record.save()
        arguments = list(targets[repository])
        previous = self.record.data.get("previous_manifest")
        if previous:
            arguments += ["--previous-manifest", str(self.record.folder / previous)]
        return arguments

    def template_version(self, repository: str, commit: str) -> str:
        record = SourceRecord.read(self.checkout(repository))
        url = record.release_url
        name = gitops.repository_name(url)
        found = []
        for ref, sha in gitops.remote_refs(url, "refs/tags/*").items():
            match = re.fullmatch(rf"refs/tags/(?:({re.escape(name)})/)?(1\.0\.\d+)", ref)
            if match and sha == commit:
                found.append((match.group(1) is not None, match.group(2)))
        if not found:
            raise failed(
                f"{name} has no Template Version tag on {commit}, the commit Platform "
                f"{self.manifest.platform_version} names."
            )
        return sorted(found)[-1][1]

    def summary(self, repository: str) -> Path:
        """The run's steps that changed something in this repository, its code's included,
        which the `merge` step writes into the pull request's description; in an upgrade, with
        the title that names its release."""
        steps = []
        for entry in self.record.entries:
            if entry.repository != repository or entry.kind not in ("upgrade", "migrate", "step"):
                continue
            if entry.phase == MERGE:
                continue
            noted = self.record.outcome(entry.id) or {}
            if noted.get("outcome") == "applied":
                steps.append(
                    {
                        "component": entry.component,
                        "phase": entry.phase or entry.kind,
                        "step": entry.step,
                        "summary": noted.get("summary") or "applied",
                        "decisions": noted.get("decisions", []),
                    }
                )
        document = {"title": self.title(), "steps": steps} if self.upgrading else {"steps": steps}
        path = self.record.file("summaries", f"{repository}.json")
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return path

    def title(self) -> str:
        """An upgrade's pull requests' title, so `main`'s history tells a candidate from the
        release: "Platform V", or on canary "Platform V from candidate N", N from the candidate
        manifest's file name. The run's change stays "Platform V", since it names the branch."""
        title = f"Platform {self.manifest.platform_version}"
        if self.channel != "canary":
            return title
        name = Path(self.record.data.get("manifest_origin") or "").name
        match = re.fullmatch(r"candidate-(\d+)\.json", name)
        return f"{title} from {f'candidate {match.group(1)}' if match else name}"

    def decide(
        self, entry: Entry, block: Block, call: Call, approvals: list[str], plan_file: Path | None = None
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
            self.record.save()
        block.outcome = "change"
        block.lines = [
            line for line in block.lines if line.label not in ("Step Plan:", "Plan File:", "Decision:")
        ]
        block.add("Step Plan:", decision.get("summary") or "a change")
        shown_plan = plan_file is not None and plan_file.exists() and plan_file.stat().st_size > 0
        if shown_plan:
            block.add("Plan File:", self.relative(plan_file))
        block.add("Decision:", identifier)
        if self.session.interactive:
            key = self.screen.ask(block, [("y", "apply"), ("n", "stop the run here")], "Apply this step?")
            if key == "y":
                approvals.append(identifier)
                block.outcome = "applying"
                return
            raise Stop(
                "decision",
                f"{entry.where}: not approved; nothing was applied.",
                reason="declined",
                decision={**decision, "entry": entry.id},
                next=(
                    Next("dp-instance resume", f"shows {entry.where}'s plan for review again"),
                    Next("dp-instance apply", "starts a new run on the checked-out code"),
                ),
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
                    "applies exactly the plan that was reviewed, and carries on",
                ),
                Next("dp-instance resume", "at a terminal, shows the plan and asks"),
            ),
        )

    def handoff(self, entry: Entry, block: Block, call: Call, cli: Cli) -> None:
        handoff = (call.result or {}).get("handoff") or {}
        actions = [item for item in handoff.get("actions") or [] if isinstance(item, dict)]
        work = [str(item) for item in handoff.get("work") or []]
        pages = self.pages(actions)
        block.outcome = "waiting: handoff"
        block.lines = []
        for system, count in by_system(actions).items():
            block.add(
                "Waiting on required handoff:",
                f"{handoffs.system_name(system)} ({count}) · handoffs/{pages[system].name}",
            )
        for line in work:
            block.add("Step Handoff:", line)
        detail = {
            "entry": entry.id,
            "actions": [action.get("title") for action in actions] + work,
            "pages": [self.relative(path) for path in pages.values()],
        }
        stop = Stop(
            "handoff",
            f"{entry.where} waits on outside work.",
            reason="handoff",
            handoff=detail,
            next=(
                Next(
                    "dp-instance resume", f"checks {entry.where} again and carries on once the work is done"
                ),
                Next("dp-instance apply", "starts a new run on the checked-out code"),
            ),
        )
        if not self.session.interactive:
            raise stop
        while True:
            key = self.screen.ask(block, [("c", "check again"), ("s", "stop the run here")])
            if key != "c":
                raise stop
            block.outcome = "checking"
            check = self.screen.run_live(
                block, cli.run, "plan", "--phase", entry.phase, "--step", entry.step, "--json", *self.version
            )
            if check.status != "done":
                if check.status in WAITS:
                    self.wait(entry, block, check)
                self.failure(entry, block, check)
            finished = check.finished(entry.step)
            block.lines = [line for line in block.lines if line.label != "Still Waiting:"]
            if finished and finished[-1].get("status") == "pending":
                block.outcome = "waiting: handoff"
                outstanding = [
                    item.get("title", item.get("id"))
                    for item in finished[-1].get("handoffs", [])
                    if item.get("required")
                ]
                block.add("Still Waiting:", "; ".join(outstanding) or "the work isn't observed done yet")
                continue
            block.add("Handoff Done:", "the work is observed done; the step goes on")
            return

    def applied(self, entry: Entry, block: Block, call: Call, plan_file: Path, approvals: list[str]) -> None:
        events = call.finished(entry.step)
        if not events:
            raise failed(f"{entry.command} reported no step {entry.step}; its log has what it ran.")
        event = events[-1]
        self.optional(event)
        status = event.get("status")
        decisions = self.record.data.get("decisions", {}).get(entry.id, [])
        if status != "applied":
            outcome = "no change" if status == "no-change" else str(status)
            self.finish(entry, block, outcome, compact=True)
            return
        how = (
            "approved"
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
        self.keep_pull_requests(entry, block)
        self.finish(
            entry,
            block,
            "applied",
            summary=event.get("summary"),
            plan=self.relative(plan_file) if plan_file.exists() and plan_file.stat().st_size else None,
            decisions=decisions,
        )

    def keep_pull_requests(self, entry: Entry, block: Block) -> None:
        """Keep in the record each pull request a step that applied opened or merged: the run's
        own after the owner's `pull-request` and `merge` steps, and the Adoption PR after a
        phase that follows the merge, in a component that maintains a Managed Repository."""
        listed = self.manifest.repository(entry.repository)
        after_merge = {(item.component, item.phase) for item in listed.after_merge} if listed else set()
        if entry.owner and (entry.kind == "upgrade" and entry.step == "pull-request" or entry.phase == MERGE):
            if not self.change:
                return
            places = [(entry.repository, self.checkout(entry.repository), gitops.run_branch(self.change))]
        elif (entry.component, entry.phase) in after_merge:
            places = [
                (item.name, item.path, github.ADOPTION_BRANCH)
                for item in self.instance.managed
                if item.managed_by == entry.component
            ]
        else:
            return
        for repository, path, branch in places:
            pull = github.pull_request(path, branch, state="all")
            if pull is None:
                continue
            self.record.pull_request(repository, pull)
            merged = self.record.data["pull_requests"][repository]["merge_commit"]
            block.add(
                "Pull Request:",
                f"#{pull['number']} {pull.get('url') or ''}".strip()
                + (f", merged as {merged[:12]}" if merged else ""),
            )

    def supply(self, entry: Entry, block: Block, call: Call) -> dict[str, dict[str, str]]:
        """Show the step that asks, as a block of its own naming the handoff pages that say how
        to get the values, then ask for each value."""
        self.value_pages(block, call)
        block.outcome, block.seconds, block.live = WAITING_FOR["values"], block.elapsed(), False
        self.screen.show(block)
        block.seconds, block.live = None, True
        self.screen.note(
            f"{entry.step} needs values only a person supplies. They go to {entry.command} alone, "
            "and nowhere else."
        )
        values = prompts.supply(self.screen, call.result["values"]["supply"])
        self.screen.blank()
        return values

    def value_pages(self, block: Block, call: Call) -> list[str]:
        """Write the handoff sections a values stop carries, the step's own, which say how to
        get the values, as handoff pages, and name each page on the step's block. A component
        whose engine predates the sections sends none."""
        values = (call.result or {}).get("values") or {}
        sections = [item for item in values.get("sections") or [] if isinstance(item, dict)]
        if not sections:
            return []
        systems = {item.get("system", "other") for item in sections}
        pages = [self.relative(path) for system, path in self.pages(sections).items() if system in systems]
        shown = {line.plain for line in block.lines if line.label == "Handoff Page:"}
        for page in pages:
            if page not in shown:
                block.add("Handoff Page:", page)
        return pages

    def wait(self, entry: Entry, block: Block, call: Call) -> None:
        """A component's stop for values, a sign-in or runs still going stops the run for the
        same, naming what it waits for and the commands that continue."""
        if call.status == "values":
            supply = ((call.result or {}).get("values") or {}).get("supply") or []
            for secret in supply:
                fields = ", ".join(field.get("name", "") for field in secret.get("fields", []))
                about = f": {secret['description']}" if secret.get("description") else ""
                block.add("Values Needed:", f"{secret.get('secret')} ({fields}){about}")
            pages = self.value_pages(block, call)
            raise Stop(
                "values",
                f"{entry.where} needs values only a person supplies.",
                reason="values",
                values={"entry": entry.id, "supply": supply, "pages": pages},
                next=(
                    Next(
                        "dp-instance resume",
                        "run by a person at a terminal: asks for each value with a hidden prompt and "
                        "hands it to the component alone; values never pass through an agent",
                    ),
                ),
            )
        if call.status == "sign-in":
            block.add("Sign-In:", call.message)
            renew = self.sign_in_next(call)
            raise Stop(
                "sign-in",
                call.message,
                reason="sign-in",
                sign_in={"entry": entry.id, "command": renew[0].command},
                next=(*renew, Next("dp-instance resume", f"carries on from {entry.where} once signed in")),
            )
        running = [str(item) for item in ((call.result or {}).get("runs") or {}).get("running") or []]
        for item in running:
            block.add("Still Running:", item)
        raise Stop(
            "runs",
            call.message,
            reason="runs",
            runs={"entry": entry.id, "running": running},
            next=(
                Next("dp-instance resume", f"waits again at {entry.where}, then carries on once they finish"),
            ),
        )

    def sign_in_next(self, call: Call) -> tuple[Next, ...]:
        """The command that renews a component's sign-in, when it stopped for one."""
        if call.status != "sign-in":
            return ()
        command = ((call.result or {}).get("sign_in") or {}).get("command") or "az login"
        return (Next(command, "renews the sign-in, which only you can do"),)

    def excerpt(self, block: Block, call: Call) -> None:
        message = call.message
        for line in call.excerpt(12):
            if line.strip() != message.strip():
                block.add("", Text(line, style="red"))
        block.add("Log:", self.relative(self.record.log_path))

    def failure(self, entry: Entry, block: Block, call: Call) -> None:
        block.add("Failure:", call.message)
        self.excerpt(block, call)
        raise Stop(
            "failed",
            f"{entry.where} failed: {call.message}",
            reason="failed",
            next=(
                Next("dp-instance resume", f"carries on from {entry.where} once the failure is fixed"),
                Next(
                    "dp-instance apply",
                    "starts a new run on the checked-out code; finished steps pass as no changes",
                ),
            ),
        )

    def optional(self, event: dict) -> None:
        """Optional outside work never stops anything: it's recorded whenever its step plans,
        never shown on a stop, and recommended once, at the end of a completed run."""
        seen = self.record.data.setdefault("optional", {})
        for item in event.get("handoffs") or []:
            if isinstance(item, dict) and not item.get("required"):
                seen[item.get("id", item.get("title"))] = item

    def recommend(self) -> None:
        """The optional outside work a completed run recorded, on one line: each system with
        how many pieces it holds, and the folder of their pages, which say what and why."""
        optional = list(self.record.data.get("optional", {}).values())
        if not optional:
            return
        self.pages([])
        systems = ", ".join(
            f"{handoffs.system_name(system)} ({count})" for system, count in by_system(optional).items()
        )
        self.screen.blank()
        self.screen.labelled("Recommended optional handoff:", f"{systems} · handoffs/")

    def pages(self, sections: list[dict]) -> dict[str, Path]:
        if self._asked_by is None:
            self._asked_by = handoffs.asker()
        everything = [*sections, *self.record.data.get("optional", {}).values()]
        return handoffs.write_pages(
            self.record.folder / "handoffs", everything, self.instance.key, self._asked_by
        )

    # Stops ----------------------------------------------------------------------------

    def hold_lines(self) -> list[Next]:
        """A run that stopped between a component's `hold` and `release`, or at `hold` while
        runs are still going, leaves the hold on; its Next block names the command that lifts
        it without carrying on."""
        found = []
        entries = self.record.entries
        on = ("applied", WAITING_FOR["runs"])
        for entry in entries:
            if entry.step != "hold" or (self.record.outcome(entry.id) or {}).get("outcome") not in on:
                continue
            release = next(
                (
                    item
                    for item in entries
                    if (item.repository, item.component, item.step)
                    == (entry.repository, entry.component, "release")
                ),
                None,
            )
            if release is None or (self.record.outcome(release.id) or {}).get("outcome") != "applied":
                found.append(
                    Next(
                        f"uv run --locked {entry.command} apply --step release",
                        f"in {self.checkout(entry.repository)}: lifts {entry.component}'s hold without "
                        "carrying on; dp-instance resume carries on to its release step instead",
                    )
                )
        return found

    def stopped(self, stop: Stop) -> Result:
        entry, block = self._entry, self._block
        reason = stop.detail.get("reason", stop.status)
        if entry is not None and block is not None and block.live:
            outcome = {"failed": "failed", "decision": "stopped", **WAITING_FOR}[stop.status]
            outcome = {
                "interrupted": "interrupted",
                "drift": "drift",
                "already-at-target": "drift",
                "not-published": "not published cleanly",
                "unmapped": "not mapped",
            }.get(reason, outcome)
            self.finish(entry, block, outcome)
        next_steps = stop.next
        if reason == "transient" and not next_steps:
            # A read that failed for a moment inside a run, its retries spent: the run goes on
            # from here once the service answers.
            next_steps = (Next("dp-instance resume", "carries on from here once the service answers"),)
        steps = (*next_steps, *self.hold_lines())
        detail = {key: value for key, value in stop.detail.items() if key != "reason"}
        self.record.stop(
            stop.status,
            reason,
            stop.message,
            entry=entry.id if entry is not None else None,
            next=[item.describe() for item in steps],
            **detail,
        )
        style = {"failed": "bold red", "decision": "bold yellow"}.get(stop.status, "bold orange1")
        self.screen.blank()
        self.screen.note(stop.message, style)
        self.screen.next_block(steps, self.relative(self.record.folder) + "/")
        return Result(
            self.record.kind,
            stop.status,
            stop.message,
            tuple(steps),
            {"run": self.record.id, "reason": reason, **detail},
        )
