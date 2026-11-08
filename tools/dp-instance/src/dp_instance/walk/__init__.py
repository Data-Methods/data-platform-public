"""A run: the sequencer walking the steps, one process per step, in one of two kinds.

A prepare turns a change into pull requests, building each repository's change in its own copy
(`prepare`); an apply brings the platform to `main`, from each repository's own copy at
`origin/main` (`apply`). Each kind's parts are counted and timed on their own, in wall time: a
prepare walks the code, its preview and its pull requests; an apply, before anything changes,
the drift check, then the platform. Neither runs a component's `preflight`, which `dp-instance
preflight` runs, all at once. In the platform, each step is planned only after the steps before
it have applied: a step with no change passes, and at a step with a change the run stops for review and, on
approval, applies exactly that plan, which the component verifies before the next step starts.
A failed step's stop ends with its own command in its repository. While rule C holds in an
apply, a Terraform step whose drift plan had no change is named to its component's `apply`
with that plan's inputs hash, and reports no change without running Terraform while the hash
still matches. Rule C holds until a step applies a real change, a create, an update in place, a
replacement or a destroy, and isn't declared to write only its repositories or to change
nothing a Terraform root reads, the run waits on outside work, it
resumes after any stop but a decision, or a decision's resume finds the step it stopped at with
nothing left to apply. Only
the latest drift check's plans count. At a terminal a person is asked with single keypresses;
without one, or with `--json`, every question is a stop naming the command that continues. Each
pull request a step reports is shown as it arrives, and its last state kept. A completed apply
names its recommended optional handoffs, and its `--json` result carries them as `handoffs`. It
also names each Managed Repository's Adoption PR a step reported and left open, which the run
never waits for: its merge and release come first, then `dp-instance verify`, the Completion
Check. A completed run's time is its wall time from its start, with the time it waited, at a
stop until it resumed or at a question until it was answered, apart from the time it worked.
"""

from .apply import Apply
from .common import APPLY, LEFT_OPEN, PARTS, UNKNOWN_VERSION, VERIFY, adoption_left, part_of
from .prepare import Prepare
from .run import Run

__all__ = [
    "APPLY",
    "LEFT_OPEN",
    "PARTS",
    "UNKNOWN_VERSION",
    "VERIFY",
    "Apply",
    "Prepare",
    "Run",
    "adoption_left",
    "part_of",
]
