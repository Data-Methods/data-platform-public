"""Running a component's CLI as its own process, through the engine's common interface.

Each component runs as `uv run --locked <command>` in its own checkout, so it keeps its own
engine version. With `--json` it writes one JSON object per line to stdout, one line as each
step starts and finishes and a result last, and everything meant for a person to stderr,
which goes to the run's log. Values a person supplies go to its stdin as one JSON document
and nowhere else. A command that only reads runs again when it fails for a moment; one that
writes never does.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import processes, transient
from .outcome import EXIT_CODES, USAGE, Stop, failed

WINDOWS_FILE_LOCK = "os error 1224"
# The commands that only read, which a failure that looks temporary runs again.
READS = (("steps",), ("outputs", "aggregate"), ("preflight",), ("plan",), ("verify",))


def read_command(arguments) -> tuple[str, ...] | None:
    """The read-only command ``arguments`` run, if they run one."""
    return next((words for words in READS if tuple(arguments[: len(words)]) == words), None)


def uv_run(command: str, project: Path | None) -> list[str]:
    """The launcher: `uv run --locked`, from the checkout's own project or from the target's
    fetched template source."""
    launcher = [processes.executable("uv"), "run", "--locked"]
    if project is not None:
        launcher += ["--project", str(project)]
    return [*launcher, command]


class Log:
    """A run's full log: every command, and every line it wrote. Never what it was given."""

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()

    def write(self, line: str) -> None:
        if self.path is None:
            return
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(line.rstrip("\n") + "\n")

    def command(self, cwd: Path, argv: list[str]) -> None:
        self.write(f"== {datetime.now().isoformat(timespec='seconds')} {cwd}$ {' '.join(argv)}")


@dataclass
class Call:
    """One component process: its result, the events it wrote and its human lines. A read
    whose last failure looked temporary holds what it looked like and how often it ran again."""

    status: str
    code: int
    result: dict | None
    events: list[dict] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    stdout: str = ""
    seconds: float = 0.0
    temporary: str | None = None
    retried: int = 0

    def finished(self, step: str | None = None) -> list[dict]:
        return [
            event
            for event in self.events
            if event.get("event") == "step"
            and event.get("state") == "finished"
            and (step is None or event.get("step") == step)
        ]

    def excerpt(self, lines: int = 15) -> list[str]:
        return [line for line in self.stderr if line.strip()][-lines:]

    @property
    def message(self) -> str:
        if self.result and self.result.get("message"):
            text = str(self.result["message"])
        else:
            last = self.excerpt(1)
            text = last[0] if last else f"exit code {self.code}"
        return transient.exhausted(text, self.temporary, self.retried)


class StepReports:
    """What each step of a process that runs several, such as a phase's `verify`, reports as it
    happens, by phase and step: its own time, from its started event's arrival to its finished
    event's, as the component writes each at once, and each pull request's last state."""

    def __init__(self):
        self._started: dict[tuple[str, str], float] = {}
        self.seconds: dict[tuple[str, str], float] = {}
        self.pull_requests: dict[tuple[str, str], dict[str, str | None]] = {}

    def __call__(self, event: dict) -> None:
        key = (event.get("phase"), event.get("step"))
        if event.get("event") == "pull_request" and event.get("url"):
            self.pull_requests.setdefault(key, {})[event["url"]] = event.get("state")
        elif event.get("event") == "step" and event.get("state") == "started":
            self._started[key] = time.monotonic()
        elif event.get("event") == "step" and event.get("state") == "finished" and key in self._started:
            self.seconds[key] = time.monotonic() - self._started[key]


