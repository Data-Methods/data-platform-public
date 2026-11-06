"""The graph of declared inputs: which steps read which, from `steps --json`."""

from __future__ import annotations

from ..sequence import Declaration, Entry

# A step that works from the commit its repository's merge lands, as `reads` names it.
MERGED_COMMIT = "merged_commit"


def producers(entries: list[Entry], declarations: dict[tuple[str, str], Declaration]) -> dict[str, str]:
    """Each output, `<component>/<name>`, and the step that publishes it, by entry."""
    found: dict[str, str] = {}
    for entry in entries:
        declared = declarations[(entry.repository, entry.component)]
        for phase in declared.phases:
            for step in phase.steps:
                for name in step.outputs:
                    found.setdefault(
                        f"{entry.component}/{name}",
                        f"{entry.repository}/{entry.component}/{phase.name}/{step.name}",
                    )
    return found


def reads(entries: list[Entry], declarations: dict[tuple[str, str], Declaration]) -> dict[str, list[str]]:
    """The graph of declared inputs: for each step, the steps whose outputs it reads, by entry,
    and `merged_commit` for one in a phase that works from the commit its repository's merge lands."""
    published = producers(entries, declarations)
    found: dict[str, list[str]] = {}
    for entry in entries:
        declared = declarations[(entry.repository, entry.component)]
        step = next(
            (item for phase in declared.phases for item in phase.steps if item.name == entry.step), None
        )
        if entry.phase in declared.after_merge:
            found.setdefault(entry.id, []).append(MERGED_COMMIT)
        for source in step.inputs if step is not None else ():
            kind, _, path = source.partition(":")
            if kind != "output":
                continue
            component, _, name = path.rpartition("/")
            component = component or entry.component
            if f"{component}/{name}" in published:
                found.setdefault(entry.id, []).append(published[f"{component}/{name}"])
    return found


def downstream(graph: dict[str, list[str]], start: str) -> set[str]:
    """Every step that reads from ``start``, directly or through the steps that do."""
    readers: dict[str, list[str]] = {}
    for reader, sources in graph.items():
        for source in sources:
            readers.setdefault(source, []).append(reader)
    found: set[str] = set()
    waiting = [start]
    while waiting:
        for reader in readers.get(waiting.pop(), ()):
            if reader not in found:
                found.add(reader)
                waiting.append(reader)
    return found
