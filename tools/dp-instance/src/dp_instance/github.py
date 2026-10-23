"""Reading pull requests through the GitHub CLI, signed in as the operator. Read only: the
repository owners' `upgrade` and `merge` phases open, describe and merge pull requests."""

from __future__ import annotations

import json
from pathlib import Path

from . import processes
from .outcome import Stop

# The one branch a Managed Repository's Adoption PR comes from.
ADOPTION_BRANCH = "platform-adoption"


def _json(path: Path, *arguments: str):
    try:
        found = processes.run("gh", *arguments, cwd=path, check=False)
    except Stop:
        return None
    if not found.ok:
        return None
    try:
        return json.loads(found.stdout)
    except ValueError:
        return None


def pull_request(path: Path, branch: str, *, state: str = "open") -> dict | None:
    """The newest pull request from ``branch`` in the checkout's repository, or None."""
    found = _json(
        path,
        "pr",
        "list",
        "--head",
        branch,
        "--state",
        state,
        "--limit",
        "1",
        "--json",
        "number,state,isDraft,url,mergeCommit,reviewRequests,statusCheckRollup",
    )
    return found[0] if isinstance(found, list) and found else None


def describe(pull: dict) -> str:
    state = "draft" if pull.get("isDraft") else str(pull.get("state", "")).lower()
    return f"#{pull['number']} {state}"


def checks(pull: dict) -> str:
    rollup = pull.get("statusCheckRollup") or []
    if not rollup:
        return "no checks reported"
    states = [
        str(item.get("conclusion") or item.get("state") or item.get("status") or "").lower()
        for item in rollup
    ]
    failing = sum(
        state in ("failure", "error", "cancelled", "timed_out", "action_required") for state in states
    )
    passing = sum(state in ("success", "neutral", "skipped") for state in states)
    pending = len(states) - failing - passing
    parts = [f"{passing} passed"]
    if failing:
        parts.append(f"{failing} failing")
    if pending:
        parts.append(f"{pending} pending")
    return ", ".join(parts)


def reviewers(pull: dict) -> list[str]:
    names = []
    for item in pull.get("reviewRequests") or []:
        name = item.get("login") or item.get("slug") or item.get("name")
        if name:
            names.append(name)
    return names
