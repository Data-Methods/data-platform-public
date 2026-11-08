"""`prepare`: a change turned into pull requests, one per repository it changes, which people
review and merge in GitHub before `apply` brings the platform to `main`.

A Configuration Change is the commits on your branch that `origin/main` hasn't; an upgrade
adds the target's code, `prepare --to`. Each repository's change is built in its own copy, on
the change's branch: `platform-<version>` for an upgrade, `platform-change` for a Configuration
Change. While the instance's open change has pull requests open, its branch, `origin/<branch>`,
is rebased onto `origin/main`, keeping the commits added to it; otherwise the branch starts
again from `origin/main`. Then your branch's commits are added, in order: those `origin/main`
and the change's branch don't hold, by their patch or their files, as a squash merge leaves
them; one that comes out empty is dropped. A conflict stops, naming the files. Your checkout is
only an input: edits in it that aren't committed are asked about at a terminal, listing each,
"Commit these edits to your branch first?", which is the only time `prepare` changes it, and
otherwise left out and named. The commits it takes are recorded, and a resumed prepare starts
again from them. An upgrade starts from what the platform applied: its first build stops on your
branch's commits and leaves your edits out, and a plain `prepare` adds them afterwards.

Then each repository's code: the owner's upgrade to the target and every component's `migrate`,
which commit on the branch, then each component's render; what they push stays in the copy's
outbox. A prepare whose branches then hold no commit `origin/main` hasn't previews nothing and
opens nothing: it only starts again from `main` any branch GitHub kept. Then the preview: every
platform step, planned, read only, with its outcome; a step is provisional when a step it reads
from, directly or through other steps, plans a change, or when it reads the commit the merge
lands. A step whose plan fails is provisional only when the engine names the output it couldn't
read and that output's step plans a change; any other failure stops. A step that would write a
committed file the engine can't render stops the prepare. With `--no-preview`, the preview's
steps are read and checked against the manifest, and none is planned, so `apply` asks about
each change. Then one pull request per changed repository: its branch pushed with a lease on
the commit fetched, so GitHub's branch is replaced only while it's as fetched, and the pull
request linking the others and showing its part of the preview, or that it has none, recorded
as it opens. Its review is the pull request: a prepare asks nothing about plans, only whether to
restore a file you edited or remove one, as the owner's upgrade asks. The change, its pull
requests and the prepare run whose preview `apply` reads are recorded in the instance's shared
run record."""

from __future__ import annotations

import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .. import github, gitops, installed, places, processes
from ..components import Call, Cli, built
from ..instance import SourceRecord
from ..outcome import Next, Result, Stop, failed
from ..record import Record
from ..screen import Block
from ..sequence import Entry
from .graph import MERGED_COMMIT, downstream, producers, reads
from .holds import STANDING_BY
from .reading import refuse_earlier_release, walks
from .run import Run

# Git's answer when a push's lease no longer holds: GitHub's branch moved.
LEASE_REJECTED = re.compile(r"\[rejected\]|stale info|fetch first")
PREVIEW_LABELS = {
    "change": "a change",
    "no change": "no change",
    "pending": "pending",
    "skipped": "skipped",
    "provisional": "provisional",
}


def reaches(entry: Entry, graph: dict[str, list[str]], reached: list[str] | bool) -> list[str] | bool:
    """What the preview reaches once a change or a provisional step at ``entry`` adds what it
    reaches: a Terraform step's, the steps that read from it; any other step's, every later step."""
    if reached is True or not entry.terraform:
        return True
    return sorted({*reached, *downstream(graph, entry.id)})


@dataclass
class Answer:
    """What a plan said of one step: its finished event, the plan that failed at it, or both, and
    the plan text written for it."""

    event: dict | None = None
    failure: Call | None = None
    text: str = ""

    @property
    def skipped(self) -> bool:
        """Whether it skipped its plan, as `--planned` let it: rule C."""
        return bool(self.event and self.event.get("skip_rule"))

    @property
    def changes(self) -> bool:
        """Whether its plan has a change, which the preview records as one, but for a hold."""
        return self.failure is None and (self.event or {}).get("status") == "change"

    @property
    def stops(self) -> bool:
        """Whether a phase's plan stops at it: at its change, or its failure."""
        return self.failure is not None or self.changes


@dataclass
class PhasePlan:
    """One phase of the preview, from its first step not yet previewed, planned at once with the
    others: what its plans may skip, each step that skips that no change the preview had found
    reached as they started, and what they said of each step. `reached` is what the phase's own
    plan answered, `lead` the plan text it wrote ahead of every step, `failed_after` its failure
    once every step had reported and `error` what its planning raised. Each plan writes its text
    into `scratch`, a folder of its own, which the shared run record leaves out."""

    members: list[Entry]
    cli: Cli
    plan_file: Path
    skips: list[tuple[str, str]]
    scratch: Path
    answers: dict[str, Answer] = field(default_factory=dict)
    call: Call | None = None
    reached: set[str] = field(default_factory=set)
    lead: str = ""
    failed_after: Call | None = None
    error: Exception | None = None
    plans: int = 0

    @property
    def phase(self) -> str:
        return self.members[0].phase

    def took(self, call: Call, text: str) -> None:
        """The phase's own plan: each step's finished event and, when it failed, its failure at
        the first step without one. Its plan text goes with the last step it answered, or ahead
        of every step when it answered none, as when it stopped at a step previewed earlier, or
        failed once every step had reported, which stops the preview."""
        events = {event["step"]: event for event in call.finished() if event.get("phase") == self.phase}
        found = {member.step: Answer(events[member.step]) for member in self.members if member.step in events}
        failed_at = None
        if call.status != "done":
            failed_at = next((member for member in self.members if member.step not in events), None)
            if failed_at is not None:
                found[failed_at.step] = Answer(failure=call)
        self.failed_after = call if call.status != "done" and failed_at is None else None
        last = next((member.step for member in reversed(self.members) if member.step in found), None)
        if last is None or self.failed_after is not None:
            self.lead = text
        else:
            self.lead, found[last].text = "", text
        self.answers.update(found)
        self.call, self.reached = call, set(found)

    def took_alone(self, step: str, call: Call, text: str) -> Answer:
        """One step's plan alone: its last finished event and, when the plan failed, its failure."""
        events = [event for event in call.finished(step) if event.get("phase") == self.phase]
        answer = Answer(events[-1] if events else None, call if call.status != "done" else None, text)
        self.answers[step] = answer
        return answer


