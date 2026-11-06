"""The run's sequence: every step in the manifest's order, read from each component's
`steps --json`.

Repositories contain components, components contain phases and phases contain steps.
Before anything changes, every component's `preflight` step runs and, in an apply, the drift
check. A prepare's code is the engine's `code` phase, repository by repository: the owner's
`upgrade` to the target, then every component's `migrate`, which run while the configuration is
at the schemas of the code before them, so the platform's steps are read only once they've run.
Then, per repository, each component's phases, then the phases that
read the merged commit.

A declaration says what `dp-instance` may rely on: its `interface` version and its
`capabilities`, which `dp-instance` branches on, never an engine or Platform Version. A
declaration from before them, as at the contract floor, declares neither, and its code has no
`code` phase: it moves to its target through the owner's `upgrade` command, then the target's
`code` phase migrates it. The `code` phase reads no configuration and changes only the
repository, so it's never one of the platform's phases.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .manifest import Manifest, Repository
from .outcome import Next, Stop, failed

UPGRADE = "upgrade"
# The phase code an instance last applied before Platform 2026.6.0 declares. Removed at the next stop.
MERGE = "merge"
# The `steps --json` schemas this dp-instance reads.
SCHEMAS = (2,)
# The engine interfaces this dp-instance reads: the contract's version, which a declaration from
# before it doesn't name.
INTERFACES = (1,)
# The capabilities `dp-instance` branches on, which tell Platform 2026.5.1 from later. `upgrade_steps`: the
# `code` phase's `upgrade` and `migrate` as ordinary steps, which the walk needs of every template.
# `adoption_state`: `repo adoption`.
UPGRADE_STEPS = "upgrade_steps"
ADOPTION_STATE = "adoption_state"
# The engine's phase before every component's own, its steps, and the step of the owner's
# `upgrade` command that moves the template source, which a release before the `code` phase runs.
CODE = "code"
MIGRATE = "migrate"
SOURCE = "source"
UPGRADE_TEXT = "Moves this repository to the target's template source and commits it."
RENDER_TEXT = "Writes the committed files this component's steps keep current; each step's is a commit."
MIGRATE_TEXT = "Runs this component's repository migrate units and commits what they change."


@dataclass(frozen=True)
class Step:
    name: str
    description: str
    enabled: bool | None
    # Whether the step writes only its repositories.
    repository_only: bool = False
    # The later step of the same component its effect lasts until, such as `hold`'s `release`.
    holds_until: str | None = None
    # A Terraform step, `terraform`, or one the component does itself, `native`.
    kind: str = "native"
    # The names of the Component Outputs it publishes.
    outputs: tuple[str, ...] = ()
    # Whether the step changes nothing a Terraform root reads: engine 0.19.0 on.
    affects_no_root: bool = False
    # Each input's source, such as `output:infrastructure/postgres_server_id`: what the step reads.
    inputs: tuple[str, ...] = ()
    # Whether `render` writes the files the step keeps committed.
    renders: bool = False
    # Whether it needn't plan again while its inputs and code are the same: every Terraform
    # step, and a native step that declares it.
    skips: bool = False


@dataclass(frozen=True)
class Phase:
    name: str
    description: str
    steps: tuple[Step, ...]
    # Whether the phase works from the commit people merged, so it follows every other phase of
    # its repository.
    after_merge: bool = False
    # Whether it's the engine's `code` phase, which reads no configuration and changes only the
    # repository.
    before_configuration: bool = False

    @property
    def runs(self) -> tuple[Step, ...]:
        """The steps the configuration doesn't switch off."""
        return tuple(step for step in self.steps if step.enabled is not False)


