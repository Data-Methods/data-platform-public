"""Running a component's CLI as its own process, through the engine's common interface.

Each component runs as `uv run --locked <command>` in its own checkout, so it keeps its own
engine version. With `--json` it writes one JSON object per line to stdout, one line as each
step starts and finishes and a result last, and everything meant for a person to stderr,
which goes to the run's log. Values a person supplies go to its stdin as one JSON document
and nowhere else. The engine retries its own reads, and a failed result names the kind of
temporary failure it was, when the command changed nothing, which dp-instance reads instead of
classifying the message; the command isn't run again. uv failing on a Windows file lock before
the command starts, as uv's own error says with no word from the command, is tried again after
each of `transient`'s waits: nothing ran, so a write is tried again too. Commands that run at
once in one environment have it built first, once, so no two build into one new environment
together. A command that fails because the Azure CLI's token cache lost the signed-in account
is a sign-in to renew, never any other failure: one that only reads runs once more first, and
one that writes never does. While the command holds the run lock, each call first reads the
lock's renewer's last result and stops the command once the lock is lost.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import processes, transient
from .outcome import EXIT_CODES, USAGE, Next, Stop, failed

WINDOWS_FILE_LOCK = "os error 1224"
# What a retry's line says of it.
FILE_IN_USE = "Windows reported a file in use before the command started (os error 1224)"
# uv's own failure, as uv 0.13 writes it: its exit code, then its error's header and each cause.
UV_FAILED = 2
UV_ERROR = "error: "
UV_CAUSE = "  cause: "
UV_HINT = "hint: "
# The Azure CLI's words when its token cache has lost the signed-in account, as several of its
# processes refreshing the cache at once can leave it: a sign-in to renew, with `az login`.
AZURE_TOKEN_CACHE = "does not exist in msal token cache"
# The commands that only read: one the token cache failed runs once more.
READS = (("steps",), ("outputs", "aggregate"), ("repo", "adoption"), ("preflight",), ("plan",), ("verify",))
# What each call checks first while the command holds the run lock.
_guard: Callable[[], None] | None = None


@contextmanager
def guarded(check: Callable[[], None]) -> Iterator[None]:
    """Have every component call run ``check`` first, which stops the command by raising."""
    global _guard
    _guard = check
    try:
        yield
    finally:
        _guard = None


def read_command(arguments) -> tuple[str, ...] | None:
    """The read-only command ``arguments`` run, if they run one."""
    return next((words for words in READS if tuple(arguments[: len(words)]) == words), None)


def signed_out(call: Call) -> bool:
    """Whether the command failed because the Azure CLI's token cache lost the signed-in
    account."""
    said = "\n".join((*call.stderr, str((call.result or {}).get("message") or ""))).lower()
    return call.status == "failed" and AZURE_TOKEN_CACHE in said


def uv_run(command: str, project: Path | None) -> list[str]:
    """The launcher: `uv run --locked`, from the checkout's own project or from the target's
    fetched template source."""
    launcher = [processes.executable("uv"), "run", "--locked"]
    if project is not None:
        launcher += ["--project", str(project)]
    return [*launcher, command]


def uv_error(stderr: list[str]) -> list[str] | None:
    """uv's own error, when the output ends with one: its header and each cause, followed only
    by uv's hints."""
    lines = [line for line in stderr if line.strip()]
    headers = [index for index, line in enumerate(lines) if line.startswith(UV_ERROR)]
    if not headers:
        return None
    chain = [lines[headers[-1]]]
    rest = lines[headers[-1] + 1 :]
    while rest and rest[0].startswith(UV_CAUSE):
        chain.append(rest.pop(0))
    return chain if all(line.startswith(UV_HINT) for line in rest) else None


def file_in_use_before_start(code: int, stderr: list[str]) -> bool:
    """Whether uv failed on a Windows file lock before the command it runs started: uv's own exit
    code, and its output ending with uv's error, one of whose lines names os error 1224. A
    command that started and failed on a uv it ran itself ends otherwise, with its own words or
    its own exit code."""
    chain = uv_error(stderr)
    return code == UV_FAILED and chain is not None and any(WINDOWS_FILE_LOCK in line for line in chain)


def uv_sync(project: Path) -> list[str]:
    """The project's environment built from its lock as `uv run --locked` builds it, removing
    nothing it holds besides."""
    return [processes.executable("uv"), "sync", "--locked", "--inexact", "--project", str(project)]