class Prepare(Run):
    """Record data: `target` (a Platform Version, or None for a Configuration Change), `change`,
    `branch`, `title`, `start`, `no_preview`, `upgrading`, `taken`, `left_out`, `preview` and
    `requests`. A
    preview of the change, `plan`, builds it the same way and walks only its preview: it pushes
    nothing, asks nothing and opens no pull request."""

    previewing_only = False
    # What a resumed prepare starts again from: what it was asked to prepare, the commits of your
    # branch it took and who walked it; never what its walk found.
    INPUTS = (
        "schema_version",
        "id",
        "kind",
        "target",
        "goal",
        "dp_instance",
        "started",
        "updated",
        "status",
        "stop",
        "channel",
        "change",
        "branch",
        "title",
        "start",
        "command",
        "no_preview",
        "manifest_origin",
        "change_kind",
        "change_target",
        "starts_upgrade",
        "beside",
        "open",
        "started_by",
        "resumed_by",
        "waiting",
        "sources",
        "left_out",
    )

    @classmethod
    def start_again(cls, run: Record) -> None:
        """Clear what the prepare's walk found, keeping its inputs, so it starts again."""
        run.data = {key: value for key, value in run.data.items() if key in cls.INPUTS}
        run.data.update(commits={}, entries=[], steps={})

    def go(self) -> None:
        if not self.record.entries:
            self.show_part("code")
            self.build()
        self.walk_entries()
        if not self.record.data.get("previewing"):
            declarations = self.build_renders()
            self.walk_entries()
            self.build_preview(declarations)
            self.walk_entries()

    # The change's branch -----------------------------------------------------------------

    def build(self) -> None:
        """Bring up each repository's branch, read its steps and, for an upgrade, the target's,
        then the run's entries: each repository's code."""
        self._progress = None
        started = time.monotonic()
        self.read_change()
        for repository in self.repositories:
            self.bring_up(repository.key)
        self.timed(
            "brought up",
            f"the {self.record.data['branch']} branch of {len(self.repositories)} repositories",
            started,
        )
        started = time.monotonic()
        declarations = self.declarations()
        upgrades: dict[str, bool] = {}
        if self.record.data.get("target"):
            self.fetch_sources()
            target = self.declarations(
                {key: self.source(key) for key in self.places},
                project={key: self.source(key) for key in self.places},
            )
            if not walks(target):
                raise refuse_earlier_release(f"Platform {self.manifest.platform_version}'s templates")
            jump = not walks(declarations)
            for repository in self.repositories:
                recorded = self.record_in(repository.key)
                if recorded.commit != self.manifest.sources[repository.key]:
                    upgrades[repository.key] = jump
            self.record.data["upgrading"] = list(upgrades)
            self.copy_notes()
            self.show_notes()
        elif not walks(declarations):
            raise refuse_earlier_release("This instance's templates")
        self.timed("read", f"the steps of {len(declarations)} components", started)
        self.record.entries = [] if self.previewing_only else self.plan(declarations).code(upgrades)
        self.record.save()

    def read_change(self) -> None:
        """What the instance's open change says of each repository: whose pull request of this
        change is open, so its branch, as GitHub holds it, is built on, any other's starting again
        from `origin/main`, as one whose pull request was closed, or merged with its branch kept;
        and the commits of your branch each merged pull request's prepare took."""
        found = self.session.shared.change() if self.session.shared is not None else None
        states = {
            item["url"]: github.state(self.instance.root, item["url"])
            for item in (found or {}).get("pull_requests") or []
        }
        same = found is not None and found.get("branch") == self.record.data["branch"]
        self.record.data["builds_on"] = [
            item["repository"]
            for item in (found or {}).get("pull_requests") or []
            if same and states[item["url"]] == "open"
        ]
        prepared = self.session.prepared(found.get("run")) if found is not None else None
        sources = (prepared.data.get("sources") or {}) if prepared is not None else {}
        self.record.data["merged_sources"] = {
            item["repository"]: [sources[item["repository"]]["commit"]]
            for item in (found or {}).get("pull_requests") or []
            if states[item["url"]] == "merged" and item["repository"] in sources
        }

    def record_in(self, repository: str) -> SourceRecord:
        found = SourceRecord.read(self.checkout(repository))
        if found is None:
            raise failed(f"{repository}'s copy has no template source record.")
        return found

    def bring_up(self, repository: str) -> None:
        """The repository's copy on the change's branch: the open change's, rebased onto
        `origin/main`, or `origin/main`; then your branch's commits that neither holds. GitHub's
        branch, as fetched, is the lease its push holds. A conflict stops, naming the files."""
        mapped = self.instance.components[repository]
        copy = self.checkout(repository)
        branch = self.record.data["branch"]
        places.fetch(copy)
        held = gitops.git(
            "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", cwd=copy, check=False
        )
        seen = held.stdout.strip() if held.ok else ""
        self.record.data.setdefault("leases", {})[repository] = seen
        building_on = bool(seen) and repository in (self.record.data.get("builds_on") or [])
        start = f"refs/remotes/origin/{branch}" if building_on else "refs/remotes/origin/main"
        gitops.git("checkout", "--quiet", "--force", "-B", branch, start, cwd=copy)
        gitops.git("clean", "--quiet", "-fd", cwd=copy)
        if not self.previewing_only:
            places.hold_pushes(copy)
        if building_on:
            self.replay_onto_main(repository, copy, branch)
        source = self.source_commit(repository, mapped, copy)
        self.take_commits(repository, copy, source, branch if building_on else None)
        self.record.save()

    def replay_onto_main(self, repository: str, copy: Path, branch: str) -> None:
        """Rebase the open change's branch onto `origin/main`, keeping every commit added to it."""
        found = gitops.git("rebase", "--quiet", "refs/remotes/origin/main", cwd=copy, check=False)
        if not found.ok:
            files = self.conflicts(copy)
            gitops.git("rebase", "--abort", cwd=copy, check=False)
            raise self.conflict(
                repository,
                f"rebasing the open change's branch, origin/{branch}, onto origin/main",
                files,
                found.first_error_line(),
                Next(
                    self.restart(),
                    f"builds the change again once origin/{branch} is rebased onto origin/main, its "
                    "conflicts resolved, and pushed",
                ),
            )

    def source_commit(self, repository: str, mapped, copy: Path) -> str:
        """The commit of your checkout the change takes, recorded the first time with your edits
        committed if you say so, and brought into the copy: a resumed prepare takes the same
        one, from your checkout or from GitHub, wherever it resumes."""
        sources = self.record.data.setdefault("sources", {})
        if repository not in sources:
            self.take_edits(repository, mapped)
            sources[repository] = {
                "commit": gitops.head(mapped.path),
                "branch": gitops.branch(mapped.path),
                "host": self.session.operator.get("host"),
            }
            self.record.save()
        source = sources[repository]
        commit = source["commit"]
        gitops.git(
            "fetch", "--quiet", str(mapped.path), "+HEAD:refs/dp-instance/checkout", cwd=copy, check=False
        )
        gitops.git(
            "fetch",
            "--quiet",
            str(mapped.path),
            "+refs/heads/*:refs/dp-instance/branches/*",
            cwd=copy,
            check=False,
        )
        if places.has(copy, commit):
            return commit
        branch, host = source.get("branch"), source.get("host") or "the workstation that started it"
        raise Stop(
            "failed",
            f"Run {self.record.id} prepares commit {commit[:12]} of {repository}"
            + (f", on {branch}" if branch else "")
            + f" on {host}, which neither this workstation nor GitHub has. Nothing has changed.",
            reason="source-missing",
            source={"repository": repository, **source},
            next=(
                *(
                    (
                        Next(
                            f"git push origin {branch}",
                            f"on {host}, in {mapped.stored}: then dp-instance resume here",
                        ),
                    )
                    if branch
                    else ()
                ),
                Next("dp-instance resume", f"on {host} instead"),
            ),
        )

    def take_commits(self, repository: str, copy: Path, source: str, open_branch: str | None) -> int:
        """Add your branch's commits that neither `origin/main` nor the change's branch holds, by
        their patch or their files, in order; one that comes out empty is dropped."""
        merged = tuple(
            commit
            for commit in (self.record.data.get("merged_sources") or {}).get(repository, [])
            if places.has(copy, commit)
        )
        ahead = gitops.unmerged(copy, "refs/remotes/origin/main", source, merged)
        new = {
            line[2:]
            for line in gitops.git("cherry", "HEAD", source, cwd=copy).stdout.splitlines()
            if line.startswith("+ ")
        }
        taken = 0
        for commit in (item for item in ahead if item in new):
            found = gitops.git("cherry-pick", commit, cwd=copy, check=False)
            if found.ok:
                taken += 1
                continue
            files = self.conflicts(copy)
            picking = gitops.git(
                "rev-parse", "--verify", "--quiet", "CHERRY_PICK_HEAD", cwd=copy, check=False
            ).ok
            empty = picking and not files and gitops.git("diff", "--quiet", "HEAD", cwd=copy, check=False).ok
            gitops.git("cherry-pick", "--abort", cwd=copy, check=False)
            if empty:
                continue
            onto = f"origin/{open_branch}" if open_branch else "origin/main"
            raise self.conflict(
                repository,
                f"adding your commit {commit[:12]}",
                files,
                found.first_error_line(),
                Next(
                    self.restart(),
                    f"builds the change again once your branch, rebased onto {onto}, no longer conflicts",
                ),
            )
        return taken

    def take_edits(self, repository: str, mapped) -> None:
        """Your edits that aren't committed: at a terminal, each file listed, an untracked one
        marked, and committed to your branch when you say so, the only time `prepare` changes your
        checkout; otherwise left out, and named. An upgrade's first build asks nothing: they join
        it afterwards, on the migrated files."""
        files = gitops.uncommitted(mapped.path, untracked=True)
        if not files:
            return
        starting = self.record.data.get("starts_upgrade")
        if self.session.interactive and not self.previewing_only and not starting:
            untracked = set(gitops.untracked(mapped.path))
            self.screen.blank()
            self.screen.labelled("Not Committed:", mapped.stored)
            for name in files:
                self.screen.labelled("", f"{name} (untracked: new to Git)" if name in untracked else name)
            if (
                self.screen.choose(
                    [("y", "commit them"), ("n", "leave them out")],
                    "Commit these edits to your branch first?",
                )
                == "y"
            ):
                on = gitops.branch(mapped.path)
                if on in (None, "main"):
                    # Never on `main`, which a squash merge then leaves behind: a branch of its own.
                    on = self.new_branch(mapped.path)
                    gitops.git("checkout", "--quiet", "-b", on, cwd=mapped.path)
                listed = "".join(f"{name}\0" for name in files)
                processes.run(
                    "git",
                    "add",
                    "--all",
                    "--pathspec-from-file=-",
                    "--pathspec-file-nul",
                    cwd=mapped.path,
                    input=listed,
                )
                processes.run(
                    "git", "commit", "--quiet", "--message", self.record.data["title"], cwd=mapped.path
                )
                self.screen.labelled("Committed:", f"{mapped.stored}: your edits, on your branch {on}")
                return
        self.record.data.setdefault("left_out", {})[repository] = files
        self.screen.labelled(
            "Left Out:", f"{mapped.stored}'s edits that aren't committed: {', '.join(files)}"
        )

    def new_branch(self, checkout: Path) -> str:
        """A branch name the checkout hasn't: the change's, or it with a number."""
        base = self.record.data["branch"]
        number = 1
        name = base
        while gitops.git(
            "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", cwd=checkout, check=False
        ).ok:
            number += 1
            name = f"{base}-{number}"
        return name

    @staticmethod
    def conflicts(copy: Path) -> list[str]:
        return gitops.git("diff", "--name-only", "--diff-filter=U", cwd=copy, check=False).stdout.split()

    @staticmethod
    def conflict(repository: str, doing: str, files: list[str], said: str, then: Next) -> Stop:
        named = ", ".join(files) if files else f"no file conflicted ({said})"
        return Stop(
            "failed",
            f"{repository} conflicts {doing}: {named}. Nothing was pushed.",
            reason="conflict",
            conflicts={"repository": repository, "files": files},
            next=(then,),
        )

    def push(self, repository: str, copy: Path, branch: str) -> None:
        """Push the branch to GitHub, past the copy's outbox, only while GitHub's branch is the
        commit fetched as the change was built, or absent when it was: a lease. A branch GitHub
        moved meanwhile stops, naming it."""
        lease = self.record.data["leases"][repository]
        url = gitops.origin(copy)
        # An explicit push URL is one Git never rewrites into the outbox.
        found = gitops.remote_git(
            "-c",
            f"remote.origin.pushurl={url}",
            "push",
            "--quiet",
            f"--force-with-lease=refs/heads/{branch}:{lease}",
            "origin",
            f"{branch}:refs/heads/{branch}",
            cwd=copy,
            answer=lambda found: LEASE_REJECTED.search(found.stderr) is not None,
        )
        if found.ok:
            return
        raise Stop(
            "failed",
            f"{repository}'s {branch} on GitHub changed after this prepare fetched it, as when someone "
            "pushed to it, so nothing was pushed to it and its pull request is as it was.",
            reason="lease",
            next=(Next(self.restart(), "builds the change again from GitHub's branches as they are now"),),
        )

    # The preview -------------------------------------------------------------------------

    def build_renders(self) -> dict:
        """Read the steps the change's code declares, its configuration migrated, then each
        component's render; the declarations, which the preview reads."""
        started = time.monotonic()
        declarations = self.declarations()
        if not walks(declarations):
            raise refuse_earlier_release(f"Platform {self.manifest.platform_version}'s templates")
        self.timed("read", f"the steps of {len(declarations)} components", started)
        self.record.entries = self.record.entries + self.plan(declarations).renders()
        self.record.save()
        return declarations

    def build_preview(self, declarations: dict) -> None:
        """Each platform step's preview, then each repository's pull request. The preview's
        entries are always built, which checks the manifest against the steps declared; `plan`
        always walks them, and a prepare only without `--no-preview` and while some branch holds
        a commit `origin/main` hasn't. With none, a prepare walks only its pull request entries,
        which start again from `main` any branch GitHub kept."""
        plan = self.plan(declarations)
        entries = plan.platform("preview")
        self.record.data["reads"] = reads(entries, declarations)
        self.record.data["producers"] = producers(entries, declarations)
        previewing = self.previewing_only or (
            not self.record.data.get("no_preview") and any(self.ahead(item.key) for item in self.repositories)
        )
        requests = [
            Entry(
                id=f"{repository.key}/pull-request",
                kind="pull-request",
                description=f"Pushes {self.record.data['branch']} and opens or updates its pull request.",
                repository=repository.key,
                repository_counter=f"{index}/{len(self.repositories)}",
                repository_description=f"The change's pull request in {repository.key}.",
            )
            for index, repository in enumerate(self.repositories, 1)
            if not self.previewing_only
        ]
        self.record.data["previewing"] = True
        self.record.entries = self.record.entries + (entries if previewing else []) + requests
        self.record.save()

    def preview_group(self, entry: Entry) -> None:
        """Plan every phase of the preview from this one on, read only, `PHASES_AT_ONCE` at a
        time, then show and judge each step in order, with its outcome and whether it's
        provisional, as planning one phase after another does."""
        phases = self.preview_phases(entry)
        try:
            self.plan_at_once(phases, self.record.data.get("reads") or {})
            for planned in phases:
                self.judge_phase(planned)
        finally:
            for planned in phases:
                # Scratch: a file a scanner still holds is left for the next preview to remove.
                shutil.rmtree(planned.scratch, ignore_errors=True)

    def preview_phases(self, entry: Entry) -> list[PhasePlan]:
        """Each phase's steps from ``entry`` on, the first from ``entry`` itself, with what its
        plans may skip: each step that skips that no change the preview has found so far reaches."""
        entries = self.record.entries
        groups: list[list[Entry]] = []
        for item in entries[entries.index(entry) :]:
            if item.kind != "preview":
                break
            last = groups[-1][-1] if groups else None
            if last is not None and (item.repository, item.component, item.phase) == (
                last.repository,
                last.component,
                last.phase,
            ):
                groups[-1].append(item)
            else:
                groups.append([item])
        phases = []
        for members in groups:
            first = members[0]
            name = f"{entries.index(first) + 1:03d}-{first.component}-{first.phase}.txt"
            cli = self.cli(first.repository, first.command)
            plan_file = self.record.file("plans", name)
            scratch = plan_file.with_suffix(".tmp")
            shutil.rmtree(scratch, ignore_errors=True)
            scratch.mkdir(exist_ok=True)
            phases.append(PhasePlan(members, cli, plan_file, self.unreached(members), scratch))
        return phases

    def plan_at_once(self, phases: list[PhasePlan], graph: dict[str, list[str]]) -> None:
        """Plan the phases, `PHASES_AT_ONCE` at a time, the longest first, as the last drift
        check measured them, so the slowest never starts last; one never measured goes first."""
        block = Block("preview", "", "", "", self.footer)
        done: list[PhasePlan] = []

        def say() -> None:
            block.message = (
                f"Planning {len(phases)} phase{'s' if len(phases) != 1 else ''}, "
                f"{processes.PHASES_AT_ONCE} at a time: {len(done)} done"
            )

        def plan(planned: PhasePlan) -> None:
            try:
                self.plan_phase(planned, graph)
            except Exception as error:
                # Raised in its phase's turn, once every phase before it is judged.
                planned.error = error
            done.append(planned)
            say()

        times = self.drift_times()
        ordered = sorted(
            phases, key=lambda item: -times.get(f"{item.members[0].component}/{item.phase}", float("inf"))
        )

        def work() -> None:
            built([planned.cli for planned in phases])
            list(pool.map(plan, ordered))

        self.count(phases[0].members[0], phases[-1].members[-1])
        say()
        with ThreadPoolExecutor(max_workers=processes.PHASES_AT_ONCE) as pool:
            self.screen.run_live(block, work)

    def plan_phase(self, planned: PhasePlan, graph: dict[str, list[str]]) -> None:
        """Plan one phase up to its first change, then each step after it alone, each skipping
        what the phase may skip that no change before it in the phase reaches. It runs beside the
        other phases, so it touches neither the record nor the screen."""
        planned.took(*self.plan_preview(planned, None, planned.skips))
        if planned.failed_after is not None:
            return
        reached: list[str] | bool = []
        for member in planned.members:
            if member.step not in planned.answers:
                skips = [
                    pair
                    for pair in planned.skips
                    if pair[0] == member.step and reached is not True and member.id not in reached
                ]
                planned.took_alone(member.step, *self.plan_preview(planned, member.step, skips))
            if planned.answers[member.step].changes and not member.holds_until and not member.affects_no_root:
                reached = reaches(member, graph, reached)

    def plan_preview(
        self, planned: PhasePlan, step: str | None, skips: list[tuple[str, str]]
    ) -> tuple[Call, str]:
        """One plan of the phase, or of one of its steps, skipping ``skips``: its call and the plan
        text it wrote, to a file of its own in the phase's scratch folder, which the phase's plan
        file takes in the steps' order. A phase's plans run one after another, never at once."""
        planned.plans += 1
        own = planned.scratch / f"{planned.plans:03d}.tmp"
        only = ["--step", step] if step else []
        call = planned.cli.run(
            "plan",
            "--phase",
            planned.phase,
            *only,
            "--json",
            "--plan-file",
            str(own),
            *self.version,
            *[part for name, value in skips for part in ("--planned", f"{name}={value}")],
            *(["--unchanged-since-planned"] if skips else []),
        )
        return call, own.read_text(encoding="utf-8") if own.exists() else ""

    def judge_phase(self, planned: PhasePlan) -> None:
        """Show and judge each step of a phase planned at once with the others, in order, as one
        planned after the phases before it is: a step whose plan skipped and that a change or a
        provisional step reaches plans again, alone, and a failed plan whose skips aren't the
        ones the preview so far leaves plans again, whole. A provisional step whose plan fails, as
        one that reads an output an earlier step of the change will publish first, is shown as
        provisional, with why its plan failed; any other failure stops, as does what its
        planning raised."""
        if planned.error is not None:
            raise planned.error
        entries = self.record.entries
        entry = planned.members[0]
        block = Block("phase", "", entry.phase, "", self.footer)
        block.message = f"Planning {entry.component}'s {entry.phase} phase"
        self.count(entry)
        skips = self.unreached(planned.members)
        if planned.call.status != "done" and skips != planned.skips:
            planned.took(*self.screen.run_live(block, self.plan_preview, planned, None, skips))
        planned.plan_file.unlink(missing_ok=True)
        self.add_plan(planned.plan_file, planned.lead)
        if planned.failed_after is not None:
            # It failed after every step reported: the phase's failure is its last step's.
            self.check_plan(planned.members[-1], planned.failed_after)
        alone = False
        for member in planned.members:
            self._position = entries.index(member) + 1
            answer = planned.answers[member.step]
            # Past the step the phase's plan stopped at, each step is planned alone, skipping only
            # while nothing judged so far reaches it.
            alone = alone or member.step not in planned.reached
            may_skip = {name for name, _ in (self.unreached([member]) if alone else skips)}
            if answer.skipped and member.step not in may_skip:
                found = self.screen.run_live(block, self.plan_preview, planned, member.step, [])
                answer = planned.took_alone(member.step, *found)
            self.add_plan(planned.plan_file, answer.text)
            if answer.failure is not None and self.unavailable_input(answer.failure) is None:
                self.check_plan(member, answer.failure)
            self.enter(member)
            self._walked.add(member.id)
            shown = self.block(member)
            self._entry, self._block = member, shown
            self.previewed(member, shown, answer.event, planned.plan_file, answer.failure)
            self.reach(member)
            self._entry = self._block = None
            alone = alone or answer.stops

    @staticmethod
    def add_plan(plan_file: Path, text: str) -> None:
        """Add one plan's text to the phase's plan file, after a blank line when it holds one
        already, as the engine separates the plans it adds to one file."""
        if not text:
            return
        with plan_file.open("a", encoding="utf-8") as stream:
            if stream.tell():
                stream.write("\n")
            stream.write(text)

    def unreached(self, members) -> list[tuple[str, str]]:
        """The steps the preview needn't plan: each that skips, as an apply last left it, that
        nothing planned so far in this change reaches. A plan stops at its first change, so a step
        after one is asked about again, alone, once this knows what that change reaches. All it
        could find is drift, which `apply`'s drift check finds."""
        reached = self.record.data.get("preview_reached")
        if reached is True:
            return []
        kept = self.kept_hashes()
        return [
            (member.step, kept[member.id])
            for member in members
            if member.plan_skip and member.id in kept and member.id not in (reached or ())
        ]

    def reach(self, entry: Entry) -> None:
        """What a change or a provisional step in the preview reaches: a Terraform step's, the
        steps that read from it; any other step's, unless it changes nothing a root reads, every
        later step."""
        item = (self.record.data.get("preview") or {}).get(entry.id) or {}
        if item.get("outcome") != "change" and not item.get("provisional"):
            return
        if entry.affects_no_root:
            return
        self.record.data["preview_reached"] = reaches(
            entry, self.record.data.get("reads") or {}, self.record.data.get("preview_reached") or []
        )

    def check_plan(self, entry: Entry, call) -> None:
        if call.status == "done":
            return
        self.enter(entry)
        failing = self.block(entry)
        self._entry, self._block = entry, failing
        if call.status in ("values", "sign-in", "runs", "pending"):
            self.wait(entry, failing, call)
        self.failure(entry, failing, call)

    def previewed(
        self, entry: Entry, block: Block, event: dict | None, plan_file: Path, failure=None
    ) -> None:
        """Show and record one step's preview: a provisional step whose plan failed waits for the
        outputs of the step it reads from, which apply applies first."""
        preview = self.record.data.setdefault("preview", {})
        if entry.holds_until:
            # A hold never plans in its turn: apply applies it for the first step it guards with
            # a change.
            preview[entry.id] = {"outcome": STANDING_BY, "summary": self.standing_by(entry)}
            self.stand_by(entry, block)
            return
        if failure is not None:
            because = self.unavailable_input(failure)
            item = {
                "outcome": "provisional",
                "summary": f"its plan failed: {failure.message}",
                "provisional": True,
            }
            item["because"] = because
            block.add("Provisional:", f"{because}; its plan failed: {failure.message}")
            preview[entry.id] = item
            self.finish(
                entry, block, "provisional", **{key: value for key, value in item.items() if key != "outcome"}
            )
            return
        if event is None:
            preview[entry.id] = {"outcome": "skipped"}
            self.finish(entry, block, "skipped", compact=True)
            return
        self.optional(event)
        status = event.get("status")
        outcome = {"change": "change", "no-change": "no change", "pending": "pending"}.get(
            status, str(status)
        )
        because = self.provisional(entry)
        item = {"outcome": outcome, "summary": event.get("summary"), "provisional": bool(because)}
        if because:
            item["because"] = because
        if outcome == "change":
            item["decisions"] = [
                {
                    "id": found.get("id"),
                    "digest": found.get("digest"),
                    "auto": found.get("auto"),
                    "counts": found.get("counts"),
                    "destroys": found.get("destroys"),
                }
                for found in event.get("decisions") or []
            ]
            block.add("Step Plan:", event.get("summary") or "a change")
            if plan_file.exists() and plan_file.stat().st_size:
                block.add("Plan File:", self.relative(plan_file))
        if because:
            block.add("Provisional:", because)
        preview[entry.id] = item
        detail = {key: value for key, value in item.items() if key != "outcome"}
        self.finish(entry, block, outcome, compact=outcome == "no change" and not because, **detail)

    def unavailable_input(self, call) -> str | None:
        """Why a step whose plan failed is provisional: the engine named the output it couldn't
        read, `input_unavailable`, and that output's step plans a change in this preview, or is
        itself provisional. Any other failure, or one from an engine that names none, is None: it
        stops the prepare."""
        found = (call.result or {}).get("input_unavailable") if call.status == "failed" else None
        if not isinstance(found, dict):
            return None
        name = f"{found.get('component')}/{found.get('output')}"
        producer = (self.record.data.get("producers") or {}).get(name)
        item = (self.record.data.get("preview") or {}).get(producer or "") or {}
        if item.get("outcome") == "change":
            return f"it reads {name}, which {producer} plans to change"
        if item.get("provisional"):
            # Its producer's own preview is provisional: what it publishes may change too.
            return f"it reads {name}, which {producer} may change: its preview is provisional"
        return None

    def provisional(self, entry: Entry) -> str | None:
        """Why a step's preview is provisional: a step it reads from, directly or through other
        steps, plans a change, or it reads the commit its repository's merge lands; None when
        it's exact."""
        reads = self.record.data.get("reads", {})
        preview = self.record.data.get("preview", {})
        seen, waiting = set(), list(reads.get(entry.id, []))
        while waiting:
            producer = waiting.pop()
            if producer in seen:
                continue
            seen.add(producer)
            if producer == MERGED_COMMIT:
                return f"it reads the commit {entry.repository}'s merge lands"
            if (preview.get(producer) or {}).get("outcome") == "change":
                return f"it reads from {producer}, which plans a change"
            waiting.extend(reads.get(producer, []))
        return None

    # The pull requests -------------------------------------------------------------------

    def handlers(self) -> dict:
        return {**super().handlers(), "pull-request": self.pull_request, "render": self.render}

    def render(self, entry: Entry, block: Block) -> None:
        """Write the committed files the component's steps keep current, from the published
        Component Outputs, read only, and commit each step's files on the branch; a step that waits
        on an earlier step's apply is named, its files left as they are."""
        cli = self.cli(entry.repository, entry.command)
        approvals = list(self.options.approvals)
        while True:
            # Only what a person approved: render never takes --auto-approve.
            approving = [
                part for identifier in dict.fromkeys(approvals) for part in ("--approve", identifier)
            ]
            call = self.screen.run_live(block, cli.run, "render", "--json", *self.version, *approving)
            if call.status == "decision":
                self.decide(entry, block, call, approvals)
                continue
            if call.status != "done":
                if call.status in ("values", "sign-in", "runs", "pending"):
                    self.wait(entry, block, call)
                self.failure(entry, block, call)
            break
        copy = self.checkout(entry.repository)
        rendered, pending = [], []
        for event in call.finished():
            files = [str(name) for name in event.get("files_written") or []]
            if event.get("status") == "rendered" and files:
                listed = "".join(f"{name}\0" for name in files)
                processes.run(
                    "git",
                    "add",
                    "--all",
                    "--pathspec-from-file=-",
                    "--pathspec-file-nul",
                    cwd=copy,
                    input=listed,
                )
                if gitops.uncommitted(copy):
                    processes.run(
                        "git",
                        "commit",
                        "--quiet",
                        "--message",
                        f"{entry.component}: {event['step']} rendered",
                        cwd=copy,
                    )
                rendered.append(
                    {"step": event["step"], "files": files, "outputs_read": event.get("outputs_read") or []}
                )
                block.add("Rendered:", f"{event['step']}: {', '.join(files)}")
            elif event.get("status") == "pending":
                pending.append(event)
        self.record.data.setdefault("rendered", {})[entry.id] = rendered
        if pending:
            self.render_pending(entry, block, pending)
        self.finish(entry, block, "applied" if rendered else "no change", compact=not rendered)

    def render_pending(self, entry: Entry, block: Block, pending: list[dict]) -> None:
        """A step that can't render yet, as when it reads an output of a component that hasn't
        published it, as one that joins, stops the prepare, naming the components to apply
        first."""
        first = sorted(
            {
                name.split("/", 1)[0]
                for event in pending
                for name in event.get("outputs_read") or []
                if name.split("/", 1)[0] != entry.component
            }
        )
        steps = ", ".join(event["step"] for event in pending)
        for event in pending:
            block.add("Not Rendered:", f"{event['step']}: waits for an output no apply has published yet")
        raise Stop(
            "failed",
            f"{entry.component}'s {steps} can't render yet: "
            + (f"apply {', '.join(first)} first, which publishes" if first else "an earlier apply publishes")
            + " the outputs it reads. Nothing was opened for review.",
            reason="render-pending",
            render_pending={
                "component": entry.component,
                "steps": [event["step"] for event in pending],
                "apply_first": first,
            },
            next=(Next("dp-instance apply", "applies what main holds, which publishes them"),),
        )

    def pull_request(self, entry: Entry, block: Block) -> None:
        """Before the first pull request: a step that would write a committed file stops the
        prepare. Then push the repository's branch and open or update its pull request, when the
        branch holds commits `origin/main` hasn't, and record it in the open change."""
        if "touches" not in self.record.data:
            # Every repository the change touches is recorded before anything is pushed, so `apply`
            # refuses while any has no merged pull request.
            self.record.data["touches"] = [item.key for item in self.repositories if self.ahead(item.key)]
            self.record.save()
            if self.record.data["touches"]:
                self.record_open_change()
        copy = self.checkout(entry.repository)
        branch = self.record.data["branch"]
        ahead = self.ahead(entry.repository)
        if not ahead:
            # A branch GitHub kept from a closed or merged pull request starts again from main,
            # so what it held never comes back.
            lease = self.record.data["leases"][entry.repository]
            if lease and lease != gitops.git("rev-parse", branch, cwd=copy).stdout.strip():
                self.push(entry.repository, copy, branch)
            self.finish(entry, block, "no change", compact=True)
            return
        self.push(entry.repository, copy, branch)
        url = gitops.origin(copy) or ""
        body = self.body(entry.repository, [])
        found = github.open_for(copy, url, branch)
        if found is None:
            found = github.create(copy, url, branch, self.record.data["title"], body)
        else:
            github.describe(copy, url, found["number"], self.record.data["title"], body)
        self.record.data.setdefault("requests", {})[entry.repository] = found
        self.record.save()
        self.record_open_change()
        self.show_pull_request(block, found["url"], "open", at_once=True)
        self.finish(
            entry,
            block,
            "applied",
            summary=f"{ahead} commit{'s' if ahead != 1 else ''}",
            pull_requests=self.pull_requests(block),
        )

    def ahead(self, repository: str) -> int:
        """How many commits the repository's branch holds that `origin/main` hasn't."""
        branch = self.record.data["branch"]
        found = gitops.git(
            "rev-list", "--count", f"refs/remotes/origin/main..{branch}", cwd=self.checkout(repository)
        )
        return int(found.stdout.strip() or 0)

    def requests(self) -> list[dict]:
        return [
            {"repository": repository, **found}
            for repository, found in (self.record.data.get("requests") or {}).items()
        ]

    def record_open_change(self) -> None:
        """Record the change, with each pull request opened so far, as the instance's open change.
        Another change with a pull request still open is never replaced."""
        found = self.session.shared.change()
        if found is not None and found.get("branch") != self.record.data["branch"]:
            still = [
                item
                for item in found.get("pull_requests") or []
                if github.state(self.instance.root, item["url"]) == "open"
            ]
            if still:
                raise Stop(
                    "failed",
                    f"The instance's open change, {found.get('title')}, has open pull requests, so this "
                    f"change isn't recorded in its place: "
                    + "; ".join(f"{item['repository']}'s {item['url']}" for item in still)
                    + ". This change's pull requests stay open: close them, or merge or close those "
                    "first and prepare again.",
                    reason="open-change",
                    next=tuple(
                        Next(item["url"], f"{item['repository']}'s open pull request") for item in still
                    ),
                )
        open_change = self.record.data.get("open") or {}
        # The change as recorded before, when it's this one: the repositories it touches, and the
        # pull request of each this run opens none for, as one merged already.
        earlier = found if found is not None and found.get("branch") == self.record.data["branch"] else {}
        touches = sorted({*(earlier.get("repositories") or []), *self.record.data.get("touches", [])})
        mine = self.requests()
        opened = {item["repository"] for item in mine}
        kept = [item for item in earlier.get("pull_requests") or [] if item.get("repository") not in opened]
        self.session.shared.record_change(
            {
                "repositories": touches,
                "kind": self.record.data.get("change_kind")
                or ("upgrade" if self.record.data.get("target") else "configuration"),
                "change": self.change,
                "branch": self.record.data["branch"],
                "title": self.record.data["title"],
                "target": self.record.data.get("change_target", self.record.data.get("target")),
                "channel": self.channel,
                "run": self.record.id,
                "pull_requests": [*mine, *kept],
                "merged": open_change.get("merged") or [],
            }
        )

    def body(self, repository: str, others: list[dict]) -> str:
        """A pull request's description: what it prepares, the change's other pull requests and
        its repository's part of the preview."""
        what = self.record.data["goal"].removeprefix("prepare: ")
        version = f" {installed.version()}" if installed.version() else ""
        lines = [
            f"{what[:1].upper()}{what[1:]}, prepared by `dp-instance{version}` in run `{self.record.id}`.",
            "",
            "Review and merge it in GitHub; `dp-instance apply` then brings the platform to `main`.",
        ]
        if others:
            lines += ["", "**This change's other pull requests:**", ""]
            lines += [f"- {item['repository']}: {item['url']}" for item in others]
        if self.record.data.get("no_preview"):
            lines += [
                "",
                "**Preview:** none: prepared with `--no-preview`, so `dp-instance apply` asks about each "
                "change before it applies it, unless `--auto-approve`.",
            ]
        else:
            lines += ["", "**Preview:**", ""]
        preview = self.record.data.get("preview") or {}
        for entry in self.record.entries:
            if entry.kind != "preview" or entry.repository != repository:
                continue
            item = preview.get(entry.id) or {"outcome": "not planned"}
            said = PREVIEW_LABELS.get(item["outcome"], item["outcome"])
            if item.get("summary"):
                said += f": {item['summary']}"
            if item.get("provisional"):
                said += f" (provisional: {item.get('because')})"
            lines.append(f"- `{entry.component}` {entry.phase}/{entry.step}: {said}")
        rendered = [
            (key, item)
            for key, items in (self.record.data.get("rendered") or {}).items()
            if key.startswith(f"{repository}/")
            for item in items
        ]
        if rendered:
            lines += ["", "**Rendered:**", ""]
            for key, item in rendered:
                said = f"- `{key.split('/')[1]}` {item['step']}: {', '.join(item['files'])}"
                changing = self.changing(item["outputs_read"])
                if changing:
                    said += f" (provisional: it reads {', '.join(changing)}, which this change changes)"
                lines.append(said)
        left = (self.record.data.get("left_out") or {}).get(repository)
        if left:
            lines += ["", f"Left out, not committed: {', '.join(left)}."]
        return "\n".join(lines) + "\n"

    def changing(self, outputs: list[str]) -> list[str]:
        """The outputs, `<component>/<name>`, whose producing step plans a change in the preview."""
        producers = self.record.data.get("producers") or {}
        preview = self.record.data.get("preview") or {}
        return [
            name
            for name in outputs
            if (preview.get(producers.get(name, "")) or {}).get("outcome") == "change"
        ]

    # Its end ----------------------------------------------------------------------------

    def end(self) -> Result:
        requests = self.requests()
        if len(requests) > 1:
            for item in requests:
                others = [other for other in requests if other is not item]
                copy = self.checkout(item["repository"])
                github.describe(
                    copy,
                    gitops.origin(copy) or "",
                    item["number"],
                    self.record.data["title"],
                    self.body(item["repository"], others),
                )
        if requests:
            self.record_open_change()
        self.record.complete()
        took = self.run_time()
        self.screen.complete(took["total"], took["waiting"])
        left_out = self.record.data.get("left_out") or {}
        if not requests:
            message = "Nothing to prepare: no repository's branch holds a commit origin/main hasn't."
            self.screen.blank()
            self.screen.note(message, "bold green")
            self.screen.run_record(self.relative(self.record.folder) + "/")
            return Result(
                "prepare",
                "done",
                message,
                (),
                {"run": self.record.id, "left_out": left_out, "run_time": took},
            )
        message = (
            f"Prepared: {len(requests)} pull request{'s' if len(requests) != 1 else ''} to review and "
            "merge in GitHub, then dp-instance apply."
        )
        steps = (
            Next(
                "dp-instance apply",
                "once each pull request is merged: brings the platform to main"
                + (
                    ", asking about each change, which no preview showed, unless --auto-approve"
                    if self.record.data.get("no_preview")
                    else ""
                ),
            ),
        )
        self.screen.then(
            "Merge each pull request, then run dp-instance apply.", self.relative(self.record.folder) + "/"
        )
        return Result(
            "prepare",
            "done",
            message,
            steps,
            {
                "run": self.record.id,
                "pull_requests": requests,
                "left_out": left_out,
                "run_time": took,
                "preview": self.record.data.get("preview") or {},
            },
        )
