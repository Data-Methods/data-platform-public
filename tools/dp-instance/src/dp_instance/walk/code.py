"""A prepare's code part: each owner's upgrade to the target, then every component's `migrate`,
each an ordinary step that commits on the change's branch.

An owner whose code declares the engine's `code` phase moves with its `upgrade` step; one from
before it, as on the jump from the contract floor, moves with its `upgrade` command's `source`
step. Both run from the target's CLI, fetched, against the repository's copy, so the target's
engine records the Platform Version, and take the manifest's commit as it is. Every component's
`migrate` then runs from the code the upgrade left, as the `code` phase's `migrate` step, never
`migrate apply`, which would also apply the units that change state outside the repository:
those wait for `apply`, as `migrate-external`."""

from __future__ import annotations

from ..sequence import CODE, MIGRATE, SOURCE, UPGRADE, Entry
from .base import Base


class Code(Base):
    def code_command(self, entry: Entry) -> list[str]:
        """The command a code step runs, before its common options."""
        if entry.kind == "migrate":
            return ["apply", "--phase", CODE, "--step", MIGRATE]
        target = ["--to", self.manifest.sources[entry.repository]]
        if entry.phase == UPGRADE:
            return ["upgrade", "--step", SOURCE, *target]
        return ["apply", "--phase", CODE, "--step", UPGRADE, *target]
