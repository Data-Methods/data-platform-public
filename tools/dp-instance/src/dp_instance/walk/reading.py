"""Reading the steps: each component's declaration, from the copy of its repository the run
works in or from another place."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import installed
from ..components import Cli
from ..outcome import Stop, failed
from ..screen import Block
from ..sequence import UPGRADE_STEPS, Declaration, Plan, declaration
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

        def read(job):
            repository, component = job
            cli = Cli(component.command, places[repository], self.log, (project or {}).get(repository))
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
        return Plan(self.manifest, self.repositories, declarations)


def walks(declarations: dict[tuple[str, str], Declaration]) -> bool:
    """Whether every template declares the walk of prepare then apply: the `code` phase's
    `upgrade` and `migrate` as ordinary steps."""
    return bool(declarations) and all(item.declares(UPGRADE_STEPS) for item in declarations.values())


def refuse_earlier_release(what: str) -> Stop:
    """A release whose templates walk only as dp-instance 3.0.0 does: only that dp-instance walks
    it, except the jump to the next, which a prepared upgrade takes."""
    from ..targets import BELOW_FLOOR, below_floor

    return Stop(
        "failed",
        f"{what} declare no `code` phase, so {installed.shown(installed.version())} only prepares the "
        f"upgrade to the next Platform Version from them; dp-instance {BELOW_FLOOR} walks the rest. "
        "Nothing has changed." + installed.before_self_update(BELOW_FLOOR),
        reason="earlier-release",
        next=(below_floor(""),),
    )
