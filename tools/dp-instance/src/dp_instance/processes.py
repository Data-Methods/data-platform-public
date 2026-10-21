"""Running the tools dp-instance relies on: Git, the GitHub CLI, the Azure CLI, OpenSSH and uv.

Every tool runs from an argument list, never a shell, with its output read as UTF-8, so the
same call works on Windows, macOS and Linux. A tool's own TLS, proxy and credential settings
apply, since dp-instance makes no web requests of its own.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .outcome import failed


@dataclass(frozen=True)
class Completed:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def first_error_line(self) -> str:
        for line in (self.stderr or self.stdout).splitlines():
            if line.strip():
                return line.strip()
        return "no output"


def executable(name: str) -> str:
    """The tool's full path; a Windows `.cmd` shim, such as the Azure CLI's, runs only by its
    full path."""
    found = shutil.which(name)
    if found is None:
        raise failed(f"{name} was not found on PATH. Install it, then run the command again.")
    return found


def environment() -> dict[str, str]:
    """The environment for a tool: this one, without a virtual environment that isn't the
    tool's, and with Python's text streams in UTF-8 for any Python a tool starts."""
    values = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    values["PYTHONUTF8"] = "1"
    return values


def run(
    name: str,
    *arguments: str,
    cwd: Path | None = None,
    input: str | None = None,
    check: bool = True,
) -> Completed:
    """Run one tool to completion and return its output; ``check`` stops on a failure,
    naming the tool's own first line of error."""
    try:
        process = subprocess.run(
            [executable(name), *arguments],
            cwd=cwd,
            input=input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment(),
            stdin=None if input is not None else subprocess.DEVNULL,
        )
    except OSError as error:
        raise failed(f"{name} could not start: {error.strerror or error}.") from None
    completed = Completed(process.returncode, process.stdout, process.stderr)
    if check and not completed.ok:
        words = " ".join((name, *arguments[:2]))
        raise failed(f"{words} failed: {completed.first_error_line()}")
    return completed