def built(clis: Sequence[Cli]) -> None:
    """Build each environment that more than one of ``clis`` runs in, once, before they run at
    once, so no two of them build into one new environment together; each environment builds
    in parallel with the others. One that can't be built stops, saying why."""
    counted = Counter(cli.environment for cli in clis)
    shared = {cli.environment: cli for cli in clis if counted[cli.environment] > 1}
    if not shared:
        return
    with ThreadPoolExecutor(max_workers=len(shared)) as pool:
        calls = list(pool.map(Cli.sync, shared.values()))
    for environment, call in zip(shared, calls, strict=True):
        if call.status != "done":
            raise failed(f"The environment in {environment} couldn't be built: {call.message}")


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
    """One component process: its result, the events it wrote and its human lines. One whose
    failed result named a temporary failure, after the engine's own retries, holds its kind."""

    status: str
    code: int
    result: dict | None
    events: list[dict] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    stdout: str = ""
    seconds: float = 0.0
    temporary: str | None = None

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
        """What it said, and, when it looked temporary and doesn't say so, that it did. Where uv
        itself failed, its error's header and last cause, never its hints."""
        chain = uv_error(self.stderr)
        if self.result and self.result.get("message"):
            text = str(self.result["message"])
        elif chain is not None:
            text = chain[0].removeprefix(UV_ERROR)
            if len(chain) > 1:
                text += f": {chain[-1].removeprefix(UV_CAUSE)}"
        else:
            last = self.excerpt(1)
            text = last[0] if last else f"exit code {self.code}"
        if self.temporary is None or "looked temporary" in text:
            return text
        end = "" if text.endswith(".") else "."
        return f"{text}{end} It looked temporary ({self.temporary}); nothing changed."


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

    @property
    def environment(self) -> Path:
        """The project whose environment the CLI runs in."""
        return self.project if self.project is not None else self.checkout

    def argv(self, *arguments: str) -> list[str]:
        repository = ["--repo", str(self.checkout)] if self.project is not None else []
        return [*uv_run(self.command, self.project), *repository, *arguments]

    def _once(self, argv, stdin, on_event, on_line) -> tuple[Call, bool]:
        if _guard is not None:
            _guard()
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
        return call, not emitted and file_in_use_before_start(code, human)

    def label(self, arguments) -> str:
        """The command, as a retry's line names it: its CLI, its words and its phase."""
        label = " ".join((self.command, *(read_command(arguments) or arguments[:1])))
        if "--phase" in arguments:
            label += f" --phase {arguments[arguments.index('--phase') + 1]}"
        return label

    def _report(self, line: str) -> None:
        self.log.write(f"   {line}")
        transient.say(line)

    def _started(self, label: str, argv, stdin=None, on_event=None, on_line=None) -> Call:
        """One process, tried again after each of `transient`'s waits while uv fails on a
        Windows file lock before the command starts, each retry logged; the last failure
        stands."""
        retried = 0
        while True:
            call, locked = self._once(argv, stdin, on_event, on_line)
            if not locked or not transient.retries(FILE_IN_USE, retried, label=label, report=self._report):
                return call
            retried += 1

    def sync(self) -> Call:
        """Build the CLI's environment from its lock, as its first `uv run` would."""
        return self._started(f"uv sync {self.environment.name}", uv_sync(self.environment))

    def run(
        self,
        *arguments: str,
        stdin: dict | None = None,
        on_event: Callable[[dict], None] | None = None,
        on_line: Callable[[str], None] | None = None,
    ) -> Call:
        """Run one command. uv failing on a Windows file lock (os error 1224, usually an
        antivirus scan holding a file in uv's cache) before the command starts is tried again
        after each of `transient`'s waits; the last failure stands. A command that only reads
        and that the Azure CLI's token cache failed runs once more, as several of its processes
        refreshing the cache at once can leave it for a moment; failing that way again, it waits
        for a sign-in. One that writes waits for it at once. A failed command whose result names
        a temporary failure, its retries spent and nothing changed, holds its kind."""
        call = self._started(self.label(arguments), self.argv(*arguments), stdin, on_event, on_line)
        reads = read_command(arguments) is not None
        if signed_out(call) and reads:
            self._report(
                f"{self.label(arguments)}: the Azure CLI's token cache lost the signed-in account; "
                "running it once more."
            )
            call = self._started(self.label(arguments), self.argv(*arguments), stdin, on_event, on_line)
        if signed_out(call):
            call.status = "sign-in"
        if call.status == "failed":
            call.temporary = (call.result or {}).get("temporary") or None
        return call

    def document(self, *arguments: str) -> dict:
        """A command that prints one JSON document, such as `steps --json`; one that waits for a
        sign-in stops for it, naming the command that renews it."""
        call = self.run(*arguments)
        if call.status != "done":
            message = f"{self.command} {' '.join(arguments)} failed: {call.message}"
            if call.status == "sign-in":
                command = ((call.result or {}).get("sign_in") or {}).get("command") or "az login"
                raise Stop(
                    "sign-in",
                    message,
                    reason="sign-in",
                    sign_in={"command": command},
                    next=(Next(command, "renews the sign-in, which only you can do"),),
                )
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
