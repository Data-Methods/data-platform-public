"""One screen for every run: a single column with no alignment and no indents.

Every command opens with `dp-instance`, then a line each for its command, its instance and
what else it knows, each as it becomes known; ends each of its parts with a line of its time,
and ends with its total, as its JSON events and result give them too. At a terminal one spinner
shows from the moment the command starts: each section takes it over and says what it does, a
live block replaces it while it shows and it returns once the block ends, and a question or a
stop ends it until it's answered or for good. A prepare's parts are
Repository Changes, Platform Preview and Pull Requests, an apply's Drift Check and Platform
Changes, and `preflight`'s Preflight, each named as it starts; a level's lines wait for a step to
show under them, so a level with nothing to show takes no line. Each level is named as it's
entered, with its position: repository n of N, component n of N, phase n of N and step n of N. A
level's line holds the
level, a cyan counter, the outcome and the time, read as 4.2s, 1m 50s or 1h 2m 5s; each
labelled item then has a line of its own, blue label first. Each step is one block, updated in
place while it plans, waits and applies, and printed once when done; under it, the part, its
count and what's ahead. What a resumed run finished earlier takes one line for the largest
level wholly done. Colour
is only for meaning: green done, yellow a change, orange an external handoff, red a failure.
Nothing is faint. While a prompt waits for a key nothing redraws. Every time is wall time, and
the time a prompt waits for a person's answer is kept apart from it. A live block is drawn at
the window's width as it is, so a resized window redraws it cleanly.

With `--json`, stdout carries one JSON object per line and the person's lines go to stderr,
plain; without a terminal the output is plain, and every prompt is a stop.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.control import Control
from rich.live import Live
from rich.live_render import LiveRender
from rich.prompt import Prompt
from rich.segment import ControlType, Segment
from rich.spinner import Spinner
from rich.style import Style
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
    "Waiting on required handoff:": "bold orange1",
    "Recommended optional handoff:": "bold orange1",
    "Sign-In:": "bold orange1",
    "Values Needed:": "bold orange1",
    "Still Running:": "bold orange1",
    "Failure:": "bold red",
    "Drift:": "bold red",
    "Unapplied:": "bold red",
    "Problem:": "bold red",
}
DONE = (
    "applied",
    "no change",
    "passed",
    "no drift",
    "approved",
    "converged",
    "complete",
    "standing by",
)
CHANGE = ("change", "stopped", "pending")
WAITING = tuple(WAITING_FOR.values())
FAILURE = (
    "failed",
    "drift",
    "unapplied",
    "not converged",
    "not verified",
    "interrupted",
)
UNDER_WAY = ("planning", "applying", "checking", "verifying", "holding")
# What the spinner says while the command works with nothing else showing.
WORKING = "Working"
NAME_LABELS = {
    "repository": "Repo Name:",
    "component": "Component Name:",
    "phase": "Phase Name:",
    "step": "Step Name:",
    "preflight": "Component Name:",
    "migrate step": "Step Name:",
    "upgrade step": "Step Name:",
    "step preview": "Step Name:",
    "render step": "Step Name:",
    "pull request": "Branch:",
    "applied commit": "Component Name:",
}
DESC_LABELS = {
    "repository": "Repo Desc:",
    "component": "Component Desc:",
    "phase": "Phase Desc:",
    "step": "Step Desc:",
    "preflight": "Step Desc:",
    "migrate step": "Step Desc:",
    "upgrade step": "Step Desc:",
    "step preview": "Step Desc:",
    "render step": "Step Desc:",
    "pull request": "Desc:",
    "applied commit": "Desc:",
    "drift check": "Check Desc:",
}


def duration(seconds: float) -> str:
    """A time as a person reads it, the same in every tool: 4.2s, 1m 50s or 1h 2m 5s."""
    if round(seconds, 1) < 60:
        return f"{seconds:.1f}s"
    hours, rest = divmod(round(seconds), 3600)
    minutes, rest = divmod(rest, 60)
    return f"{hours}h {minutes}m {rest}s" if hours else f"{minutes}m {rest}s"


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
        f"   {duration(seconds)}",
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


def link(text: str, url: str) -> Text:
    """Text a terminal that can makes clickable; any other shows it as it is."""
    return Text(text, style=Style(link=url))


def level_line(level: str, counter: str = "", outcome: str = "", seconds: float | None = None) -> Text:
    return Text.assemble(
        (level, "bold"),
        (f" {counter}" if counter else "", "cyan"),
        (f"   {outcome}" if outcome else "", outcome_style(outcome)),
        (f"   {duration(seconds)}" if seconds is not None else ""),
    )


class Reflowed(LiveRender):
    """A live block's render that knows the length of each line it drew, so once the window
    narrows and the terminal rewraps those lines, the next frame first erases every row they now
    take, not only as many as it drew."""

    def __init__(self, renderable: RenderableType, console: Console, **options):
        super().__init__(renderable, **options)
        self.console = console
        self.lengths: list[int] = []

    def rows(self) -> int:
        """The rows the last frame takes at the window's width now."""
        width = max(self.console.size.width, 1)
        return sum(max(1, -(-length // width)) for length in self.lengths) or 1

    def position_cursor(self) -> Control:
        if self._shape is None:
            return Control()
        return Control(
            ControlType.CARRIAGE_RETURN,
            (ControlType.ERASE_IN_LINE, 2),
            *(((ControlType.CURSOR_UP, 1), (ControlType.ERASE_IN_LINE, 2)) * (self.rows() - 1)),
        )

    def restore_cursor(self) -> Control:
        if self._shape is None:
            return Control()
        return Control(
            ControlType.CARRIAGE_RETURN,
            *((ControlType.CURSOR_UP, 1), (ControlType.ERASE_IN_LINE, 2)) * self.rows(),
        )

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        segments = list(super().__rich_console__(console, options))
        self.lengths = [Segment.get_line_length(line) for line in Segment.split_lines(segments)]
        yield from segments


class Redrawn(Live):
    """Rich's live display with a render that redraws cleanly after the window is resized."""

    def __init__(self, renderable: RenderableType, *, console: Console, **options):
        super().__init__(renderable, console=console, **options)
        self._live_render = Reflowed(self.get_renderable(), console, vertical_overflow=self.vertical_overflow)


class Block:
    """One step's block: its level line, its name, then its details, updated in place
    until the step is done. Its time leaves out the time a prompt waited for an answer."""

    def __init__(self, level: str, counter: str, name: str, description: str, footer: Callable[[], str]):
        self.level, self.counter, self.name = level, counter, name
        self.description = description
        self.footer = footer
        self.outcome = "planning"
        self.message = ""
        self.lines: list[Line] = []
        self.started = time.monotonic()
        self.seconds: float | None = None
        # The time a prompt waited for a person's answer, which the step's own time leaves out.
        self.waited = 0.0
        self.live = True
        self.spinner = Spinner("dots", style="cyan")

    def elapsed(self) -> float:
        return self.seconds if self.seconds is not None else time.monotonic() - self.started - self.waited

    def finish(self, outcome: str) -> None:
        self.outcome, self.seconds, self.live = outcome, time.monotonic() - self.started - self.waited, False

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
            # The width is read again at every draw, so a live block fits a resized window.
            self.console = Console(highlight=False)
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
        # The command's parts, each with its time, and the one under way, with its start and the
        # time waited for answers by then.
        self.started = time.monotonic()
        self.parts: dict[str, float] = {}
        self._part: tuple[str, float, float, bool] | None = None
        # The time the command's prompts waited for a person's answer.
        self.waited = 0.0
        # The opening lines shown, each once.
        self._opened = False
        self._rows: set[str] = set()
        # The spinner while the command works with nothing else showing, and what it says.
        self._status = None
        self._said = WORKING
        # Each level's lines, until a step shows under them.
        self._held: dict[str, list[tuple[str, tuple]]] = {}
        self._holding: str | None = None

    # Timing -----------------------------------------------------------------------------

    def begin(self, title: str, *, shown: bool = True) -> None:
        """Start a part of the command, ending the one under way; one not ``shown`` keeps its
        time in the command's parts and takes no line."""
        self.end()
        self._part = (title, time.monotonic(), self.waited, shown)

    def reading(self, title: str) -> None:
        """What the command reads before its first part: a spinner saying so, at a terminal, until
        it asks something, shows a step or ends."""
        self.begin(title, shown=False)
        self.working(title)

    def working(self, title: str | None = None) -> None:
        """At a terminal, the spinner, until the command asks something, shows a step or ends:
        started, saying ``title`` or `Working`, when it isn't showing; when it is, it says
        ``title``, or goes on saying what it said."""
        if not self.interactive:
            return
        if self._status is None:
            self._said = title or WORKING
            self._status = self.console.status(Text(self._said), spinner="dots", spinner_style="cyan")
            self._status.start()
        elif title is not None:
            self._said = title
            self._status.update(Text(title))

    @contextmanager
    def busy(self, title: str):
        """The spinner saying ``title`` while the work inside runs, at a terminal, then as it was:
        saying what it said before, or gone."""
        before = self._said if self._status is not None else None
        self.working(title)
        try:
            yield
        finally:
            if before is not None:
                self.working(before)
            else:
                self._stop()

    def _stop(self) -> None:
        """The spinner goes."""
        if self._status is not None:
            self._status.stop()
            self._status = None

    def read(self) -> None:
        """The reading is over: the spinner goes."""
        self._stop()
        if self._part is not None and not self._part[3]:
            self.end()

    def end(self, steps: int | None = None) -> None:
        """End the part under way, if any, with a line of its time, leaving out the time its
        prompts waited for an answer."""
        if self._part is not None:
            title, started, waited, shown = self._part
            self._part = None
            self.ended(title, time.monotonic() - started - (self.waited - waited), steps=steps, shown=shown)

    def waiting(self, started: float) -> float:
        """Count the time since ``started`` as waiting for an answer; how long it was."""
        seconds = time.monotonic() - started
        self.waited += seconds
        return seconds

    def ended(
        self, title: str, seconds: float, *, steps: int | None = None, shown: bool = True, **fields
    ) -> None:
        """A part of the command, done: its line, its event and its place in the command's
        parts."""
        self.parts[title] = round(seconds, 1)
        if shown:
            self.part(title, steps, seconds)
        self.emit(
            {
                "event": "part",
                **fields,
                "part": title,
                **({"steps": steps} if steps is not None else {}),
                "seconds": round(seconds, 1),
            }
        )

    def total(self) -> dict:
        """End the part under way and show the command's wall time, and how much of it its
        prompts waited for an answer; its parts and total, as its result gives them."""
        self.read()
        self.end()
        seconds = time.monotonic() - self.started
        waited = round(self.waited, 1)
        self.blank()
        self.print(
            Text.assemble(
                ("Took", "bold"),
                f" {duration(seconds)}",
                f", {duration(waited)} of it waiting for answers" if waited else "",
            )
        )
        return {
            "seconds": round(seconds, 1),
            "parts": dict(self.parts),
            **({"waiting": waited} if waited else {}),
        }

    # Output -----------------------------------------------------------------------------

    def emit(self, event: dict) -> None:
        if self.json:
            sys.stdout.write(json.dumps(event, sort_keys=True) + "\n")
            sys.stdout.flush()

    def print(self, *parts) -> None:
        """Print lines for good: unwrapped, so the terminal wraps them and nothing is cut. Only
        a live block, which is redrawn, is wrapped at the width taken at the start."""
        if self._holding is not None:
            self._held[self._holding].append(("print", parts))
            return
        self.release_held()
        for part in parts:
            self.console.print(part, soft_wrap=True)
            self._blanks = 0
        self._done = False

    def blank(self, count: int = 1) -> None:
        """Leave ``count`` blank lines, counting any just printed, so gaps never stack."""
        if self._holding is not None:
            self._held[self._holding].append(("blank", (count,)))
            return
        for _ in range(count - self._blanks):
            self.console.print()
        self._blanks = max(count, self._blanks)

    @contextmanager
    def holding(self, level: str):
        """Keep what's printed meanwhile, a level's lines, until a step shows under them."""
        self._holding = level
        self._held.setdefault(level, [])
        try:
            yield
        finally:
            self._holding = None

    def drop_held(self, *levels: str) -> None:
        """Forget the lines of ``levels``, every level by default: nothing showed under them."""
        for level in levels or tuple(self._held):
            self._held.pop(level, None)

    def release_held(self) -> None:
        """Show what's held, the largest level first, as it was printed."""
        held, self._held = self._held, {}
        for lines in held.values():
            for kind, arguments in lines:
                getattr(self, kind)(*arguments)

    def labelled(self, label: str, text: str | Text, style: str | None = None) -> None:
        self.print(Line(text, label, style))

    def header(self, command: str, *rows: tuple[str, str]) -> None:
        """The lines a command opens with: `dp-instance`, `Command: status`, then each row as its
        label and value, each once, so what's known later follows when it's known."""
        if not self._opened:
            self._opened = True
            self.blank()
            self.print(Text("dp-instance", style="bold"))
            self.blank()
            self.labelled("Command:", command)
        for label, value in rows:
            if label not in self._rows:
                self._rows.add(label)
                self.labelled(f"{label[:1].upper()}{label[1:]}:", value)

    def stage(self, title: str) -> None:
        """A part's heading, and the spinner while the part sets up, until its first step shows."""
        self.blank(2)
        self.print(Text(title, style="bold underline"))
        self.blank()
        self.working()

    def level(self, level: str, counter: str = "", outcome: str = "", seconds: float | None = None) -> None:
        self.print(level_line(level, counter, outcome, seconds))

    def note(self, text: str, style: str = "") -> None:
        self.print(Text(text, style=style))

    def timed(self, label: str, text: str, seconds: float) -> None:
        """Work with no step of its own and its time, such as reading the steps."""
        self.print(Text.assemble((label, "bold"), f"   {text}", f"   {duration(seconds)}"))

    def part(self, title: str, steps: int | None, seconds: float) -> None:
        """A part of a command, done: how many of its entries, when it counts them, and its time."""
        self.blank()
        done = f"   {steps} done" if steps is not None else "   done"
        self.print(Text.assemble((title, "bold"), (done, "green"), f"   {duration(seconds)}"))
        self._done = False

    def done(self, level: str, counter: str, name: str, steps: int, seconds: float) -> None:
        """A level a resumed run finished earlier; consecutive ones stand together."""
        if not self._done:
            self.blank()
        self.print(done_line(level, counter, name, steps, seconds))
        self._done = True

    # Blocks -----------------------------------------------------------------------------

    def run_live(self, block: Block, work, *arguments, **keywords):
        """Run ``work(*arguments, **keywords)`` while the block shows live, at a terminal, in the
        spinner's place; the spinner shows again once the block ends."""
        self.read()
        if not self.interactive:
            return work(*arguments, **keywords)
        try:
            with Redrawn(block, console=self.console, refresh_per_second=8, transient=True):
                return work(*arguments, **keywords)
        finally:
            self.working()

    def ask(self, block: Block, choices: list[tuple[str, str]], prompt: str = "") -> str:
        """Wait for a key with the block drawn once and nothing redrawing, so the key
        arrives at once. The wait counts in neither the block's time nor the command's working
        time; the spinner shows again once it's answered."""
        self.read()
        self.release_held()
        block.seconds, block.live = block.elapsed(), False
        parts = []
        for key, label in choices:
            parts += [(key, "bold"), f" {label}    "]
        block.lines += [Line(""), Line(Text.assemble(prompt + ("   " if prompt else ""), *parts))]
        started = time.monotonic()
        try:
            with Redrawn(block, console=self.console, auto_refresh=False, transient=True) as live:
                live.refresh()
                key = keys.choose("".join(key for key, _ in choices))
        finally:
            del block.lines[-2:]
            block.waited += self.waiting(started)
            block.seconds, block.live = None, True
        self.working()
        return key

    def show(self, block: Block, compact: bool = False) -> None:
        for part in block.renderables(compact=compact):
            self.print(part)
        if not compact:
            self.blank()

    def choose(self, choices: list[tuple[str, str]], prompt: str = "") -> str:
        """A keypress outside a block, such as setup's question before it writes the map."""
        self.read()
        parts = [prompt + "   "] if prompt else []
        for key, label in choices:
            parts += [(key, "bold"), f" {label}    "]
        self.blank()
        self.print(Text.assemble(*parts))
        return self._answered(lambda: keys.choose("".join(key for key, _ in choices)))

    def menu(self, title: str, choices: list[tuple[str, str]]) -> str:
        """A keypress among choices, one a line, under what's being asked."""
        self.read()
        self.blank()
        self.print(Text(title, style="bold"))
        for key, label in choices:
            self.print(Text.assemble("  ", (key, "bold"), f"  {label}"))
        return self._answered(lambda: keys.choose("".join(key for key, _ in choices)))

    def secret(self, label: str) -> str:
        """A hidden prompt: what's typed is never shown."""
        return self._ask(label, hidden=True)

    def text(self, label: str) -> str:
        """A prompt that shows what's typed: a file's path, or a value that isn't secret."""
        return self._ask(label, hidden=False)

    def _ask(self, label: str, *, hidden: bool) -> str:
        self.read()
        value = self._answered(
            lambda: Prompt.ask(Text(label, style=LABEL), console=self.console, password=hidden)
        )
        self._blanks = 0
        return value

    def _answered(self, question: Callable[[], str]) -> str:
        """A person's answer to ``question``, its wait counted apart; the spinner shows again
        once it's answered."""
        started = time.monotonic()
        try:
            answer = question()
        finally:
            self.waiting(started)
        self.working()
        return answer

    # Endings ----------------------------------------------------------------------------

    def continuing(self, steps, record: str | None = None) -> None:
        """How to carry on from a stop, a line each."""
        self.blank()
        for item in steps:
            self.labelled("To Continue:", f"{item.command}: {item.why}")
        if record:
            self.run_record(record)

    def then(self, sentence: str, record: str | None = None) -> None:
        """What a person does after a run that succeeded, in a sentence."""
        self.blank()
        self.print(Text(sentence))
        if record:
            self.run_record(record)

    def run_record(self, record: str) -> None:
        self.blank()
        self.print(Text(f"Run record: {record}", style="italic"))

    def complete(self, seconds: float, waited: float, said: str = "") -> None:
        """A run's end: its wall time from its start, and how much of it the run waited, at a
        stop until a resume or at a question, apart from the time it worked, and what it says."""
        self.blank(2)
        took = (
            f"in {duration(seconds)}: {duration(seconds - waited)} working and {duration(waited)} waiting"
            if waited
            else f"in {duration(seconds)}"
        )
        self.print(
            Text.assemble(
                ("Complete", "bold green"),
                f"   {took}." + (f" {said}" if said else ""),
            )
        )
