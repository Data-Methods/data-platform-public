"""What earlier upgrade runs left open: their pull requests and branches.

An upgrade run works on the branch its change names, such as `platform-2026.3`, in each
repository it upgrades, and opens a draft pull request for it; its `merge` readies the pull
request, lands it and deletes the branch. A run that stopped, followed by an upgrade to another
target, leaves them open. When an upgrade starts, the branches of the upgrade runs since the
last one that completed, other than the new run's own, are looked for in its repositories: in
the checkout, on origin and, once origin has the branch, as an open pull request, draft or
ready, through the GitHub CLI, a few lookups at a time. A run that completed landed its pull
requests and deleted its branches, and those of the runs before it were named as it started. A
branch a checkout is on is the one a new run carries on, as the components work on the
checkout's branch. dp-instance only names them, with whether the run touched the platform: a
platform step of it applied, failed or was interrupted, or a hold of it waits on runs, which
leaves the hold on. Closing them is the person's own call, on GitHub. A lookup that fails, such
as the GitHub CLI not signed in or answering with something it doesn't read, leaves what it
would have found out.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import takewhile
from pathlib import Path

from . import gitops, processes, record
from .outcome import WAITING_FOR, Stop, failed
from .record import Record

# How many lookups run at once, each a Git or GitHub CLI call.
LOOKUPS_AT_ONCE = 6
# The outcomes of a platform step that touched the platform: not a stop for review or a step
# with no change, which changed nothing. A step stopped for a sign-in may have written before
# it lost the sign-in, so it counts.
TOUCHED = ("applied", "failed", "interrupted", WAITING_FOR["runs"], WAITING_FOR["sign-in"])


@dataclass(frozen=True)
class Left:
    """One repository's branch an earlier upgrade run left, and its open pull requests, each URL
    with its state, `draft` or `open`."""

    repository: str
    branch: str
    local: bool
    on_origin: bool
    pull_requests: tuple[tuple[str, str], ...]

    @property
    def where(self) -> str:
        if self.local and self.on_origin:
            return "in the checkout and on origin"
        return "in the checkout" if self.local else "on origin"

    def describe(self) -> dict:
        return {
            "repository": self.repository,
            "branch": self.branch,
            "local": self.local,
            "on_origin": self.on_origin,
            "pull_requests": [{"url": url, "state": state} for url, state in self.pull_requests],
        }


@dataclass(frozen=True)
class Earlier:
    """An earlier upgrade run's branch: its newest run, whether a run on it touched the
    platform, and what it left in each repository."""

    run: str
    branch: str
    touched_platform: bool
    left: tuple[Left, ...]

    def describe(self) -> dict:
        return {
            "run": self.run,
            "branch": self.branch,
            "touched_platform": self.touched_platform,
            "left": [item.describe() for item in self.left],
        }


def touched_platform(run: Record) -> bool:
    """Whether a platform step of the run applied, failed or was interrupted, or a hold of it
    waits on runs."""
    return any(
        entry.kind == "step" and (run.outcome(entry.id) or {}).get("outcome") in TOUCHED
        for entry in run.entries
    )


def open_pull_requests(checkout: Path, branch: str) -> tuple[tuple[str, str], ...]:
    """The branch's open pull requests, each URL with its state, `draft` or `open`, as the
    GitHub CLI finds them from the checkout."""
    found = processes.run(
        "gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url,isDraft", cwd=checkout
    )
    try:
        return tuple(
            (item["url"], "draft" if item["isDraft"] else "open") for item in json.loads(found.stdout or "[]")
        )
    except (ValueError, KeyError, TypeError):
        raise failed(f"gh pr list answered with no list of pull requests for {branch}.") from None


def _left(repository: str, checkout: Path, branch: str) -> tuple[Left | None, str | None]:
    """What the branch left in the repository, if anything, and why a lookup failed, if one
    did: then what the lookups before it found."""
    local, on_origin, pulls, error = False, False, (), None
    try:
        local = gitops.has_branch(checkout, branch)
        on_origin = gitops.on_origin(checkout, branch)
        # GitHub closes a pull request whose branch it no longer has.
        pulls = open_pull_requests(checkout, branch) if on_origin else ()
    except Stop as stop:
        error = stop.message
    if not local and not on_origin:
        return None, error
    return Left(repository, branch, local, on_origin, pulls), error


def find(
    state: Path, current: str, branch: str, checkouts: dict[str, Path]
) -> tuple[list[Earlier], list[str]]:
    """Each branch of the upgrade runs since the last that completed, newest first, that it left
    in a repository of ``checkouts`` and that isn't ``branch``, the new run's own; and why each
    lookup that failed did."""
    since = takewhile(
        lambda run: run.status != "complete",
        (run for run in record.records(state, ("upgrade",)) if run.id != current),
    )
    runs: dict[str, list[Record]] = {}
    for run in since:
        if run.data.get("change"):
            runs.setdefault(gitops.run_branch(run.data["change"]), []).append(run)
    runs.pop(branch, None)
    jobs = [
        (repository, checkout, name)
        for name in runs
        for repository, checkout in checkouts.items()
        if gitops.branch(checkout) != name
    ]
    with ThreadPoolExecutor(max_workers=max(min(len(jobs), LOOKUPS_AT_ONCE), 1)) as pool:
        looked = list(pool.map(lambda job: _left(*job), jobs))
    lefts = [item for item, _ in looked if item is not None]
    found = [
        Earlier(
            members[0].id,
            name,
            any(touched_platform(run) for run in members),
            tuple(item for item in lefts if item.branch == name),
        )
        for name, members in runs.items()
        if any(item.branch == name for item in lefts)
    ]
    return found, [error for _, error in looked if error is not None]
