"""What every part of a run shares: its parts and labels, and the Adoption PRs and outside work it
names."""

from __future__ import annotations

from .. import gitops, handoffs
from ..outcome import Next
from ..record import Record
from ..sequence import Entry

# A run's parts, in order, each counted and timed on its own: a prepare's are Preflight,
# Repository Changes, Platform Preview and Pull Requests; an apply's, Preflight and Platform Changes.
PARTS = {
    "before": "Preflight",
    "code": "Repository Changes",
    "preview": "Platform Preview",
    "requests": "Pull Requests",
    "platform": "Platform Changes",
}
# The checks before the run, as a resumed run names those that passed earlier.
CHECKS = {"preflight": "preflights", "drift": "the drift check"}
# How many of a decision's plan lines its JSON result carries.
PLAN_HEAD = 40
UNKNOWN_VERSION = (
    "The drift check can't tell which Platform Version the platform runs: the records at the commits "
    "it last applied name no published release, and this workstation has no record of the candidate "
    "they hold. Run the apply on the workstation that prepared the candidate."
)
# A `hold` step while nothing needs holding yet.
STANDING_BY = "standing by"
LEVELS = ("repository", "component", "phase", "step")
# The lines a step's block shows for a decision, which give way to what it applied.
DECISION_LABELS = ("Step Plan:", "Reviewed:", "Plan File:", "Decision:", "Hold:", "Release Notes:")
# What a run says of the Adoption PRs it leaves open, and the command after their merge and release.
LEFT_OPEN = (
    "the run doesn't wait for these: dp-instance verify, the Completion Check, passes once each is "
    "merged and released"
)
VERIFY = Next("dp-instance verify", "the Completion Check, once each Adoption PR is merged and released")
APPLY = Next("dp-instance apply", "brings the platform to main, reviewing each change before it applies")
DRIFT_TEXT = (
    "Every component's phases planned, read only and in parallel, on the commit its walk last applied; "
    "any change is drift, a change made outside a run. A component no walk has applied is left out."
)
# The kinds of entry in each part.
CODE_KINDS = ("upgrade", "migrate", "render")


def part_of(entry: Entry) -> str:
    """The part a run's entry is in: the checks before anything changes, a prepare's code, its
    preview and its pull requests, or an apply's platform."""
    if entry.kind in ("preflight", "drift"):
        return "before"
    if entry.kind in CODE_KINDS:
        return "code"
    if entry.kind == "preview":
        return "preview"
    if entry.kind == "pull-request":
        return "requests"
    return "platform"


def level_key(entry: Entry, level: str) -> tuple:
    """Which repository part, component or phase the entry is in, each part of a run apart."""
    part = (part_of(entry), entry.repository)
    return {
        "repository": (part,),
        "component": (part, entry.component),
        "phase": (part, entry.component, entry.phase),
    }[level]


def adoption_prs(instance, run: Record) -> list[dict]:
    """Each Managed Repository's Adoption PR the run's steps reported and left open: each pull
    request in a mapped Managed Repository, in the last state any step reported it in, unless
    that's closed. A Managed Repository's pull requests are its Adoption PRs, whichever component
    maintains it."""
    last: dict[str, str | None] = {}
    for entry in run.entries:
        for item in (run.outcome(entry.id) or {}).get("pull_requests") or []:
            if isinstance(item, dict) and item.get("url"):
                last[item["url"]] = item.get("state")
    found = []
    for url, state in last.items():
        repository = gitops.pull_request_repository(url)
        managed = next(
            (
                item
                for item in instance.managed
                if repository and gitops.same_repository(item.remote, repository)
            ),
            None,
        )
        if managed is not None and state != "closed":
            found.append(
                {"repository": managed.name, "managed_by": managed.managed_by, "url": url, "state": state}
            )
    return found


def adoption_left(item: dict) -> str:
    """What's left of an Adoption PR a run leaves open: its merge and release, or its release once
    it's merged."""
    return "release" if item.get("state") == "merged" else "merge and release"


def by_system(sections: list[dict]) -> dict[str, int]:
    """How many pieces of outside work each system holds, as its handoff page groups them, in
    the order of the systems' names."""
    found: dict[str, set] = {}
    for item in sections:
        found.setdefault(item.get("system", "other"), set()).add(item.get("id", item.get("title")))
    return {system: len(found[system]) for system in sorted(found, key=handoffs.system_name)}
