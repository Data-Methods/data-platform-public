"""A run: the sequencer walking the steps, one process per step.

A run has up to three parts, each counted and timed on its own. Before anything changes, an
upgrade fetches every repository's target source at once, every component's `preflight` step
runs, all at once, and, in an upgrade run, the drift check. An upgrade's code part then runs
each owner's `upgrade` phase and every `migrate` step. In the platform part, each step is
planned only after the steps before it have applied: a step with no change passes, and at a
step with a change the run stops for review and, on approval, applies exactly that plan, which
the component verifies before the next step starts. While rule C holds in an upgrade, a
Terraform step whose drift plan had no change is named to its component's `apply` with that
plan's inputs hash, and reports no change without running Terraform while the hash still
matches; while its repository is at the commit the drift check planned, a hold names the native
steps it guards whose drift plan had no change too, which its preview doesn't plan. Rule C
holds until a step that writes more than its repositories applies, the run waits on outside
work, or it resumes, and never for a component whose configuration `migrate` changed. At a
terminal a person is asked with single keypresses; without one, or with `--json`, every
question is a stop naming the command that continues. Each pull request a step reports is shown
as it arrives, and its last state kept.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path

from rich.text import Text

from . import configuration, earlier, gitops, handoffs, prompts, targets
from .components import Call, Cli, Log
from .instance import (
    INFRASTRUCTURE,
    INFRASTRUCTURE_COMMAND,
    SETUP,
    SourceRecord,
    merged_commits,
    registrations,
)
from .manifest import Manifest, cli_platform_version
from .outcome import WAITING_FOR, WAITS, Next, Result, Stop, failed
from .record import NOT_STARTED, Record, mark_replaced, under_way
from .screen import Block, duration
from .sequence import MERGE, Declaration, Entry, Plan, declaration
from .session import Session

# A run's parts, in order, each counted and timed on its own.
PARTS = {"before": "before anything changes", "code": "the code", "platform": "the platform"}
# The checks before the run, as a resumed run names those that passed earlier.
CHECKS = {"preflight": "preflights", "drift": "the drift check"}
# 2026.4's engine, the first whose `steps --json` is schema 2 and whose `upgrade` works out
# the channel from its target; an engine before it takes `upgrade --channel`. Goes at the next
# stop.
FIRST_SCHEMA_2_ENGINE = (0, 15)
# The reason a new upgrade stops when it can't read the steps while a run had stopped, which it
# keeps as it was.
KEPT = "stopped-run-kept"
# How many of a decision's plan lines its JSON result carries.
PLAN_HEAD = 40
LEVELS = ("repository", "component", "phase", "step")
DRIFT_TEXT = (
    "Every component's phases planned, read only and in parallel, on the code the platform runs: the "
    "checkout, or its main while an upgrade is under way on its branch; any change is drift. A "
    "repository no run has finished is left out."
)


def part_of(entry: Entry) -> str:
    """The part a run's entry is in: the checks before anything changes, an upgrade's code (each
    owner's `upgrade` phase and every `migrate`), or the platform's steps."""
    if entry.kind in ("preflight", "drift"):
        return "before"
    return "code" if entry.kind in ("upgrade", "migrate") else "platform"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + ", and " + items[-1]


def level_key(entry: Entry, level: str) -> tuple:
    """Which repository part, component or phase the entry is in. A repository has two parts in
    an upgrade: its code, the owner's `upgrade` phase and the `migrate` steps, then its platform."""
    part = (entry.kind in ("upgrade", "migrate"), entry.repository)
    return {
        "repository": (part,),
        "component": (part, entry.component),
        "phase": (part, entry.component, entry.phase),
    }[level]


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
        # One fetch at a time into each repository's own folder, every repository at once.
        self._fetching = {item.key: threading.Lock() for item in self.repositories}
        # The entry under way and its block, which a stop finishes.
        self._entry: Entry | None = None
        self._block: Block | None = None
        # The progress the footer shows for the entry under way; None while reading, which has
        # no count.
        self._progress: str | None = None
        # The part being walked, which ends with a line of its time, and those that have.
        self._part: str | None = None
        self._closed: set[str] = set()
        # Each pull request the entry under way reported, with its last state, by URL.
        self._pulls: dict[str, str | None] = {}

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
        return self.fetch(repository)[0]

    def fetch(self, repository: str) -> tuple[Path, bool]:
        """The owner's source at the target, and whether this fetched it. A fetch that fails
        leaves no folder behind, so the next one starts clean."""
        commit = self.manifest.sources[repository]
        folder = self.instance.own("src", repository)
        with self._fetching[repository]:
            if gitops.is_checkout_root(folder) and gitops.commit_of(folder) == commit:
                return folder, False
            try:
                gitops.remove(folder)
                record = SourceRecord.read(self.checkout(repository))
                url = record.release_url if self.channel == "stable" else record.development_url
                gitops.fetch_commit(folder, url, commit)
            except BaseException as error:
                try:
                    gitops.remove(folder)
                except OSError:
                    pass  # the next fetch removes it before fetching again
                if isinstance(error, Stop):
                    raise Stop(
                        error.status,
                        f"{repository}'s target source couldn't be fetched: {error.message}",
                        next=error.next,
                        **error.detail,
                    ) from None
                if isinstance(error, OSError):
                    raise failed(f"{repository}'s target source couldn't be fetched: {error}") from None
                raise
        return folder, True

    def fetch_sources(self) -> None:
        """Fetch every repository's target source at once, with Git alone, before an upgrade
        runs its preflights and owners' `upgrade` phases from them; every fetch that failed stops
        the run, which `resume` fetches again."""
        block = Block("sources", "", "", "", self.footer)
        block.message = f"Fetching the target source of {len(self.repositories)} repositories at once"
        started = time.monotonic()

        def attempt(repository: str) -> tuple[bool, Stop | None]:
            try:
                return self.fetch(repository)[1], None
            except Stop as stop:
                return False, stop

        keys = [repository.key for repository in self.repositories]
        with ThreadPoolExecutor(max_workers=max(len(keys), 1)) as pool:
            results = self.screen.run_live(block, lambda: list(pool.map(attempt, keys)))
        failures = [stop for _, stop in results if stop is not None]
        if failures:
            said = [stop.message if stop.message.endswith(".") else f"{stop.message}." for stop in failures]
            raise Stop(
                "failed",
                " ".join(said) + " Nothing has changed.",
                reason="transient"
                if any(stop.detail.get("reason") == "transient" for stop in failures)
                else "fetch",
                next=(
                    Next("dp-instance resume", "fetches them again and carries on, once they can be fetched"),
                ),
            )
        fetched = sum(new for new, _ in results)
        if fetched:
            self.timed("fetched", f"{fetched} target source{'s' if fetched != 1 else ''}", started)

    def earlier_runs(self) -> None:
        """As an upgrade starts, the open pull requests and branches each earlier upgrade run
        left in its repositories, and whether that run touched the platform, which then may be
        part-way between releases. They're only named: closing them is the call of the person
        accountable for the instance. A lookup that failed is one line, and the upgrade goes
        on."""
        if not self.change:
            return
        started = time.monotonic()
        block = Block("earlier runs", "", "", "", self.footer)
        block.message = "Looking for earlier upgrade runs' pull requests and branches"
        checkouts = {repository.key: self.checkout(repository.key) for repository in self.repositories}
        found, failures = self.screen.run_live(
            block,
            earlier.find,
            self.instance.state,
            self.record.id,
            gitops.run_branch(self.change),
            checkouts,
        )
        if not found and not failures:
            return
        self.timed("looked for", "earlier upgrade runs' pull requests and branches", started)
        for item in found:
            self.screen.blank()
            said = (
                "it touched the platform, which may be part-way between releases"
                if item.touched_platform
                else "it didn't touch the platform"
            )
            self.screen.labelled("Earlier Run:", f"{item.run}, on {item.branch}: {said}")
            for left in item.left:
                for url, state in left.pull_requests:
                    self.screen.labelled("Pull Request:", f"{url} ({state})")
                self.screen.labelled("Branch:", f"{left.repository}'s {left.branch}, {left.where}")
        if found:
            self.screen.note(
                "dp-instance leaves them as they are: closing them is the call of the person accountable "
                "for the instance."
            )
        if failures:
            more = f" and {len(failures) - 1} more" if len(failures) > 1 else ""
            self.screen.labelled("Not Looked Up:", f"{failures[0].rstrip('.')}{more}; the upgrade goes on")
        described = {"earlier": [item.describe() for item in found], "failures": failures}
        self.record.data["earlier_runs"] = described
        self.record.save()
        self.screen.emit({"event": "earlier_runs", "run": self.record.id, **described})

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
        self, places: dict[str, Path] | None = None, *, only: tuple[str, str] | None = None
    ) -> dict[tuple[str, str], Declaration]:
        """Each component's `steps --json`, read in parallel: from each checkout, or from the
        code in ``places`` for the repositories it holds. ``only`` reads one repository's
        component alone. A component's `steps` loads its configuration, which it refuses at an
        older schema, so an upgrade reads the target's only once `migrate` has brought the
        configuration to it."""
        if places is None:
            places = {repository.key: self.checkout(repository.key) for repository in self.repositories}
        jobs = [
            (repository.key, component)
            for repository in self.repositories
            if repository.key in places
            for component in repository.components
            if only in (None, (repository.key, component.key))
        ]

        def read(job):
            repository, component = job
            cli = Cli(component.command, places[repository], self.log)
            found = declaration(cli.document("steps", "--json"), component.command)
            if found.key != component.key:
                raise failed(
                    f"{component.command} declares the component {found.key}; the manifest "
                    f"names {component.key}."
                )
            return (repository, component.key), found

        block = Block("sequence", "", "", "", self.footer)
        block.message = f"Reading the steps of {len(jobs)} component{'s' if len(jobs) != 1 else ''}"
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
        the platform's steps (an apply), from the declarations the checkouts hold. An upgrade
        runs its preflights, which read no configuration, and each owner's whole `upgrade`
        phase from the target's fetched CLI; the drift check stays on the code the platform
        runs. A repository the upgrade leaves unchanged takes no `upgrade` phase. Its platform
        part is counted from the checkouts' declarations until the upgraded code is read. An
        upgrade reads the steps first, then fetches the target's sources."""
        self._progress = None
        started = time.monotonic()
        declarations = self.read_steps()
        read = f"the steps of {len(declarations)} components"
        if self.upgrading:
            self.timed("read", read, started)
            self.earlier_runs()
            self.fetch_sources()
            self.record.data["unchanged"] = self.find_unchanged()
        plan = self.plan(declarations)
        entries = plan.preflights()
        if self.upgrading:
            self.record.data["platform_estimate"] = plan.estimate()
            # The owners whose target is still at 2026.3's schema 1, which take `upgrade
            # --channel`. Goes at the next stop.
            self.record.data["schema_1_targets"] = [
                repository.key for repository in self.repositories if self.schema_1_target(repository.key)
            ]
            for entry in entries:
                entry.fetched = True
            entries.append(Entry(id="drift-check", kind="drift", description=DRIFT_TEXT))
            code = plan.code()
            for entry in code:
                entry.fetched = entry.kind == "upgrade"
            entries += code
        else:
            entries += plan.platform(upgrade=False)
            self.record.data["platform"] = True
            if self.record.kind == "apply":
                self.record.data["configuration_changes"] = self.configuration_changes(plan.declarations)
                read += " and the instance's published outputs"
            self.timed("read", read, started)
        self.record.entries = entries

    def read_steps(self) -> dict[tuple[str, str], Declaration]:
        """The checkouts' declarations. A new upgrade replaces the run that stopped before it
        only once it has read them: a checkout that run left part way, such as with the target's
        code and configuration its `migrate` hasn't brought to the target's schemas yet, refuses
        its steps, and the upgrade then stops, never started, with that run as it was, for
        `resume`."""
        replaces = self.record.data.get("replaces")
        try:
            declarations = self.declarations()
        except Stop as stop:
            previous = under_way(self.instance.state, replaces) if replaces else None
            if previous is None:
                raise
            said = stop.message if stop.message.endswith(".") else f"{stop.message}."
            raise Stop(
                stop.status,
                f"{said} The upgrade didn't start: run {previous.id}, which stopped, is as it was.",
                reason=KEPT,
                kept=previous.id,
                next=(
                    *stop.next,
                    Next("dp-instance resume", f"carries run {previous.id} on from where it stopped"),
                ),
            ) from None
        if replaces:
            mark_replaced(self.instance.state, replaces)
            del self.record.data["replaces"]
            self.record.save()
        return declarations

    def configuration_changes(self, declarations) -> list[str]:
        """The components whose configuration on `main` isn't the one they last published: what
        a configuration change applies, which titles its pull requests."""
        if self.instance.infrastructure is None:
            return []
        block = Block("sequence", "", "", "", self.footer)
        block.message = "Reading the instance's published outputs"
        places = {repository.key: self.checkout(repository.key) for repository in self.repositories}
        return configuration.unapplied(places, declarations, self.aggregate(block, places))

    def schema_1_target(self, repository: str) -> bool:
        """Whether the target's fetched source bundles an engine from before 2026.4's, read
        from its own `pyproject.toml`, so its `steps --json` is 2026.3's schema 1. A target
        whose engine version doesn't read stops the run, since the upgrade can't tell how to
        call it."""
        engine = self.source(repository) / "tools" / "dp-template-engine" / "pyproject.toml"
        try:
            found = re.search(r'(?m)^version\s*=\s*"(\d+)\.(\d+)\.', engine.read_text(encoding="utf-8"))
        except OSError:
            found = None
        if found is None:
            raise failed(
                f"{repository}'s target bundles no engine version dp-instance reads: {engine} is "
                "missing or names none. Nothing has changed."
            )
        return (int(found.group(1)), int(found.group(2))) < FIRST_SCHEMA_2_ENGINE

    def build_platform(self) -> None:
        """An upgrade's platform steps, read from the code its `upgrade` phases synced, once
        `migrate` brought each configuration to the target's schemas: only then do the target's
        declarations read. What each `migrate` applied is judged by them too, before any
        platform step."""
        self.show_part("platform")
        self._progress = None
        started = time.monotonic()
        declarations = self.declarations()
        plan = self.plan(declarations)
        self.record.entries = self.record.entries + plan.platform(upgrade=True)
        self.record.data["platform"] = True
        self.timed("read", f"the steps of {len(declarations)} components", started)
        self.migrated(declarations)

    # The screen -----------------------------------------------------------------------

    def footer(self) -> str:
        shown = [self._progress] if self._progress else []
        shown += [self.record.data["goal"], f"{duration(time.monotonic() - self.started)} so far"]
        return " · ".join(shown)

    def counted(self) -> dict[str, list[str]]:
        """Each part's entries, by ID, in order."""
        found: dict[str, list[str]] = {}
        for entry in self.record.entries:
            found.setdefault(part_of(entry), []).append(entry.id)
        return found

    def position(self, entry: Entry) -> tuple[str, int, int]:
        """The entry's part, its position in that part and the part's count."""
        members = self.counted().get(part_of(entry), [])
        place = members.index(entry.id) + 1 if entry.id in members else len(members) + 1
        return part_of(entry), place, max(len(members), place)

    def count(self, entry: Entry, last: Entry | None = None) -> None:
        """The footer's progress for the entry under way, or the entries from ``entry`` to
        ``last`` under way at once: its part, its position and count, and the parts ahead, the
        platform's count estimated from the checkouts' declarations until the target's are read,
        once `migrate` has run."""
        part, place, total = self.position(entry)
        if last is not None and last.id != entry.id:
            place = f"{place}-{self.position(last)[1]}"
        counted = self.counted()
        ahead = []
        for later in list(PARTS)[list(PARTS).index(part) + 1 :]:
            if later in counted:
                ahead.append(f"{PARTS[later]}, {len(counted[later])}")
            elif later == "platform" and self.record.data.get("platform_estimate") is not None:
                ahead.append(f"{PARTS[later]}, about {self.record.data['platform_estimate']}")
        self._progress = f"{PARTS[part]} {place} of {total}" + (f" · then {_and(ahead)}" if ahead else "")

    def timed(self, label: str, text: str, started: float) -> None:
        """Show work no entry holds, such as reading the steps, with its time, which counts in
        the part under way."""
        seconds = time.monotonic() - started
        part = self._shown[0] if self._shown else "before"
        reads = self.record.data.setdefault("reads", {})
        reads[part] = round(reads.get(part, 0) + seconds, 1)
        self.record.save()
        self.screen.timed(label, text, seconds)

    def show_part(self, part: str) -> None:
        """Name a part as it starts."""
        if self._shown is None or self._shown[0] != part:
            self.screen.stage(PARTS[part])
            self._shown = (part, None, None, None)

    def close_part(self, part: str | None = None, seconds: float | None = None) -> None:
        """End a part, by default the one being walked, with a line of its count and time: by
        default its entries' times and its reads, which a resumed run's record keeps. A part
        shows once in a command."""
        part = part or self._part
        self._part = None
        if part is None or part in self._closed:
            return
        self._closed.add(part)
        # Its entries that ran: all of them, but in a preview that ends at its first change.
        steps = [item for item in self.counted().get(part, []) if self.record.outcome(item)]
        if seconds is None:
            seconds = self.record.data.get("reads", {}).get(part, 0) + sum(
                (self.record.outcome(item) or {}).get("seconds") or 0 for item in steps
            )
        self.record.data.setdefault("parts", {})[part] = round(seconds, 1)
        self.record.save()
        self.screen.ended(PARTS[part], seconds, steps=len(steps), run=self.record.id)

    def run_seconds(self) -> float:
        """The run's own time: its parts', in every command that walked it."""
        return round(sum(self.record.data.get("parts", {}).values()), 1)

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
        """Name each level as it's entered: the part, then repository, component and phase,
        down to ``until``, the level the caller shows itself."""
        stage = part_of(entry)
        self.show_part(stage)
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
            repository, component, phase = part, None, None
        if depth > 1 and entry.component != component:
            self.screen.blank()
            self.screen.level(
                "component", entry.component_counter + (" (Repository Owner)" if entry.owner else "")
            )
            self.screen.labelled("Component Name:", Text(entry.component, style="bold"))
            self.screen.labelled("Component Desc:", entry.component_description)
            component, phase = entry.component, None
        if depth > 2 and entry.kind in ("upgrade", "migrate"):
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
        if self._shown is None or self._shown[0] != part_of(entry):
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
        if part_of(entry) == "before":
            self.replay_checks()
            return
        for depth, level in enumerate(LEVELS[:3]):
            if level == "phase" and entry.kind in ("upgrade", "migrate"):
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

    def replay_checks(self) -> None:
        """The checks before the run (preflights and the drift check) are its start: those
        that passed earlier take one line and don't run again. Each preflight that didn't pass
        runs, and every check after the preflights."""
        stage = [item for item in self.record.entries if part_of(item) == "before"]
        passed = [item for item in stage if self.record.finished(item.id) and item.id not in self._walked]
        if not passed:
            return
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
        if entry.kind == "migrate":
            return Block("migrate step", "", "migrate", entry.description, self.footer)
        if entry.kind == "upgrade":
            return Block("upgrade step", "", "upgrade", entry.description, self.footer)
        return Block("step", entry.step_counter, entry.step, entry.description, self.footer)

    def finish(
        self,
        entry: Entry,
        block: Block,
        outcome: str,
        *,
        compact: bool = False,
        took: float | None = None,
        **detail,
    ) -> None:
        """Show and record an entry's outcome and time: its block's, or ``took``, the step's own
        time in a process that ran others."""
        block.finish(outcome)
        if took is not None:
            block.seconds = took
        if outcome == "applied":
            self.applied_change(entry, block)
        self.screen.show(block, compact=compact and not block.lines)
        if entry.repository in self.instance.components:
            self.record.data["commits"][entry.repository] = gitops.head(self.checkout(entry.repository))
        self.record.note(entry, outcome, block.elapsed(), details=block.details(), **detail)
        part, place, total = self.position(entry)
        self.screen.emit(
            {
                "event": "step",
                "run": self.record.id,
                "entry": entry.id,
                "kind": entry.kind,
                "part": PARTS[part],
                "position": place,
                "total": total,
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
                self.show_part("before")
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
        self.screen.complete(self.run_seconds())
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
        """Walk the entries not yet walked in this command, in order, each part ending with a
        line of its time."""
        for index, entry in enumerate(self.record.entries, 1):
            if part_of(entry) != self._part:
                self.close_part()
                self._part = part_of(entry)
            if entry.id in self._walked:
                continue
            self._position = index
            if entry.kind == "preflight":
                self.replay_checks()
                self.preflights([item for item in self.record.entries if item.kind == "preflight"])
                continue
            if self.record.finished(entry.id):
                self.replay(index - 1)
                continue
            self.enter(entry)
            self._walked.add(entry.id)
            self.count(entry)
            self._pulls = {}
            block = self.block(entry)
            self._entry, self._block = entry, block
            handler = {
                "drift": self.drift_check,
                "migrate": self.migrate,
                "upgrade": self.step,
                "step": self.step,
            }[entry.kind]
            handler(entry, block)
            self._entry = self._block = None
        self.close_part()

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
        return ["--platform-version", cli_platform_version(self.manifest.platform_version)]

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
        return ["--platform-version", cli_platform_version(version or self.manifest.platform_version)]

    def again(self) -> tuple[Next, ...]:
        """A new run in place of resuming this one, when every repository is on `main`, the only
        code `apply` runs: finished steps pass as no changes."""
        if any(gitops.branch(self.checkout(item.key)) != "main" for item in self.repositories):
            return ()
        return (
            Next("dp-instance apply", "starts a new run on main's code; finished steps pass as no changes"),
        )

    def restart(self) -> str:
        """The command that starts this upgrade again."""
        origin = self.record.data.get("manifest_origin")
        if self.channel == "canary" and origin:
            return f"dp-instance upgrade --manifest {origin}"
        return f"dp-instance upgrade --to {self.manifest.platform_version}"

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
        with self.applied_code(self.repositories, block) as places:
            declarations = self.declarations(places)
            aggregate = None
            if self.absent or self.instance.infrastructure is not None:
                aggregate = self.aggregate(block, places)
            unfinished = self.unfinished(block, aggregate, declarations)
            finished = tuple(item for item in self.repositories if item.key not in unfinished)
            if self.channel == "stable":
                self.check_not_at_target(block, finished)
            checked = {key: value for key, value in declarations.items() if key[0] not in unfinished}
            if aggregate is not None:
                self.check_mapped(block, aggregate)
                self.check_applied(block, places, checked, aggregate)
            self.plan_drift(entry, block, finished, places, checked)

    def unfinished(self, block: Block, aggregate: dict | None, declarations) -> set[str]:
        """The repositories no run has finished. A run finishes a repository at its owner's
        `merge`, which publishes the owner's `merged_commit`: unfinished, the owner's Component
        Outputs hold it uncollected or, listing every component, don't register an owner whose
        `steps --json` says its `merge` publishes it, as before its first publish. That absence
        counts only while the infrastructure owner, which the outputs are read through and every
        instance finishes first, shows its own `merged_commit`: an emptied store registers no
        one. A first run that stopped part way leaves its repository unfinished too. The drift
        check leaves their components out, a line each, and the run plans each of their steps for
        review."""
        if aggregate is None:
            return set()
        merged = merged_commits(aggregate)
        infrastructure = self.manifest.repository(INFRASTRUCTURE)
        whole = (
            (aggregate.get("enumeration") or {}).get("status") == "complete"
            and infrastructure is not None
            and merged.get(infrastructure.owner.key) == "available"
        )
        found = [
            repository
            for repository in self.repositories
            if merged.get(repository.owner.key) == "not_collected"
            or (
                whole
                and repository.owner.key not in merged
                and declarations[(repository.key, repository.owner.key)].publishes_merged_commit
            )
        ]
        for repository in found:
            for component in repository.components:
                block.add(
                    "Left Out:",
                    f"{component.key}: no run has finished {repository.key}; the run plans each of its "
                    "steps for review",
                )
        return {repository.key for repository in found}

    def check_not_at_target(self, block: Block, repositories) -> None:
        """On `stable`, a repository whose `main` already records the target, unless the
        target leaves it unchanged, has an upgrade to it under way."""
        target = self.manifest.platform_version
        already = [
            repository.key
            for repository in repositories
            if repository.key not in self.unchanged
            and self.applied_record(repository.key).commit == self.manifest.sources[repository.key]
        ]
        if not already:
            return
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
                    "starts a new run on main's code once every repository is at the target",
                ),
            ),
        )

    def aggregate(self, block: Block, places: dict[str, Path]) -> dict:
        """The instance's Component Outputs, read by the Client Infrastructure Repository's CLI
        from its applied code; another Platform Instance's are refused."""
        infrastructure = self.instance.infrastructure
        if infrastructure is None:
            raise failed("The repository map holds no Client Infrastructure Repository.", SETUP)
        listed = self.manifest.repository(INFRASTRUCTURE)
        command = listed.owner.command if listed is not None else INFRASTRUCTURE_COMMAND
        cli = Cli(command, places.get(INFRASTRUCTURE, infrastructure.path), self.log)
        found = self.screen.run_live(block, cli.document, "outputs", "aggregate")
        key = (found.get("platform") or {}).get("key")
        if key != self.instance.key:
            raise failed(
                f"The Component Outputs read through {INFRASTRUCTURE} belong to "
                f"{key or 'no Platform Instance'}, and the map names {self.instance.key}; nothing has "
                "changed.",
                SETUP,
            )
        return found

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

    def check_applied(self, block: Block, places: dict[str, Path], declarations, aggregate: dict) -> None:
        """An upgrade never carries a configuration change. A component whose configuration in
        the code the platform runs isn't the one it last published holds a change merged to
        `main` that no run has applied: the upgrade stops before planning, naming those
        components and the configuration change that applies them; real drift keeps its own
        stop. A repository with an upgrade under way is left out: the steps it applied from its
        branch published the branch's configuration."""
        settled = {key: place for key, place in places.items() if not self.in_progress(key)}
        found = configuration.unapplied(settled, declarations, aggregate)
        if not found:
            return
        for name in found:
            block.add("Unapplied:", f"{name}: main's configuration isn't the one it last applied")
        raise Stop(
            "failed",
            f"{', '.join(found)} {'holds' if len(found) == 1 else 'hold'} a configuration change "
            "merged to main that no run has applied; nothing has changed. An upgrade never carries a "
            "configuration change: apply it on its own first.",
            reason="unapplied",
            unapplied=found,
            next=(
                Next(
                    "dp-instance apply",
                    "applies main's configuration, reviewing each change before it applies",
                ),
                Next(self.restart(), "starts the upgrade again once that's applied"),
            ),
        )

    def plan_drift(
        self, entry: Entry, block: Block, repositories, places: dict[str, Path], declarations
    ) -> None:
        """Plan every phase the drift check covers, as each repository's applied code declares
        them, where that code is."""
        phases = Plan(self.manifest, repositories, declarations).drift_targets()
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
        # Each step's plan, by its entry: its status and, for a Terraform step, the hash of its
        # inputs, so a later walk can tell when the drift check already planned exactly what it
        # would plan.
        planned = self.record.data.setdefault("drift_plans", {})
        for repository, component, phase, file, call in results:
            for event in call.finished():
                planned[f"{repository}/{component}/{phase}/{event['step']}"] = {
                    "status": event.get("status"),
                    **({"inputs_hash": event["inputs_hash"]} if event.get("inputs_hash") else {}),
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
        # The commit of the code each repository's phases were planned on: a hold names a
        # native step only while its repository's checkout is still at it.
        self.record.data["drift_commits"] = {key: gitops.head(place) for key, place in places.items()}
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
            call = self.screen.run_live(
                block,
                cli.run,
                *arguments,
                on_line=self.messages(block),
                on_event=self.pull_request_events(block),
            )
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
        reported = events[-1] if events else {}
        if not changed:
            pulls = self.pull_requests(block, reported)
            self.finish(entry, block, "no change", compact=True, pull_requests=pulls)
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
        pulls = self.pull_requests(block, reported)
        self.finish(
            entry,
            block,
            "applied",
            summary=summary,
            plan=self.relative(file),
            units=units,
            commit=commit,
            decisions=self.record.data.get("decisions", {}).get(entry.id, []),
            pull_requests=pulls,
        )

    def step(self, entry: Entry, block: Block) -> None:
        cli = self.cli_for(entry)
        approvals = list(self.options.approvals)
        supplied: dict | None = None
        plan_file = self.record.file("plans", f"{self._position:03d}-{entry.component}-{entry.step}.txt")
        while True:
            # A step's plan is each attempt's own, except an owner's upgrade phase's once a step
            # of it applied: a later attempt adds the plans of the steps still to apply.
            if not any(step["status"] == "applied" for step in self.upgrade_steps(entry)):
                plan_file.unlink(missing_ok=True)
            arguments = self.arguments(entry, plan_file, approvals, supplied is not None)
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
            if entry.kind == "upgrade":
                self.upgrade_steps(entry, call)
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

    # The walk's plan skip ---------------------------------------------------------------

    def skipping(self) -> bool:
        """Whether rule C holds: in an upgrade, since its drift check, nothing but
        repository-only steps has applied, a `migrate` counting as one when every unit it
        applied is a configuration unit, and the run hasn't waited on outside work or resumed."""
        data = self.record.data
        return self.upgrading and "drift_plans" in data and not data.get("plan_skip_off")

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

    def applied_change(self, entry: Entry, block: Block) -> None:
        """An applied step ends rule C unless it writes only its repositories, as its
        declaration says. A `migrate` is judged once the target's declarations, which name
        each unit's kind, read: `migrated`."""
        if entry.kind != "migrate" and not entry.repository_only:
            self.end_plan_skip(block, f"{entry.where} applied a change")

    def migrated(self, declarations: dict[tuple[str, str], Declaration]) -> None:
        """Each `migrate` that applied only configuration units, as its component's target
        declares them, leaves rule C holding, and its component never skips; one that applied
        any other unit ends it."""
        for entry in self.record.entries:
            noted = self.record.outcome(entry.id) or {}
            if entry.kind != "migrate" or noted.get("outcome") != "applied":
                continue
            units = noted.get("units") or []
            declared = declarations.get((entry.repository, entry.component))
            configuration = (
                {unit.name for unit in declared.units if unit.kind == "configuration"} if declared else set()
            )
            if units and set(units) <= configuration:
                migrated = self.record.data.setdefault("configuration_migrated", [])
                component = f"{entry.repository}/{entry.component}"
                if component not in migrated:
                    migrated.append(component)
                    self.record.save()
                continue
            applied = ", ".join(units) or "units it didn't name"
            self.end_plan_skip(None, f"{entry.where} applied {applied}, not only configuration units")

    def planned(self, entry: Entry) -> tuple[list[tuple[str, str]], list[str]]:
        """What a step's apply may skip planning, from the drift check, each step its component
        takes the skip for whose drift plan had no change: the Terraform steps, with each one's
        inputs hash, the step itself and, for a hold, the steps it guards until the step that
        lifts it; and the native steps the hold guards, by name, only while the hold's
        repository is at the commit the drift check planned: a native step has no inputs hash,
        so code that changed since, such as the target's, may plan it differently. A native step
        the run reaches plans as usual. Nothing while rule C doesn't hold, or for a component
        whose configuration `migrate` changed."""
        component = f"{entry.repository}/{entry.component}"
        if not self.skipping() or component in self.record.data.get("configuration_migrated", []):
            return [], []
        entries = self.record.entries
        guarded = []
        if entry.holds_until:
            for item in entries[entries.index(entry) + 1 :]:
                if (item.repository, item.component) != (entry.repository, entry.component):
                    continue
                if item.step == entry.holds_until:
                    break
                guarded.append(item)
        drift = self.record.data["drift_plans"]
        unchanged = [
            item for item in [entry, *guarded] if (drift.get(item.id) or {}).get("status") == "no-change"
        ]
        terraform = [
            (item.step, drift[item.id]["inputs_hash"])
            for item in unchanged
            if item.plan_skip and drift[item.id].get("inputs_hash")
        ]
        native = [item.step for item in unchanged if item is not entry and item.unchanged_native]
        planned_on = self.record.data.get("drift_commits", {}).get(entry.repository)
        if native and planned_on != gitops.head(self.checkout(entry.repository)):
            native = []
        return terraform, list(dict.fromkeys(native))

    # Pieces of a step -----------------------------------------------------------------

    def approval_arguments(self, approvals: list[str], *, auto: bool = False) -> list[str]:
        found = [part for identifier in dict.fromkeys(approvals) for part in ("--approve", identifier)]
        return found + (["--auto-approve"] if auto or self.options.auto_approve else [])

    def arguments(self, entry: Entry, plan_file: Path, approvals: list[str], supplied: bool) -> list[str]:
        if entry.kind == "upgrade":
            # The whole phase: its steps change only the run's branch, which its pull request
            # shows and the `merge` lands, so --auto-approve, which leaves restoring an edited
            # template file or removing a file to a decision.
            arguments = [
                "upgrade",
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
            terraform, native = self.planned(entry)
            # Rule C alone: never --planned or --unchanged without --unchanged-since-planned.
            arguments += [part for step, value in terraform for part in ("--planned", f"{step}={value}")]
            arguments += [part for step in native for part in ("--unchanged", step)]
            if terraform or native:
                arguments.append("--unchanged-since-planned")
        if self.change:
            arguments += ["--change", self.change]
        return arguments + self.approval_arguments(approvals, auto=entry.kind == "upgrade")

    def target_arguments(self, repository: str) -> list[str]:
        """The owner's `upgrade` target: on `stable`, the Template Version tagged on the
        manifest's commit; on `canary`, the commit itself. 2026.4's engine works out the
        channel from the target's form. A target on 2026.3's engine takes the channel too,
        since that engine otherwise takes the channel the repository records and resolves a
        commit only on `canary`; that goes at the next stop."""
        targets = self.record.data.setdefault("targets", {})
        if repository not in targets:
            commit = self.manifest.sources[repository]
            target = self.template_version(repository, commit) if self.channel == "stable" else commit
            arguments = ["--to", target]
            if repository in self.record.data.get("schema_1_targets", []):
                arguments = ["--channel", self.channel, *arguments]
            targets[repository] = arguments
            self.record.save()
        return list(targets[repository])

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
        which the `merge` step writes into the pull request's description, with its title."""
        steps = []
        for entry in self.record.entries:
            if entry.repository != repository or entry.kind not in ("upgrade", "migrate", "step"):
                continue
            if entry.phase == MERGE:
                continue
            noted = self.record.outcome(entry.id) or {}
            if noted.get("outcome") != "applied":
                continue
            # The owner's `upgrade` phase, walked whole, by each of its steps that applied.
            for step in noted.get("steps") or [{"step": entry.step, "summary": noted.get("summary")}]:
                if step.get("status", "applied") != "applied":
                    continue
                steps.append(
                    {
                        "component": entry.component,
                        "phase": entry.phase or entry.kind,
                        "step": step["step"],
                        "summary": step.get("summary") or "applied",
                        "decisions": noted.get("decisions", []),
                    }
                )
        document = {"title": self.title(), "steps": steps}
        path = self.record.file("summaries", f"{repository}.json")
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return path

    def title(self) -> str:
        """The run's pull requests' title. A configuration change's names the components whose
        configuration it applies: "Configuration change: <components>". An upgrade's names its
        release, so `main`'s history tells a candidate from the release: "Platform V", or on
        canary "Platform V from candidate N", N from the candidate manifest's file name. The
        run's change stays as it is, since it names the branch."""
        if not self.upgrading:
            changed = self.record.data.get("configuration_changes") or []
            return "Configuration change" + (f": {', '.join(changed)}" if changed else "")
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
                    *self.again(),
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
        block.lines = [line for line in block.lines if line.label == "Pull Request:"]
        self.end_plan_skip(block, f"{entry.where} waited on outside work")
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
                *self.again(),
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
                block,
                cli.run,
                "plan",
                "--phase",
                entry.phase,
                "--step",
                entry.step,
                "--json",
                *self.version,
                on_event=self.pull_request_events(block),
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
        if entry.kind == "upgrade":
            self.upgraded(entry, block, call, plan_file)
            return
        events = call.finished(entry.step)
        if not events:
            raise failed(f"{entry.command} reported no step {entry.step}; its log has what it ran.")
        event = events[-1]
        self.optional(event)
        status = event.get("status")
        decisions = self.record.data.get("decisions", {}).get(entry.id, [])
        if status != "applied":
            outcome = "no change" if status == "no-change" else str(status)
            pulls = self.pull_requests(block, event)
            skipped = event.get("skip_rule")
            if skipped:
                block.add(
                    "Plan Skip:",
                    f"no change, as the drift check planned it (rule {skipped}); Terraform didn't run",
                )
            self.finish(entry, block, outcome, compact=True, pull_requests=pulls, skip_rule=skipped)
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
        pulls = self.pull_requests(block, event)
        self.finish(
            entry,
            block,
            "applied",
            summary=event.get("summary"),
            plan=self.relative(plan_file) if plan_file.exists() and plan_file.stat().st_size else None,
            decisions=decisions,
            pull_requests=pulls,
        )

    def upgrade_steps(self, entry: Entry, call: Call | None = None) -> list[dict]:
        """An owner's upgrade phase's steps over every attempt at it, a resumed run's earlier
        ones included, each as it last finished, but a step that applied in any attempt stays
        applied: a later attempt finds it done and reports no change. With ``call``, its
        finished steps join them first. None for any other entry."""
        if entry.kind != "upgrade":
            return []
        found = self.record.data.setdefault("upgrade_steps", {}).setdefault(entry.id, {})
        if call is not None:
            for event in call.finished():
                if (found.get(event["step"]) or {}).get("status") != "applied":
                    found[event["step"]] = {"status": event.get("status"), "summary": event.get("summary")}
            self.record.save()
        return [{"step": name, **value} for name, value in found.items()]

    def upgraded(self, entry: Entry, block: Block, call: Call, plan_file: Path) -> None:
        """The owner's `upgrade` phase, walked whole: each of its steps, over every attempt, on
        a line of its own and in the record, and the phase applied when any step did."""
        steps = self.upgrade_steps(entry)
        if not steps:
            raise failed(f"{entry.command} upgrade reported no step; its log has what it ran.")
        for event in call.finished():
            self.optional(event)
        # A decision's lines give way to what each step did.
        block.lines = [
            line for line in block.lines if line.label not in ("Step Plan:", "Plan File:", "Decision:")
        ]
        for step in steps:
            said = (step["summary"] or "applied") if step["status"] == "applied" else "no change"
            block.add("Step Plan:", f"{step['step']}: {said}")
        changed = [step for step in steps if step["status"] == "applied"]
        shown = plan_file.exists() and plan_file.stat().st_size > 0
        if changed and shown:
            block.add("Plan File:", self.relative(plan_file))
        events = call.finished()
        pulls = self.pull_requests(
            block, {"pull_requests": [item for event in events for item in event.get("pull_requests") or []]}
        )
        self.finish(
            entry,
            block,
            "applied" if changed else "no change",
            compact=not changed,
            summary="; ".join(f"{step['step']}: {step['summary'] or 'applied'}" for step in changed) or None,
            plan=self.relative(plan_file) if changed and shown else None,
            steps=steps,
            decisions=self.record.data.get("decisions", {}).get(entry.id, []),
            pull_requests=pulls,
        )

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
        text = f"{url} ({state})" if state else url
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
            line.text = Text(text)
        if at_once:
            if not self.screen.interactive:
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
        """A component's stop for values, a sign-in, runs still going or a step still pending
        stops the run for the same, naming what it waits for and the commands that continue."""
        if call.status in ("pending", "runs"):
            self.end_plan_skip(block, f"{entry.where} waited on outside work")
        if call.status == "pending":
            waits_for = ((call.result or {}).get("pending") or {}).get("message") or call.message
            block.add("Still Waiting:", waits_for)
            raise Stop(
                "pending",
                f"{entry.where} still waits: {waits_for}",
                reason="pending",
                pending={"entry": entry.id, "message": waits_for},
                next=(
                    Next("dp-instance resume", f"checks {entry.where} again and carries on once it's ready"),
                ),
            )
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
                *self.again(),
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
        """A run that stopped after a step that holds, such as Data Engineering's `hold`, or at
        it while runs are still going, before the step its `holds_until` names applied, leaves
        the hold on; its Next block names the command that lifts it without carrying on: that
        step alone, in its phase."""
        found = []
        on = ("applied", WAITING_FOR["runs"])
        for entry in self.record.entries:
            if not entry.holds_until or (self.record.outcome(entry.id) or {}).get("outcome") not in on:
                continue
            lifted = f"{entry.repository}/{entry.component}/{entry.holds_until_phase}/{entry.holds_until}"
            if (self.record.outcome(lifted) or {}).get("outcome") == "applied":
                continue
            found.append(
                Next(
                    f"uv run --locked {entry.command} apply --phase {entry.holds_until_phase} "
                    f"--step {entry.holds_until}",
                    f"in {self.checkout(entry.repository)}: lifts {entry.component}'s hold without "
                    f"carrying on; dp-instance resume carries on to its {entry.holds_until} step instead",
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
                "unapplied": "unapplied",
                "unmapped": "not mapped",
            }.get(reason, outcome)
            # A step that stops has no finished event: the pull requests it reported as they
            # happened are its only report.
            self.finish(entry, block, outcome, pull_requests=self.pull_requests(block))
        next_steps = stop.next
        if reason == "transient" and not next_steps:
            # A read that failed for a moment inside a run, its retries spent: the run goes on
            # from here once the service answers.
            next_steps = (Next("dp-instance resume", "carries on from here once the service answers"),)
        if reason == "sign-in" and not any(item.command == "dp-instance resume" for item in next_steps):
            # A read that waits for a sign-in inside a run: the run goes on from here once signed in.
            next_steps = (*next_steps, Next("dp-instance resume", "carries on from here once signed in"))
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
        if reason == KEPT:
            # It never started: the run it would have replaced stays the active one.
            self.record.data["status"] = NOT_STARTED
            self.record.save()
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