@dataclass(frozen=True)
class Cli:
    """A component's CLI in its checkout; ``project`` runs another source's CLI against it."""

    command: str
    checkout: Path
    log: Log
    project: Path | None = None

    def argv(self, *arguments: str) -> list[str]:
        repository = ["--repo", str(self.checkout)] if self.project is not None else []
        return [*uv_run(self.command, self.project), *repository, *arguments]

    def _once(self, arguments, stdin, on_event, on_line) -> tuple[Call, bool]:
        argv = self.argv(*arguments)
        self.log.command(self.checkout, argv)
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.checkout,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=processes.environment(),
            )
        except OSError as error:
            raise failed(f"{self.command} could not start: {error.strerror or error}.") from None
        human: list[str] = []

        def read_stderr():
            for line in process.stderr:
                text = line.rstrip("\n")
                human.append(text)
                self.log.write(f"   {text}")
                if on_line is not None and text.strip():
                    on_line(text.strip())

        reader = threading.Thread(target=read_stderr, daemon=True)
        reader.start()
        if stdin is not None:
            try:
                process.stdin.write(json.dumps(stdin))
                process.stdin.close()
            except OSError:
                pass
        events, result, emitted, raw = [], None, False, []
        for line in process.stdout:
            raw.append(line)
            text = line.strip()
            if not text:
                continue
            self.log.write(f" > {text}")
            try:
                event = json.loads(text)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            emitted = True
            if event.get("event") == "result":
                result = event
            else:
                events.append(event)
                if on_event is not None:
                    on_event(event)
        code = process.wait()
        reader.join()
        if result is not None and result.get("status") in EXIT_CODES:
            status = result["status"]
        else:
            status = "usage" if code == USAGE else ("done" if code == 0 else "failed")
        call = Call(status, code, result, events, human, "".join(raw), time.monotonic() - started)
        retry = (
            status in ("failed", "usage") and not emitted and any(WINDOWS_FILE_LOCK in line for line in human)
        )
        return call, retry

    def _started(self, arguments, stdin, on_event, on_line) -> Call:
        """uv failing on a Windows file lock (os error 1224, usually an antivirus scan holding a
        file in uv's cache) before the command starts is retried once; a second failure stands."""
        call, retry = self._once(arguments, stdin, on_event, on_line)
        if retry:
            self.log.write("   Windows os error 1224 before the command started: retrying once.")
            call, _ = self._once(arguments, stdin, on_event, on_line)
        return call

    def _report(self, line: str) -> None:
        self.log.write(f"   {line}")
        transient.say(line)

    def run(
        self,
        *arguments: str,
        stdin: dict | None = None,
        on_event: Callable[[dict], None] | None = None,
        on_line: Callable[[str], None] | None = None,
    ) -> Call:
        """Run one command. A read that fails the way a network or a busy service fails for a
        moment runs again after a wait, up to three times, each retry said in one line; a
        sign-in to renew never does, nor does a command that writes."""
        call = self._started(arguments, stdin, on_event, on_line)
        words = read_command(arguments)
        if words is None:
            return call
        label = " ".join((self.command, *words))
        if "--phase" in arguments:
            label += f" --phase {arguments[arguments.index('--phase') + 1]}"
        retried = 0
        while call.status in ("failed", "usage"):
            found = transient.classify("\n".join((*call.stderr, call.message)))
            if not transient.retries(found, retried, label=label, report=self._report):
                if found is not None:
                    call.temporary, call.retried = found, retried
                break
            retried += 1
            call = self._started(arguments, stdin, on_event, on_line)
        return call

    def document(self, *arguments: str) -> dict:
        """A command that prints one JSON document, such as `steps --json`."""
        call = self.run(*arguments)
        if call.status != "done":
            message = f"{self.command} {' '.join(arguments)} failed: {call.message}"
            if call.temporary is not None:
                raise Stop("failed", message, reason="transient")
            raise failed(message)
        try:
            found = json.loads(call.stdout)
        except ValueError:
            raise failed(f"{self.command} {' '.join(arguments)} printed no JSON document.") from None
        if not isinstance(found, dict):
            raise failed(f"{self.command} {' '.join(arguments)} printed no JSON object.")
        return found