@dataclass(frozen=True)
class Declaration:
    """One component's `steps --json`."""

    key: str
    command: str
    preflight: str | None
    phases: tuple[Phase, ...]
    # The engine interface it declares, and its capabilities; None and none before them.
    interface: int | None = None
    capabilities: frozenset[str] = frozenset()
    # Whether it maintains a Managed Repository.
    manages: bool = False

    def declares(self, capability: str) -> bool:
        return capability in self.capabilities

    @property
    def platform_phases(self) -> tuple[Phase, ...]:
        """Its phases that act on the platform: every one but the engine's `code` phase."""
        return tuple(phase for phase in self.phases if not phase.before_configuration)

    def phase(self, name: str) -> Phase | None:
        return next((phase for phase in self.phases if phase.name == name), None)

    def phase_of(self, step: str) -> str:
        """The phase that declares ``step``, whether or not the configuration switches it off."""
        return next(
            (phase.name for phase in self.phases if any(item.name == step for item in phase.steps)), ""
        )

    @property
    def after_merge(self) -> list[str]:
        """The phases that follow the repository's other phases, in their declared order."""
        return [phase.name for phase in self.phases if phase.after_merge]


def _phase(data: dict) -> Phase:
    return Phase(
        data["name"],
        data.get("description") or "",
        tuple(
            Step(
                step["name"],
                step.get("description") or "",
                step.get("enabled", True),
                bool(step.get("repository_only", False)),
                step.get("holds_until"),
                step.get("kind") or "native",
                tuple(step.get("outputs") or ()),
                bool(step.get("affects_no_root", False)),
                tuple(item["source"] for item in step.get("inputs") or ()),
                bool(step.get("renders", False)),
                bool(step.get("skips", (step.get("kind") or "native") == "terraform")),
            )
            for step in data.get("steps", [])
        ),
        bool(data.get("after_merge", False)),
        bool(data.get("before_configuration", False)),
    )


def declaration(document: dict, command: str) -> Declaration:
    """Read a component's `steps --json`. One that names an interface this dp-instance doesn't
    read names the dp-instance that does."""
    interface = document.get("interface") if isinstance(document, dict) else None
    if isinstance(interface, int) and interface not in INTERFACES:
        raise Stop(
            "failed",
            f"{command} declares engine interface {interface}, which this dp-instance doesn't read.",
            reason="interface",
            next=(Next("dp-instance self update", "installs the newest dp-instance, which reads it"),),
        )
    try:
        if document.get("schema_version") not in SCHEMAS:
            raise ValueError
        if interface is not None and not isinstance(interface, int):
            raise ValueError
        capabilities = document.get("capabilities") or []
        if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
            raise ValueError
        component = document["component"]
        preflight = document.get("preflight")
        return Declaration(
            component["key"],
            component.get("command") or command,
            preflight["description"] if preflight else None,
            tuple(_phase(phase) for phase in document.get("phases", [])),
            interface,
            frozenset(capabilities),
            bool(document.get("managed_repositories")),
        )
    except (KeyError, TypeError, ValueError):
        raise failed(f"{command} steps --json isn't a declaration this dp-instance reads.") from None


@dataclass
class Entry:
    """One place in the run: a component's preflight, the drift check, or a step."""

    id: str
    # preflight, drift, branch, upgrade, migrate, preview, pull-request, step or mark
    kind: str
    description: str = ""
    repository: str = ""
    component: str = ""
    command: str = ""
    phase: str = ""
    phase_description: str = ""
    step: str = ""
    owner: bool = False
    repository_counter: str = ""
    component_counter: str = ""
    phase_counter: str = ""
    step_counter: str = ""
    preflight_counter: str = ""
    repository_description: str = ""
    component_description: str = ""
    # An entry run from the owner's CLI fetched at the target: an upgrade's preflights and its
    # owners' `upgrade` steps.
    fetched: bool = False
    # The later step of the component that lifts this step's hold, and its phase; empty for a
    # step that holds nothing.
    holds_until: str = ""
    holds_until_phase: str = ""
    # Whether the step writes only its repositories, as its declaration says.
    repository_only: bool = False
    # Whether the step changes nothing a Terraform root reads, as its declaration says.
    affects_no_root: bool = False
    # Whether `render` writes the files the step keeps committed, as its declaration says.
    renders: bool = False
    # A Terraform step whose component's `apply` takes the walk's plan skip.
    plan_skip: bool = False
    # A Terraform step, whose real change reaches only the steps that read from it.
    terraform: bool = False

    def describe(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, data: dict) -> Entry:
        return cls(**data)

    @property
    def where(self) -> str:
        """Where the entry is, in the vocabulary the screen teaches."""
        if self.kind == "preflight":
            return f"preflight {self.preflight_counter} ({self.component})"
        if self.kind == "drift":
            return "the drift check"
        if self.kind == "render":
            return f"{self.component}'s render"
        if self.kind == "pull-request":
            return f"{self.repository}'s pull request"
        if self.kind == "migrate":
            return f"{self.component}'s migrate step"
        if self.kind == "upgrade":
            return f"{self.component}'s upgrade step"
        if self.kind == "mark":
            return f"{self.component}'s applied commit"
        return f"step {self.step_counter} {self.step} in {self.component}'s {self.phase} phase"


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def parse_step(name: str) -> tuple[str, str] | None:
    """The repository and component a step's full name, `repository/component/phase/step`,
    names; None when it isn't one."""
    parts = name.split("/")
    return (parts[0], parts[1]) if len(parts) == 4 and all(parts) else None


