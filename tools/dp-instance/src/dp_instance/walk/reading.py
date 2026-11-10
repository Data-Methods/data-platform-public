"""Reading the steps: each component's declaration, from the copy of its repository the run
works in or from another place."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..components import Cli, built
from ..outcome import failed
from ..screen import Block
from ..sequence import Declaration, Plan, declaration
from .base import Base


class Reading(Base):
    def declarations(
        self,
        places: dict[str, Path] | None = None,
        *,
        only: tuple[str, str] | None = None,
        project: dict[str, Path] | None = None,
    ) -> dict[tuple[str, str], Declaration]:
        """Each component's `steps --json`, read in parallel: from the copy of each repository
        the run works in, or from the code in ``places`` for the repositories it holds; with
        ``project``, from another source's CLI, such as the target's. ``only`` reads one
        repository's component alone. A component's `steps` loads its configuration, which it
        refuses at an older schema, so a prepared upgrade reads the target's only once `migrate`
        has brought the configuration to it."""
        places = self.places if places is None else places
        jobs = [
            (repository.key, component)
            for repository in self.repositories
            if repository.key in places
            for component in repository.components
            if only in (None, (repository.key, component.key))
        ]

        def cli_of(job) -> Cli:
            repository, component = job
            return Cli(component.command, places[repository], self.log, (project or {}).get(repository))

        def read(job):
            repository, component = job
            found = declaration(cli_of(job).document("steps", "--json"), component.command)
            if found.key != component.key:
                raise failed(
                    f"{component.command} declares the component {found.key}; the manifest "
                    f"names {component.key}."
                )
            return (repository, component.key), found

        block = Block("sequence", "", "", "", self.footer)
        block.message = f"Reading the steps of {len(jobs)} component{'s' if len(jobs) != 1 else ''}"

        def work():
            built([cli_of(job) for job in jobs])
            return list(pool.map(read, jobs))

        with ThreadPoolExecutor(max_workers=4) as pool:
            return dict(self.screen.run_live(block, work))

    def plan(self, declarations) -> Plan:
        return Plan(self.manifest, self.repositories, declarations)
