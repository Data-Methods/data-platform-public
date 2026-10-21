"""The run's sequence: every step in the manifest's order, read from each component's
`steps --json`.

Repositories contain components, components contain phases and phases contain steps.
Before anything changes, every component's `preflight` step runs and, in an upgrade run, the
drift check. An upgrade run then runs each repository owner's `upgrade` phase and every
component's `migrate` step, repository by repository. Then, per repository, each component's
phases, the owner's `merge` phase, and the phases that read the merged commit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .manifest import Manifest, Repository
from .outcome import failed

UPGRADE = "upgrade"
MERGE = "merge"


@dataclass(frozen=True)
class Step:
    name: str
    description: str
    enabled: bool | None


@dataclass(frozen=True)
class Phase:
    name: str
    description: str
    steps: tuple[Step, ...]
    # Whether the phase reads its repository's merged commit, so it runs after the `merge`.
    after_merge: bool = False

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
    upgrade: Phase | None
    migrate: str | None
    phases: tuple[Phase, ...]

    @property
    def owner(self) -> bool:
        return self.upgrade is not None

    def phase(self, name: str) -> Phase | None:
        return next((phase for phase in self.phases if phase.name == name), None)

    @property
    def after_merge(self) -> list[str]:
        """The phases that follow the repository's `merge`, in their declared order."""
        return [phase.name for phase in self.phases if phase.after_merge]


def _phase(data: dict) -> Phase:
    return Phase(
        data["name"],
        data.get("description") or "",
        tuple(
            Step(step["name"], step.get("description") or "", step.get("enabled", True))
            for step in data.get("steps", [])
        ),
        bool(data.get("after_merge", False)),
    )


def declaration(document: dict, command: str) -> Declaration:
    """Read a component's `steps --json`."""
    try:
        if document.get("schema_version") != 1:
            raise ValueError
        component = document["component"]
        preflight = document.get("preflight")
        upgrade = document.get("upgrade")
        migrate = document.get("migrate")
        return Declaration(
            component["key"],
            component.get("command") or command,
            preflight["description"] if preflight else None,
            _phase(upgrade) if upgrade else None,
            migrate["description"] if migrate else None,
            tuple(_phase(phase) for phase in document.get("phases", [])),
        )
    except (KeyError, TypeError, ValueError):
        raise failed(f"{command} steps --json isn't a declaration this dp-instance reads.") from None


@dataclass
class Entry:
    """One place in the run: a component's preflight, the drift check, or a step."""

    id: str
    kind: str  # preflight, drift, published, upgrade, migrate or step
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
    # At the 2026.3 hop, an entry run from the owner's fetched 2026.3 CLI.
    fetched: bool = False

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
        if self.kind in ("drift", "published"):
            return "the drift check" if self.kind == "drift" else "the published outputs check"
        if self.kind == "migrate":
            return f"{self.component}'s migrate step"
        return f"step {self.step_counter} {self.step} in {self.component}'s {self.phase} phase"


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


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

    def _base(self, repository: Repository, component, kind: str, upgrade: bool) -> dict:
        declared = self.declared(repository.key, component.key)
        index = [item.key for item in repository.components].index(component.key)
        phases = self._phase_names(repository, declared, upgrade)
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
                f"Runs as {declared.command}, with {len(phases)} "
                f"phase{'s' if len(phases) != 1 else ''} in this run."
            ),
        }

    def _phase_names(self, repository: Repository, declared: Declaration, upgrade: bool) -> list[str]:
        names = [phase.name for phase in declared.phases]
        if upgrade and declared.owner:
            names.insert(0, UPGRADE)
        return names

    def _phase_entries(self, repository, component, phase: Phase, upgrade: bool, kind="step"):
        declared = self.declared(repository.key, component.key)
        names = self._phase_names(repository, declared, upgrade)
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
                **self._base(repository, component, kind, upgrade),
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
                **self._base(repository, component, "preflight", False),
            )
            for number, (repository, component) in enumerate(found, 1)
        ]

    def code(self) -> list[Entry]:
        """An upgrade run's code: each owner's `upgrade` phase, then every component's
        `migrate` step, repository by repository."""
        entries = []
        for repository in self.repositories:
            owner = repository.owner
            declared = self.declared(repository.key, owner.key)
            if declared.upgrade is None:
                raise failed(f"{owner.key} declares no upgrade phase, so it can't own {repository.key}.")
            entries.extend(self._phase_entries(repository, owner, declared.upgrade, True, "upgrade"))
            for component in repository.components:
                migrate = self.declared(repository.key, component.key).migrate
                if migrate:
                    entries.append(
                        Entry(
                            id=f"{repository.key}/{component.key}/migrate",
                            description=migrate,
                            step="migrate",
                            **self._base(repository, component, "migrate", True),
                        )
                    )
        return entries

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

    def platform(self, upgrade: bool) -> list[Entry]:
        """Per repository: each component's phases, the owner's `merge`, then the phases that
        read the merged commit, in the manifest's order."""
        entries = []
        for repository in self.repositories:
            self.check_after_merge(repository)
            after = {(item.component, item.phase) for item in repository.after_merge}
            for component in repository.components:
                for phase in self.declared(repository.key, component.key).phases:
                    if phase.name == MERGE or (component.key, phase.name) in after:
                        continue
                    entries.extend(self._phase_entries(repository, component, phase, upgrade))
            owner = self.declared(repository.key, repository.owner.key)
            merge = owner.phase(MERGE)
            if merge is not None:
                entries.extend(self._phase_entries(repository, repository.owner, merge, upgrade))
            for item in repository.after_merge:
                component = next(c for c in repository.components if c.key == item.component)
                phase = self.declared(repository.key, item.component).phase(item.phase)
                if phase is None:
                    raise failed(f"{item.component} declares no phase {item.phase}.")
                entries.extend(self._phase_entries(repository, component, phase, upgrade))
        return entries

    def drift_targets(self) -> list[tuple[Repository, str, str]]:
        """What the drift check plans: every component's phases except the owner's
        `upgrade` and `merge` phases and the phases after a merge. The current code's
        declaration and the target's manifest can differ on those, so a phase either names
        stays out."""
        targets = []
        for repository in self.repositories:
            listed = {(item.component, item.phase) for item in repository.after_merge}
            for component in repository.components:
                for phase in self.declared(repository.key, component.key).phases:
                    if (
                        phase.name != MERGE
                        and not phase.after_merge
                        and (component.key, phase.name) not in listed
                    ):
                        targets.append((repository, component.key, phase.name))
        return targets