@dataclass(frozen=True)
class Plan:
    """The repositories a run covers, in the manifest's order, and their declarations."""

    manifest: Manifest
    repositories: tuple[Repository, ...]
    declarations: dict[tuple[str, str], Declaration]

    def declared(self, repository: str, component: str) -> Declaration:
        return self.declarations[(repository, component)]

    def _repository_counter(self, repository: Repository) -> str:
        return f"{self.repositories.index(repository) + 1}/{len(self.repositories)}"

    def _repository_text(self, repository: Repository) -> str:
        names = [component.key for component in repository.components]
        return f"Holds {_join(names)}; {repository.owner.key} is its Repository Owner."

    def _base(self, repository: Repository, component, kind: str) -> dict:
        declared = self.declared(repository.key, component.key)
        index = [item.key for item in repository.components].index(component.key)
        phases = len(declared.platform_phases)
        return {
            "kind": kind,
            "repository": repository.key,
            "component": component.key,
            "command": declared.command,
            "owner": index == 0,
            "repository_counter": self._repository_counter(repository),
            "component_counter": f"{index + 1}/{len(repository.components)}",
            "repository_description": self._repository_text(repository),
            "component_description": (
                f"Runs as {declared.command}, with {phases} phase{'s' if phases != 1 else ''} in this run."
            ),
        }

    def _phase_entries(self, repository, component, phase: Phase, kind: str = "step"):
        declared = self.declared(repository.key, component.key)
        names = [item.name for item in declared.platform_phases]
        steps = phase.runs
        for number, step in enumerate(steps, 1):
            yield Entry(
                id=f"{repository.key}/{component.key}/{phase.name}/{step.name}",
                description=step.description,
                phase=phase.name,
                phase_description=phase.description,
                step=step.name,
                phase_counter=f"{names.index(phase.name) + 1}/{len(names)}",
                step_counter=f"{number}/{len(steps)}",
                holds_until=step.holds_until or "",
                holds_until_phase=declared.phase_of(step.holds_until) if step.holds_until else "",
                repository_only=step.repository_only,
                affects_no_root=step.affects_no_root,
                renders=step.renders,
                plan_skip=step.skips,
                terraform=step.kind == "terraform",
                **self._base(repository, component, kind),
            )

    def preflights(self) -> list[Entry]:
        found = [
            (repository, component)
            for repository in self.repositories
            for component in repository.components
            if self.declared(repository.key, component.key).preflight
        ]
        return [
            Entry(
                id=f"{repository.key}/{component.key}/preflight",
                description=self.declared(repository.key, component.key).preflight,
                preflight_counter=f"{number}/{len(found)}",
                **self._base(repository, component, "preflight"),
            )
            for number, (repository, component) in enumerate(found, 1)
        ]

    def code(self, upgrades: dict[str, bool]) -> list[Entry]:
        """A prepare's code, repository by repository: for each repository ``upgrades`` names,
        its owner's upgrade to the target, the `code` phase's `upgrade` step, or, with True, as
        for code from before the `code` phase, the `upgrade` command's `source` step; then every
        component's `migrate`, which runs from the code the upgrade leaves."""
        entries = []
        for repository in self.repositories:
            owner = repository.owner
            if repository.key in upgrades:
                before = upgrades[repository.key]
                phase, step = (UPGRADE, SOURCE) if before else (CODE, UPGRADE)
                entries.append(
                    Entry(
                        id=f"{repository.key}/{owner.key}/{phase}/{step}",
                        description=UPGRADE_TEXT,
                        phase=phase,
                        step=step,
                        repository_only=True,
                        fetched=True,
                        **self._base(repository, owner, "upgrade"),
                    )
                )
            for component in repository.components:
                entries.append(
                    Entry(
                        id=f"{repository.key}/{component.key}/{CODE}/{MIGRATE}",
                        description=MIGRATE_TEXT,
                        phase=CODE,
                        step=MIGRATE,
                        repository_only=True,
                        **self._base(repository, component, "migrate"),
                    )
                )
        return entries

    def renders(self) -> list[Entry]:
        """A prepare's render, after its code: every component that declares `render` writes the
        files its steps keep current, repository by repository."""
        return [
            Entry(
                id=f"{repository.key}/{component.key}/render",
                description=RENDER_TEXT,
                **self._base(repository, component, "render"),
            )
            for repository in self.repositories
            for component in repository.components
        ]

    def check_after_merge(self, repository: Repository) -> None:
        """The manifest's `after_merge`, which the Command Center computes from each phase's
        `after_merge` in `steps --json`, must say what the components declare."""
        for component in repository.components:
            listed = [item.phase for item in repository.after_merge if item.component == component.key]
            declared = self.declared(repository.key, component.key).after_merge
            if listed != declared:
                raise failed(
                    f"The manifest and {component.key} disagree on the phases that follow "
                    f"{repository.key}'s merge: the manifest names {', '.join(listed) or 'none'}, "
                    f"and {component.key} declares {', '.join(declared) or 'none'}."
                )

    def platform(self, kind: str = "step") -> list[Entry]:
        """Per repository: each component's phases, then the phases that work from the merged
        commit, in the manifest's order."""
        entries = []
        for repository in self.repositories:
            self.check_after_merge(repository)
            after = {(item.component, item.phase) for item in repository.after_merge}
            for component in repository.components:
                for phase in self.declared(repository.key, component.key).platform_phases:
                    if (component.key, phase.name) in after:
                        continue
                    entries.extend(self._phase_entries(repository, component, phase, kind))
            for item in repository.after_merge:
                component = next(c for c in repository.components if c.key == item.component)
                phase = self.declared(repository.key, item.component).phase(item.phase)
                if phase is None:
                    raise failed(f"{item.component} declares no phase {item.phase}.")
                entries.extend(self._phase_entries(repository, component, phase, kind))
        return entries

    def step(self, name: str, kind: str = "step") -> Entry | None:
        """The platform step a full name, `repository/component/phase/step`, names, as a run's
        entries name it; None when it names no step the configuration runs."""
        parts = name.split("/")
        if len(parts) != 4:
            return None
        repository_key, component_key, phase_name, step_name = parts
        repository = next((item for item in self.repositories if item.key == repository_key), None)
        if repository is None or (repository_key, component_key) not in self.declarations:
            return None
        component = next(item for item in repository.components if item.key == component_key)
        phase = self.declared(repository_key, component_key).phase(phase_name)
        if phase is None or phase.before_configuration:
            return None
        return next(
            (
                entry
                for entry in self._phase_entries(repository, component, phase, kind)
                if entry.step == step_name
            ),
            None,
        )


def drift_phases(repository: Repository, component: str, declared: Declaration) -> list[str]:
    """A component's phases the drift check plans: each platform phase but the phases after a
    merge, and the `merge` phase of the code an instance last applied before Platform 2026.6.0."""
    listed = {item.phase for item in repository.after_merge if item.component == component}
    return [
        phase.name
        for phase in declared.platform_phases
        if phase.name != MERGE and not phase.after_merge and phase.name not in listed
    ]
