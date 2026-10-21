"""One screen for every run: a single column with no alignment and no indents.

Each level is named as it's entered, with its position: repository n of N, component n of N,
phase n of N and step n of N. A level's line holds the level, a cyan counter, the outcome and
the time; each labelled item then has a line of its own, blue label first. Each step is one
block, updated in place while it plans, waits and applies, and printed once when done. What a
resumed run finished earlier takes one line for the largest level wholly done. Colour
is only for meaning: green done, yellow a change, orange an external handoff, red a failure.
Nothing is faint. While a prompt waits for a key nothing redraws.

With `--json`, stdout carries one JSON object per line and the person's lines go to stderr,
plain; without a terminal the output is plain, and every prompt is a stop.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from collections.abc import Callable

from rich.console import Console, Group
from rich.live import Live
from rich.prompt import Prompt
from rich.spinner import Spinner
from rich.text import Text

from . import keys
from .outcome import WAITING_FOR

LABEL = "bold deep_sky_blue1"
LABEL_STYLES = {
    "Step Plan:": "bold yellow",
    "Plan File:": "bold yellow",
    "Step Handoff:": "bold orange1",
    "Handoff Page:": "bold orange1",
    "Still Waiting:": "bold orange1",
    "Optional Handoff:": "bold orange1",
    "Sign-In:": "bold orange1",
    "Values Needed:": "bold orange1",
    "Still Running:": "bold orange1",
    "Failure:": "bold red",
    "Drift:": "bold red",
    "Not Clean:": "bold red",
    "Never Published:": "bold yellow",
    "Problem:": "bold red",
}
DONE = (
    "applied",
    "no change",
    "passed",
    "no drift",
    "published cleanly",
    "approved",
    "converged",
    "complete",
)
CHANGE = ("change", "stopped", "pending")
WAITING = tuple(WAITING_FOR.values())
FAILURE = ("failed", "drift", "not converged", "not verified", "not published cleanly", "interrupted")
UNDER_WAY = ("planning", "applying", "checking", "verifying")
NAME_LABELS = {
    "repository": "Repo Name:",
    "component": "Component Name:",
    "phase": "Phase Name:",
    "step": "Step Name:",
    "preflight": "Component Name:",
    "migrate step": "Step Name:",
}
DESC_LABELS = {
    "repository": "Repo Desc:",
    "component": "Component Desc:",
    "phase": "Phase Desc:",
    "step": "Step Desc:",
    "preflight": "Step Desc:",
    "migrate step": "Step Desc:",
    "drift check": "Check Desc:",
    "published outputs": "Check Desc:",
}


def outcome_style(outcome: str) -> str:
    word = outcome.removesuffix(" earlier")
    if word in DONE:
        return "green"
    if word in CHANGE:
        return "yellow"
    if word in WAITING:
        return "orange1"
    if word in FAILURE:
        return "red"
    return "cyan" if word in UNDER_WAY else ""


def done_line(level: str, counter: str, name: str, steps: int, seconds: float) -> Text:
    """A level a resumed run finished earlier, on one line: its name, how many steps and their
    time."""
    return Text.assemble(
        (level, "bold"),
        (f" {counter}", "cyan"),
        (f" {name}", "bold"),
        (f"   {steps} step{'s' if steps != 1 else ''} done earlier", "green"),
        f"   {seconds:.1f} s",
    )


class Line:
    """A line of its own: an optional coloured label, then the value."""

    def __init__(self, text: str | Text, label: str = "", style: str | None = None):
        self.text = text if isinstance(text, Text) else Text(text)
        self.label = label
        self.style = style or LABEL_STYLES.get(label, LABEL)

    @property
    def plain(self) -> str:
        return self.text.plain

    def __rich__(self):
        return Text.assemble((f"{self.label} " if self.label else "", self.style), self.text)


def level_line(level: str, counter: str = "", outcome: str = "", seconds: float | None = None) -> Text:
    return Text.assemble(
        (level, "bold"),
        (f" {counter}" if counter else "", "cyan"),
        (f"   {outcome}" if outcome else "", outcome_style(outcome)),
        (f"   {seconds:.1f} s" if seconds is not None else ""),
    )


class Block:
    """One step's block: its level line, its name, then its details, updated in place
    until the step is done."""

    def __init__(self, level: str, counter: str, name: str, description: str, footer: Callable[[], str]):
        self.level, self.counter, self.name = level, counter, name
        self.description = description
        self.footer = footer
        self.outcome = "planning"
        self.message = ""
        self.lines: list[Line] = []
        self.started = time.monotonic()
        self.seconds: float | None = None
        self.live = True
        self.spinner = Spinner("dots", style="cyan")

    def elapsed(self) -> float:
        return self.seconds if self.seconds is not None else time.monotonic() - self.started

    def finish(self, outcome: str) -> None:
        self.outcome, self.seconds, self.live = outcome, time.monotonic() - self.started, False

    def add(self, label: str, text: str | Text, style: str | None = None) -> Line:
        line = Line(text, label, style)
        self.lines.append(line)
        return line

    def renderables(self, compact: bool) -> list:
        parts = [level_line(self.level, self.counter, self.outcome, self.elapsed())]
        if self.name:
            parts.append(Line(Text(self.name, style="bold"), NAME_LABELS.get(self.level, "")))
        if compact:
            return parts
        if self.description:
            parts.append(Line(self.description, DESC_LABELS.get(self.level, "Desc:")))
        parts.extend(self.lines)
        if self.live:
            parts.append(Text(""))
            if self.message:
                parts.append(Line(Text(self.message, style="cyan")))
            parts.append(Text.assemble(self.spinner.render(time.monotonic()), " ", (self.footer(), "italic")))
        return parts

    def details(self) -> list[list[str]]:
        return [[line.label, line.plain] for line in self.lines]

    def __rich__(self):
        return Group(*self.renderables(compact=False))


class Screen:
    def __init__(self, *, json_mode: bool, interactive: bool):
        self.json = json_mode
        self.interactive = interactive and not json_mode
        if self.interactive:
            # The width is taken once, at the start; nothing depends on it changing later.
            width = shutil.get_terminal_size((100, 40)).columns
            self.console = Console(width=width, highlight=False)
        else:
            self.console = Console(
                file=sys.stderr if json_mode else sys.stdout,
                no_color=True,
                highlight=False,
                soft_wrap=True,
                emoji=False,
                force_terminal=False,
            )
        self._blanks = 0
        self._done = False

    # Output -----------------------------------------------------------------------------

    def emit(self, event: dict) -> None:
        if self.json:
            sys.stdout.write(json.dumps(event, sort_keys=True) + "\n")
            sys.stdout.flush()

    def print(self, *parts) -> None:
        """Print lines for good: unwrapped, so the terminal wraps them and nothing is cut. Only
        a live block, which is redrawn, is wrapped at the width taken at the start."""
        for part in parts:
            self.console.print(part, soft_wrap=True)
            self._blanks = 0
        self._done = False

    def blank(self, count: int = 1) -> None:
        """Leave ``count`` blank lines, counting any just printed, so gaps never stack."""
        for _ in range(count - self._blanks):
            self.console.print()
        self._blanks = max(count, self._blanks)

    def labelled(self, label: str, text: str | Text, style: str | None = None) -> None:
        self.print(Line(text, label, style))

    def header(self, goal: str, *rows: tuple[str, str]) -> None:
        self.blank()
        self.print(Text("dp-instance", style="bold"))
        self.print(Text(goal, style="bold cyan"))
        for label, value in rows:
            self.print(Text.assemble((f"{label} ", "bold"), value))

    def stage(self, title: str) -> None:
        self.blank(2)
        self.print(Text(title, style="bold underline"))
        self.blank()

    def level(self, level: str, counter: str = "", outcome: str = "", seconds: float | None = None) -> None:
        self.print(level_line(level, counter, outcome, seconds))

    def note(self, text: str, style: str = "") -> None:
        self.print(Text(text, style=style))

    def done(self, level: str, counter: str, name: str, steps: int, seconds: float) -> None:
        """A level a resumed run finished earlier; consecutive ones stand together."""
        if not self._done:
            self.blank()
        self.print(done_line(level, counter, name, steps, seconds))
        self._done = True

    # Blocks -----------------------------------------------------------------------------

    def run_live(self, block: Block, work, *arguments, **keywords):
        """Run ``work(*arguments, **keywords)`` while the block shows live; at a terminal only."""
        if not self.interactive:
            return work(*arguments, **keywords)
        with Live(block, console=self.console, refresh_per_second=8, transient=True):
            return work(*arguments, **keywords)

    def ask(self, block: Block, choices: list[tuple[str, str]], prompt: str = "") -> str:
        """Wait for a key with the block drawn once and nothing redrawing, so the key
        arrives at once."""
        block.seconds, block.live = block.elapsed(), False
        parts = []
        for key, label in choices:
            parts += [(key, "bold"), f" {label}    "]
        block.lines += [Line(""), Line(Text.assemble(prompt + ("   " if prompt else ""), *parts))]
        try:
            with Live(block, console=self.console, auto_refresh=False, transient=True) as live:
                live.refresh()
                key = keys.choose("".join(key for key, _ in choices))
        finally:
            del block.lines[-2:]
            block.seconds, block.live = None, True
        return key

    def show(self, block: Block, compact: bool = False) -> None:
        for part in block.renderables(compact=compact):
            self.print(part)
        if not compact:
            self.blank()

    def choose(self, choices: list[tuple[str, str]], prompt: str = "") -> str:
        """A keypress outside a block, such as status's offer to resume."""
        parts = [prompt + "   "] if prompt else []
        for key, label in choices:
            parts += [(key, "bold"), f" {label}    "]
        self.blank()
        self.print(Text.assemble(*parts))
        return keys.choose("".join(key for key, _ in choices))

    def secret(self, label: str) -> str:
        """A hidden prompt: what's typed is never shown."""
        return self._ask(label, hidden=True)

    def text(self, label: str) -> str:
        """A prompt that shows what's typed: a file's path, or a value that isn't secret."""
        return self._ask(label, hidden=False)

    def _ask(self, label: str, *, hidden: bool) -> str:
        value = Prompt.ask(Text(label, style=LABEL), console=self.console, password=hidden)
        self._blanks = 0
        return value

    # Endings ----------------------------------------------------------------------------

    def next_block(self, steps, record: str | None = None) -> None:
        self.blank(2)
        self.print(Text("Next", style="bold"))
        for item in steps:
            self.blank()
            self.print(Text(item.command, style="bold"))
            self.print(Text(item.why))
        if record:
            self.blank()
            self.print(Text(f"Run record: {record}", style="italic"))

    def complete(self, seconds: float, record: str | None) -> None:
        self.blank(2)
        self.print(
            Text.assemble(
                ("Complete", "bold green"),
                f"   in {seconds:.0f} s. Every changed step passed its verify, and every other "
                "step planned no change.",
            )
        )
        if record:
            self.blank()
            self.print(Text(f"Run record: {record}", style="italic"))
