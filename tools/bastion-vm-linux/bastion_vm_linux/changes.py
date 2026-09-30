from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Change:
    action: str
    target: str
    reason: str
    details: tuple[str, ...] = ()


def render_plan(
    component: str,
    action: str,
    changes: list[Change],
    *,
    dry_run: bool,
    blockers: list[str] | None = None,
) -> None:
    print(f"Component: {component}")
    print(f"Action: {action}")
    if dry_run:
        print("Dry run: yes")
    blocked = blockers or []
    if blocked:
        print("Result: blocked")
        for blocker in blocked:
            print(f"Blocked: {blocker}")
        if changes:
            _render_changes(changes)
        return
    if not changes:
        print("Result: no changes")
        return
    print("Result: changes required")
    _render_changes(changes)


def _render_changes(changes: list[Change]) -> None:
    print(f"Changes: {len(changes)}")
    for change in changes:
        print(f"- {change.action}: {change.target}")
        print(f"  Why: {change.reason}")
        for detail in change.details:
            print(f"  {detail}")


def confirm(component: str, *, yes: bool) -> bool:
    if yes:
        return True
    answer = input(f"Apply {component} changes? [y/N] ").strip().lower()
    return answer == "y"
