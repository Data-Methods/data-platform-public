"""What a step waits on: outside work, values only a person supplies, a sign-in or running
work; its failure; and the optional outside work a run recommends."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text

from .. import handoffs, prompts
from ..components import Call, Cli
from ..outcome import WAITING_FOR, WAITS, Next, Stop
from ..screen import Block
from ..sequence import Entry
from .base import Base
from .common import by_system


class Waits(Base):
    def handoff(self, entry: Entry, block: Block, call: Call, cli: Cli) -> None:
        handoff = (call.result or {}).get("handoff") or {}
        actions = [item for item in handoff.get("actions") or [] if isinstance(item, dict)]
        work = [str(item) for item in handoff.get("work") or []]
        pages = self.pages(actions)
        block.outcome = "waiting: handoff"
        block.lines = [line for line in block.lines if line.label == "Pull Request:"]
        self.end_plan_skip(block, f"{entry.where} waited on outside work")
        for system, count in by_system(actions).items():
            block.add(
                "Waiting on required handoff:",
                Text.assemble(
                    f"{handoffs.system_name(system)}: {count} item{'s' if count != 1 else ''}, ",
                    handoffs.page_link(self.relative(pages[system]), pages[system]),
                ),
            )
        for line in work:
            block.add("Step Handoff:", line)
        detail = {
            "entry": entry.id,
            "actions": [action.get("title") for action in actions] + work,
            "pages": [self.relative(path) for path in pages.values()],
        }
        stop = Stop(
            "handoff",
            f"{entry.where} waits on outside work.",
            reason="handoff",
            handoff=detail,
            next=(
                Next(
                    "dp-instance resume", f"checks {entry.where} again and carries on once the work is done"
                ),
                *self.again(),
            ),
        )
        if not self.session.interactive:
            raise stop
        while True:
            key = self.screen.ask(block, [("c", "check again"), ("s", "stop the run here")])
            if key != "c":
                raise stop
            block.outcome = "checking"
            check = self.screen.run_live(
                block,
                cli.run,
                "plan",
                "--phase",
                entry.phase,
                "--step",
                entry.step,
                "--json",
                *self.version,
                on_event=self.pull_request_events(block),
            )
            if check.status != "done":
                if check.status in WAITS:
                    self.wait(entry, block, check)
                self.failure(entry, block, check)
            finished = check.finished(entry.step)
            block.lines = [line for line in block.lines if line.label != "Still Waiting:"]
            if finished and finished[-1].get("status") == "pending":
                block.outcome = "waiting: handoff"
                outstanding = [
                    item.get("title", item.get("id"))
                    for item in finished[-1].get("handoffs", [])
                    if item.get("required")
                ]
                block.add("Still Waiting:", "; ".join(outstanding) or "the work isn't observed done yet")
                continue
            block.add("Handoff Done:", "the work is observed done; the step goes on")
            return

    def supply(self, entry: Entry, block: Block, call: Call) -> dict[str, dict[str, str]]:
        """Show the step that asks, as a block of its own naming the handoff pages that say how
        to get the values, then ask for each value."""
        self.value_pages(block, call)
        block.outcome, block.seconds, block.live = WAITING_FOR["values"], block.elapsed(), False
        self.screen.show(block)
        block.seconds, block.live = None, True
        self.screen.note(
            f"{entry.step} needs values only a person supplies. They go to {entry.command} alone, "
            "and nowhere else."
        )
        started = self.screen.waited
        values = prompts.supply(self.screen, call.result["values"]["supply"])
        block.waited += self.screen.waited - started
        self.screen.blank()
        return values

    def value_pages(self, block: Block, call: Call) -> list[str]:
        """Write the handoff sections a values stop carries, the step's own, which say how to
        get the values, as handoff pages, and name each page on the step's block. A component
        whose engine predates the sections sends none."""
        values = (call.result or {}).get("values") or {}
        sections = [item for item in values.get("sections") or [] if isinstance(item, dict)]
        if not sections:
            return []
        systems = {item.get("system", "other") for item in sections}
        pages = [self.relative(path) for system, path in self.pages(sections).items() if system in systems]
        shown = {line.plain for line in block.lines if line.label == "Handoff Page:"}
        for page in pages:
            if page not in shown:
                block.add("Handoff Page:", page)
        return pages

    def wait(self, entry: Entry, block: Block, call: Call) -> None:
        """A component's stop for values, a sign-in, runs still going or a step still pending
        stops the run for the same, naming what it waits for and the commands that continue."""
        if call.status in ("pending", "runs"):
            self.end_plan_skip(block, f"{entry.where} waited on outside work")
        if call.status == "pending":
            waits_for = ((call.result or {}).get("pending") or {}).get("message") or call.message
            block.add("Still Waiting:", waits_for)
            raise Stop(
                "pending",
                f"{entry.where} still waits: {waits_for}",
                reason="pending",
                pending={"entry": entry.id, "message": waits_for},
                next=(
                    Next("dp-instance resume", f"checks {entry.where} again and carries on once it's ready"),
                ),
            )
        if call.status == "values":
            supply = ((call.result or {}).get("values") or {}).get("supply") or []
            for secret in supply:
                fields = ", ".join(field.get("name", "") for field in secret.get("fields", []))
                about = f": {secret['description']}" if secret.get("description") else ""
                block.add("Values Needed:", f"{secret.get('secret')} ({fields}){about}")
            pages = self.value_pages(block, call)
            raise Stop(
                "values",
                f"{entry.where} needs values only a person supplies.",
                reason="values",
                values={"entry": entry.id, "supply": supply, "pages": pages},
                next=(
                    Next(
                        "dp-instance resume",
                        "run by a person at a terminal: asks for each value with a hidden prompt and "
                        "hands it to the component alone; values never pass through an agent",
                    ),
                ),
            )
        if call.status == "sign-in":
            block.add("Sign-In:", call.message)
            renew = self.sign_in_next(call)
            raise Stop(
                "sign-in",
                call.message,
                reason="sign-in",
                sign_in={"entry": entry.id, "command": renew[0].command},
                next=(*renew, Next("dp-instance resume", f"carries on from {entry.where} once signed in")),
            )
        running = [str(item) for item in ((call.result or {}).get("runs") or {}).get("running") or []]
        for item in running:
            block.add("Still Running:", item)
        raise Stop(
            "runs",
            call.message,
            reason="runs",
            runs={"entry": entry.id, "running": running},
            next=(
                Next("dp-instance resume", f"waits again at {entry.where}, then carries on once they finish"),
            ),
        )

    def sign_in_next(self, call: Call) -> tuple[Next, ...]:
        """The command that renews a component's sign-in, when it stopped for one."""
        if call.status != "sign-in":
            return ()
        command = ((call.result or {}).get("sign_in") or {}).get("command") or "az login"
        return (Next(command, "renews the sign-in, which only you can do"),)

    def excerpt(self, block: Block, call: Call) -> None:
        message = call.message
        for line in call.excerpt(12):
            if line.strip() != message.strip():
                block.add("", Text(line, style="red"))
        block.add("Log:", self.relative(self.record.log_path))

    def failure(self, entry: Entry, block: Block, call: Call) -> None:
        """A step's failure stops the run: a read that looked temporary for a resume once the
        service answers."""
        block.add("Failure:", call.message)
        self.excerpt(block, call)
        raise Stop(
            "failed",
            f"{entry.where} failed: {call.message}",
            reason="transient" if call.temporary is not None else "failed",
            next=(
                Next(
                    "dp-instance resume",
                    "starts the prepare again from its start, with the commits it took, once the failure "
                    "is fixed"
                    if self.record.kind == "prepare"
                    else f"carries on from {entry.where} once the failure is fixed",
                ),
                *self.again(),
            ),
        )

    def optional(self, event: dict) -> None:
        """Optional outside work never stops anything: it's recorded whenever its step plans,
        never shown on a stop, and recommended once, at the end of a completed run."""
        seen = self.record.data.setdefault("optional", {})
        for item in event.get("handoffs") or []:
            if isinstance(item, dict) and not item.get("required"):
                seen[item.get("id", item.get("title"))] = item

    def recommend(self) -> list[dict]:
        """The optional outside work a completed run recorded, on one line: each system with
        how many pieces it holds, and the folder of their pages, which say what and why. For
        --json, the same systems, each with its page and its pieces."""
        optional = list(self.record.data.get("optional", {}).values())
        if not optional:
            return []
        pages = self.pages([])
        counted = by_system(optional)
        handoffs.show_optional(
            self.screen,
            {
                system: (count, self.relative(pages[system]), pages[system])
                for system, count in counted.items()
            },
        )
        return [
            {
                "system": system,
                "name": handoffs.system_name(system),
                "required": False,
                "page": self.relative(pages[system]),
                "items": [
                    {"id": item.get("id"), "title": item.get("title")}
                    for item in sorted(optional, key=lambda item: item.get("id", ""))
                    if item.get("system", "other") == system
                ],
            }
            for system in counted
        ]

    def pages(self, sections: list[dict]) -> dict[str, Path]:
        if self._asked_by is None:
            self._asked_by = handoffs.asker()
        everything = [*sections, *self.record.data.get("optional", {}).values()]
        return handoffs.write_pages(
            self.record.folder / "handoffs", everything, self.instance.key, self._asked_by
        )
