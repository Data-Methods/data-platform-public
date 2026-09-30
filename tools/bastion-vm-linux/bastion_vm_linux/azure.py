from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path, PureWindowsPath
from typing import Any


class CommandError(RuntimeError):
    pass


def resolve_executable(name: str) -> str | None:
    """Resolve native executables and Windows command shims deterministically."""
    if sys.platform == "win32" and Path(name).suffix == "":
        candidates = (f"{name}.exe", f"{name}.cmd", f"{name}.bat", name)
    else:
        candidates = (name,)
    for candidate in candidates:
        if executable := shutil.which(candidate):
            return executable
    return None


def azure_cli_command(*args: str) -> list[str]:
    """Build an Azure CLI command without Windows batch-file reparsing."""
    executable = resolve_executable("az")
    if executable is None:
        return ["az", *args]
    if sys.platform == "win32" and Path(executable).suffix.casefold() == ".cmd":
        python = PureWindowsPath(executable).parent.parent / "python.exe"
        return [str(python), "-IBm", "azure.cli", *args]
    return [executable, *args]


def display_command(command: list[str]) -> str:
    """Render a command for diagnostics without changing its execution shape."""
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def _debug_enabled() -> bool:
    return os.environ.get("BASTION_VM_DEBUG", "").casefold() in {"1", "true", "yes"}


def _redact(value: str) -> str:
    result = re.sub(
        r"(?i)((?:authorization|cookie|set-cookie)\s*[:=]\s*(?:bearer\s+)?)[^\r\n]+",
        r"\1<redacted>",
        value,
    )
    result = re.sub(
        r'(?i)("?(?:(?:access|refresh|id)[_-]?token|client[_-]?secret|password|'
        r'api[_-]?key|secret)"?\s*[:=]\s*"?)[^\s,"}]+',
        r"\1<redacted>",
        result,
    )
    result = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        "<redacted-jwt>",
        result,
    )
    return result


def _debug_detail(label: str, value: str) -> None:
    if _debug_enabled() and value.strip():
        print(f"DEBUG {label}: {_redact(value.strip())}", file=sys.stderr)


def _concise_error(value: str) -> str:
    text = _redact(value).strip()
    aad = re.search(r"(AADSTS\d+):\s*([^\r\n]+)", text)
    if aad:
        message = aad.group(2).split(" Trace ID:", 1)[0].strip()
        return f"{aad.group(1)}: {message}"
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("at ") or candidate.startswith("---"):
            continue
        if "Stack trace:" in candidate:
            candidate = candidate.split("Stack trace:", 1)[0].strip()
        if candidate:
            return candidate[:800]
    return "unknown service error"


def _failure(command: list[str], stdout: str, stderr: str) -> CommandError:
    detail = stderr.strip() or stdout.strip() or "unknown Azure CLI error"
    _debug_detail("Azure CLI response", detail)
    return CommandError(f"{display_command(command)} failed: {_concise_error(detail)}")


class AzureCli:
    def __init__(self, *, subscription: str | None = None) -> None:
        self.subscription = subscription

    def _command(self, args: tuple[str, ...]) -> list[str]:
        context = ("--subscription", self.subscription) if self.subscription else ()
        return azure_cli_command(*args, *context)

    def run(self, *args: str, parse_json: bool = False) -> Any:
        command = self._command(args)
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as exc:
            raise CommandError(
                f"could not start Azure CLI command {display_command(command)}: {exc}"
            ) from exc
        if result.returncode != 0:
            raise _failure(command, result.stdout, result.stderr)
        output = result.stdout.strip()
        return json.loads(output or "null") if parse_json else output

    def run_with_heartbeat(
        self,
        *args: str,
        parse_json: bool = False,
        heartbeat_label: str,
        heartbeat_seconds: int = 30,
    ) -> Any:
        command = self._command(args)
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise CommandError(
                f"could not start Azure CLI command {display_command(command)}: {exc}"
            ) from exc

        started = time.monotonic()
        while True:
            try:
                stdout, stderr = process.communicate(timeout=heartbeat_seconds)
                break
            except subprocess.TimeoutExpired:
                elapsed = int(time.monotonic() - started)
                print(f"{heartbeat_label} is still running ({elapsed}s elapsed).")

        if process.returncode != 0:
            raise _failure(command, stdout, stderr)
        output = stdout.strip()
        return json.loads(output or "null") if parse_json else output

    def account(self) -> dict[str, Any]:
        return self.run("account", "show", "--output", "json", parse_json=True)
