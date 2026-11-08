"""Each Managed Repository's Adoption PR as it is now, from the component that maintains it.

A component that declares the `adoption_state` capability and a Managed Repository publishes
that repository's adoption state, and answers `repo adoption --json`, which asks GitHub alone
and writes nothing. Its people merge and release an Adoption PR on their own timing, outside any
run, so its state is read now: each component's declaration, then each such component's `repo
adoption`, every read at once. Nothing here names a component: the declarations say which
maintain one, and the release's manifest which command each runs as. Each Adoption PR is named
as a run names one it leaves open: its Managed Repository, as the repository map names it, the
component that maintains it, its URL and its state.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import gitops
from .components import Cli, Log, built
from .instance import Instance
from .manifest import Manifest
from .outcome import Next, Stop, failed
from .sequence import ADOPTION_STATE, declaration

# The `repo adoption --json` schemas this dp-instance reads.
SCHEMAS = (1,)
# An Adoption PR still to merge and release, or to release once it's merged.
OUTSTANDING = ("open", "merged")


def _maintainers(instance: Instance, manifest: Manifest, places: dict[str, Path] | None) -> list[Cli]:
    """Each mapped component that declares it publishes a Managed Repository's adoption state,
    by its CLI in its Component Repository's checkout, or the copy ``places`` names, from its
    declaration there."""
    clis = [
        Cli(
            component.command,
            (places or {}).get(repository.key, instance.components[repository.key].path),
            Log(None),
        )
        for repository in manifest.order
        if repository.key in instance.components
        for component in repository.components
    ]

    def declares(cli: Cli) -> bool:
        found = declaration(cli.document("steps", "--json"), cli.command)
        return found.declares(ADOPTION_STATE) and found.manages

    built(clis)
    with ThreadPoolExecutor(max_workers=max(len(clis), 1)) as pool:
        return [cli for cli, maintains in zip(clis, pool.map(declares, clis), strict=True) if maintains]


def _document(found: dict, command: str) -> list:
    schema = found.get("schema_version")
    if isinstance(schema, int) and schema not in SCHEMAS:
        raise Stop(
            "failed",
            f"{command} repo adoption is at schema version {schema}, which this dp-instance doesn't read.",
            next=(Next("dp-instance self update", "installs the newest dp-instance, which reads it"),),
        )
    items = found.get("managed_repositories")
    if schema not in SCHEMAS or not isinstance(items, list):
        raise failed(f"{command} repo adoption --json printed no document this dp-instance reads.")
    return items


def read(instance: Instance, manifest: Manifest, places: dict[str, Path] | None = None) -> list[dict] | None:
    """Every Managed Repository's latest Adoption PR, read now by the component that maintains
    it, from its Component Repository's checkout or the copy ``places`` names; None when no
    component declares that it publishes one, as before the capability. A read that fails
    stops, naming why."""
    maintainers = _maintainers(instance, manifest, places)
    if not maintainers:
        return None

    def named(url: str) -> str:
        mapped = next((item for item in instance.managed if gitops.same_repository(item.remote, url)), None)
        return mapped.name if mapped is not None else gitops.repository_name(url)

    def one(cli: Cli) -> list[dict]:
        document = cli.document("repo", "adoption", "--json")
        found = []
        for item in _document(document, cli.command):
            try:
                pull = item["pull_request"]
                if pull is not None:
                    found.append(
                        {
                            "repository": named(item["url"]),
                            "managed_by": document["component"],
                            "url": pull["url"],
                            "state": pull["state"],
                        }
                    )
            except (KeyError, TypeError):
                raise failed(
                    f"{cli.command} repo adoption --json printed no document this dp-instance reads."
                ) from None
        return found

    with ThreadPoolExecutor(max_workers=len(maintainers)) as pool:
        return [item for found in pool.map(one, maintainers) for item in found]


def outstanding(found: list[dict]) -> list[dict]:
    """The Adoption PRs still to merge and release, or to release."""
    return [item for item in found if item.get("state") in OUTSTANDING]
