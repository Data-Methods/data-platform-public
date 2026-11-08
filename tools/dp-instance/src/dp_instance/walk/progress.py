"""How a run shows itself: its parts, levels and entries on the screen, each with its time, and
each entry's outcome in the record."""

from __future__ import annotations

import time
from datetime import datetime

from rich.text import Text

from .. import gitops
from ..screen import Block, duration
from ..sequence import Entry
from .base import Base
from .common import CHECKS, LEVELS, PARTS, STANDING_BY, level_key, part_of

# What a prepare, a plan and an apply show of a step that changed nothing: a count, as its part
# ends. The pull request and the run record keep each one.
QUIET_RUNS = ("prepare", "plan", "apply")
QUIET_OUTCOMES = ("no change", "skipped", STANDING_BY)
QUIET_KINDS = ("render", "upgrade", "migrate", "preview", "step", "pull-request", "mark")


class Progress(Base):
    def footer(self) -> str:
        shown = [self._progress] if self._progress else []
        working = time.monotonic() - self.started - (self.screen.waited - self._waited)
        shown.append(duration(working))
        return "   ".join(shown)

    def counted(self) -> dict[str, list[str]]:
        """Each part's entries, by ID, in order."""
        found: dict[str, list[str]] = {}
        for entry in self.record.entries:
            found.setdefault(part_of(entry), []).append(entry.id)
        return found

    def position(self, entry: Entry) -> tuple[str, int, int]:
        """The entry's part, its position in that part and the part's count."""
        members = self.counted().get(part_of(entry), [])
        place = members.index(entry.id) + 1 if entry.id in members else len(members) + 1
        return part_of(entry), place, max(len(members), place)

    def count(self, entry: Entry, last: Entry | None = None) -> None:
        """The footer's progress for the entry under way, or the entries from ``entry`` to
        ``last`` under way at once: its part, its position and count, and the parts ahead, the
        platform's count estimated from the checkouts' declarations until the target's are read,
        once `migrate` has run."""
        part, place, total = self.position(entry)
        if last is not None and last.id != entry.id:
            place = f"{place}-{self.position(last)[1]}"
        counted = self.counted()
        ahead = []
        for later in list(PARTS)[list(PARTS).index(part) + 1 :]:
            if later in counted:
                ahead.append(f"{PARTS[later]}: {len(counted[later])} Steps")
        self._progress = f"{PARTS[part]} Step {place} of {total}" + (
            f"   Followed by {', '.join(ahead)}" if ahead else ""
        )

    def timed(self, label: str, text: str, started: float) -> None:
        """Show work no entry holds, such as reading the steps, with its time."""
        self.screen.timed(label, text, time.monotonic() - started)

    def show_part(self, part: str) -> None:
        """Name a part as it starts, and start its clock the first time this command shows it."""
        self._clock.setdefault(part, (time.monotonic(), self.screen.waited))
        if self._shown is None or self._shown[0] != part:
            self.screen.drop_held()
            self.screen.stage(PARTS[part])
            self._shown = (part, None, None, None)
            if part == "code":
                self.show_notes()

    def clock_part(self, part: str) -> float:
        """Add the part's wall time in this command, since it was first shown and leaving out
        the time a prompt waited for an answer, to its time in the record's `clock`, which keeps
        the time of every command that walked it; the part's time."""
        clock = self.record.data.setdefault("clock", {})
        if part in self._clock:
            started, waited = self._clock.pop(part)
            seconds = time.monotonic() - started - (self.screen.waited - waited)
            clock[part] = round(clock.get(part, 0) + seconds, 1)
        return clock.get(part, 0)

    def close_part(self, part: str | None = None) -> None:
        """End a part, by default the one being walked, with a line of its count and wall time, in
        every command that walked it, which the record's `parts` keeps for each part that ended.
        A part shows once in a command."""
        part = part or self._part
        self._part = None
        if part is None or part in self._closed:
            return
        self._closed.add(part)
        self.screen.drop_held()
        self.quiet_count(part)
        # Its entries that ran: all of them, but in a preview that ends at its first change.
        steps = [item for item in self.counted().get(part, []) if self.record.outcome(item)]
        seconds = self.clock_part(part)
        self.record.data.setdefault("parts", {})[part] = seconds
        self.record.save()
        self.screen.ended(PARTS[part], seconds, steps=len(steps), run=self.record.id)

    def quiet_count(self, part: str) -> None:
        """What the part didn't show: the repositories, or the steps, that changed nothing."""
        count = self._quiet.get(part, 0)
        if not count:
            return
        if part in ("code", "requests"):
            others = len(self.repositories) - len(self._showing.get(part, ()))
            if others:
                self.screen.blank()
                self.screen.note(
                    f"No changes in the {'other ' if part in self._showing else ''}{others} "
                    f"repositor{'ies' if others != 1 else 'y'}."
                )
            return
        other = "other " if part in self._showing else ""
        said = "No change or skipped in" if part == "platform" else "No change in"
        self.screen.blank()
        self.screen.note(f"{said} {count} {other}step{'s' if count != 1 else ''}.")

    def pause(self) -> None:
        """As the command ends: the wall time of each part it was walking, and the time its
        prompts waited for answers, which counts as the run's waiting."""
        for part in list(self._clock):
            self.clock_part(part)
        waited = self.screen.waited - self._waited
        self._waited = self.screen.waited
        self.record.data["waiting"] = round(self.record.data.get("waiting", 0) + waited, 1)

    def run_time(self) -> dict[str, float]:
        """The run's wall time from its start to now, and how much of it the run waited, at a
        stop until it resumed or at a question until it was answered, or worked."""
        total = (
            datetime.now().astimezone() - datetime.fromisoformat(self.record.data["started"])
        ).total_seconds()
        waiting = min(self.record.data.get("waiting", 0), total)
        return {"total": round(total, 1), "working": round(total - waiting, 1), "waiting": round(waiting, 1)}

    def versions(self) -> list[tuple[str, str]]:
        """The Platform Versions a run names: what a prepare starts from and, for an upgrade, what
        it takes the instance to; what an apply brings it to; what any other run reads."""
        version = self.manifest.platform_version
        if self.record.kind == "apply":
            return [("Target Platform Version", version)]
        if self.record.kind != "prepare":
            return [("Platform Version", version)]
        if self.record.data.get("change_kind") != "upgrade":
            return [("Current Platform Version", version)]
        start = self.record.data.get("start")
        return [
            *([("Current Platform Version", start)] if start else []),
            ("Target Platform Version", version),
        ]

    def header(self) -> None:
        self.screen.header(
            self.record.kind,
            ("instance", self.instance.key),
            *self.versions(),
            ("run", self.record.id),
        )
        if self.unlisted:
            self.screen.note(f"Not in this release, left as they are: {', '.join(self.unlisted)}.")
        self.screen.emit(
            {
                "event": "run",
                "run": self.record.id,
                "kind": self.record.kind,
                "goal": self.record.data["goal"],
            }
        )

    def enter(self, entry: Entry, until: str = "step") -> None:
        """Name each level as it's entered: the part, then repository, component and phase,
        down to ``until``, the level the caller shows itself."""
        stage = part_of(entry)
        self.show_part(stage)
        if stage in ("preflight", "before", "requests"):
            return
        depth = LEVELS.index(until)
        _, repository, component, phase = self._shown
        (part,) = level_key(entry, "repository")
        if depth > 0 and part != repository:
            self.screen.drop_held("repository", "component", "phase")
            with self.screen.holding("repository"):
                self.screen.blank()
                self.screen.level("repository", entry.repository_counter)
                self.screen.labelled("Repo Name:", Text(entry.repository, style="bold"))
                self.screen.labelled("Repo Desc:", entry.repository_description)
                branch = gitops.branch(self.checkout(entry.repository))
                if branch:
                    self.screen.labelled("Repo Branch:", branch)
            repository, component, phase = part, None, None
        if depth > 1 and entry.component and entry.component != component:
            self.screen.drop_held("component", "phase")
            with self.screen.holding("component"):
                self.screen.blank()
                self.screen.level(
                    "component", entry.component_counter + (" (Repository Owner)" if entry.owner else "")
                )
                self.screen.labelled("Component Name:", Text(entry.component, style="bold"))
                self.screen.labelled("Component Desc:", entry.component_description)
            component, phase = entry.component, None
        if depth > 2 and entry.kind in ("render", "upgrade", "migrate", "pull-request", "mark"):
            self.screen.drop_held("phase")
            with self.screen.holding("phase"):
                self.screen.blank()
            phase = None
        elif depth > 2 and entry.phase != phase:
            self.screen.drop_held("phase")
            with self.screen.holding("phase"):
                self.screen.blank()
                self.screen.level("phase", entry.phase_counter)
                self.screen.labelled("Phase Name:", Text(entry.phase, style="bold"))
                self.screen.labelled("Phase Desc:", entry.phase_description)
                self.screen.blank()
            phase = entry.phase
        self._shown = (stage, repository, component, phase)

    def entered(self, entry: Entry, level: str) -> bool:
        """Whether the screen already shows the entry's repository part, component or phase."""
        if self._shown is None or self._shown[0] != part_of(entry):
            return False
        depth = LEVELS.index(level)
        return self._shown[1 : depth + 2] == level_key(entry, level)

    def replay(self, start: int) -> None:
        """Show what a resumed run finished earlier, from entry ``start`` on, in the fewest
        lines: the checks before the run that passed take one line, and in the run, the largest
        level not yet entered whose entries all finished takes one line, so a step done earlier
        keeps a line of its own only in a phase still under way."""
        entries = self.record.entries
        entry = entries[start]
        if part_of(entry) == "before":
            self.replay_checks()
            return
        for depth, level in enumerate(LEVELS[:3]):
            if level == "phase" and entry.kind in ("render", "upgrade", "migrate", "pull-request", "mark"):
                break
            if self.entered(entry, level):
                continue
            key = level_key(entry, level)
            group = []
            for item in entries[start:]:
                if level_key(item, level) != key:
                    break
                group.append(item)
            if not all(self.record.finished(item.id) for item in group):
                continue
            self.enter(entry, until=level)
            name, counter = {
                "repository": (entry.repository, entry.repository_counter),
                "component": (entry.component, entry.component_counter),
                "phase": (entry.phase, entry.phase_counter),
            }[level]
            seconds = sum(self.record.outcome(item.id).get("seconds") or 0 for item in group)
            self.screen.done(level, counter, name, len(group), seconds)
            self._walked.update(item.id for item in group)
            self._shown = self._shown[: depth + 1] + (None,) * (3 - depth)
            return
        self.enter(entry)
        self._walked.add(entry.id)
        noted = self.record.outcome(entry.id)
        block = self.block(entry)
        block.outcome, block.seconds, block.live = f"{noted['outcome']} earlier", noted["seconds"], False
        self.screen.show(block, compact=True)

    def replay_checks(self) -> None:
        """The check before the run, an apply's drift check, is its start: once it passed
        earlier, it takes one line and doesn't run again."""
        stage = [item for item in self.record.entries if part_of(item) == "before"]
        passed = [item for item in stage if self.record.finished(item.id) and item.id not in self._walked]
        if not passed:
            return
        names = list(dict.fromkeys(CHECKS[item.kind] for item in passed))
        named = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        seconds = sum(self.record.outcome(item.id).get("seconds") or 0 for item in passed)
        self.enter(passed[0])
        self.screen.done("checks", f"{len(passed)}/{len(stage)}", named, len(passed), seconds)
        self._walked.update(item.id for item in passed)

    def show_request(self, entry: Entry, block: Block) -> None:
        """A pull request: its repository and its address, or the whole step when it failed."""
        self.screen.blank()
        self.screen.print(Text(entry.repository, style="bold"))
        for line in block.lines:
            self.screen.print(line.text if line.label == "Pull Request:" else line)

    def block(self, entry: Entry) -> Block:
        if entry.kind == "preflight":
            return Block(
                "preflight", entry.preflight_counter, entry.component, entry.description, self.footer
            )
        if entry.kind == "drift":
            return Block("drift check", "", "", entry.description, self.footer)
        if entry.kind == "render":
            return Block("render step", "", "render", entry.description, self.footer)
        if entry.kind == "migrate":
            return Block("migrate step", "", "migrate", entry.description, self.footer)
        if entry.kind == "upgrade":
            return Block("upgrade step", "", "upgrade", entry.description, self.footer)
        if entry.kind == "pull-request":
            return Block("pull request", "", self.change_branch, entry.description, self.footer)
        if entry.kind == "mark":
            return Block("applied commit", "", entry.component, entry.description, self.footer)
        level = "step preview" if entry.kind == "preview" else "step"
        return Block(level, entry.step_counter, entry.step, entry.description, self.footer)

    @property
    def change_branch(self) -> str:
        return self.record.data.get("branch") or ""

    def finish(
        self,
        entry: Entry,
        block: Block,
        outcome: str,
        *,
        compact: bool = False,
        took: float | None = None,
        upload: bool = True,
        **detail,
    ) -> None:
        """Show and record an entry's outcome and time: its block's, or ``took``, the step's own
        time in a process that ran others; uploaded at once, except as the run stops."""
        block.finish(outcome)
        if took is not None:
            block.seconds = took
        real = detail.pop("real", True)
        if outcome == "applied":
            self.applied_change(entry, block, real=real)
        self.decision_resumed(entry, block, outcome)
        if block.waited >= 0.1:
            detail["waited"] = round(block.waited, 1)
        part_now = part_of(entry)
        if (
            self.record.kind in QUIET_RUNS
            and entry.kind in QUIET_KINDS
            and outcome in QUIET_OUTCOMES
            and compact
            and all(line.label == "Plan Skip:" for line in block.lines)
        ):
            self._quiet[part_now] = self._quiet.get(part_now, 0) + 1
        else:
            self._showing.setdefault(part_now, set()).add(entry.repository)
            if entry.kind == "pull-request":
                self.show_request(entry, block)
            else:
                self.screen.show(block, compact=compact and not block.lines)
        self.record.note(entry, outcome, block.elapsed(), upload=upload, details=block.details(), **detail)
        part, place, total = self.position(entry)
        self.screen.emit(
            {
                "event": "step",
                "run": self.record.id,
                "entry": entry.id,
                "kind": entry.kind,
                "part": PARTS[part],
                "position": place,
                "total": total,
                "repository": entry.repository or None,
                "component": entry.component or None,
                "phase": entry.phase or None,
                "step": entry.step or None,
                "outcome": outcome,
                "seconds": round(block.elapsed(), 1),
                **{key: value for key, value in detail.items() if value not in (None, [], {})},
            }
        )
