"""A SOCKS proxy on the operator's loopback, through Azure Bastion to the Bastion VM."""

from __future__ import annotations

import argparse
import errno
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from .azure import CommandError, azure_cli_command, display_command
from .config import WorkstationConfig

DEFAULT_PORT = 1080
LOOPBACK = "127.0.0.1"
POLL_SECONDS = 0.25
STOP_SECONDS = 10


def port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a port number") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("the port must be from 1 to 65535")
    return port


def check_port(port: int) -> None:
    """Refuse a port that is already in use, binding the way OpenSSH binds its listener."""
    try:
        with socket.create_server((LOOPBACK, port)):
            pass
    except OSError as exc:
        reason = "it is in use" if exc.errno == errno.EADDRINUSE else str(exc)
        raise CommandError(
            f"Port {port} on {LOOPBACK} is not free: {reason}. Choose another with --port."
        ) from exc


def ssh_command(config: WorkstationConfig, vm_id: str, port: int) -> list[str]:
    return azure_cli_command(
        "network",
        "bastion",
        "ssh",
        "--name",
        config.name_prefix,
        "--resource-group",
        config.resource_group_name,
        "--target-resource-id",
        vm_id,
        "--auth-type",
        "AAD",
        "--subscription",
        config.subscription_id,
        "--",
        "-D",
        f"{LOOPBACK}:{port}",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
    )


def browser_profile(config: WorkstationConfig) -> Path:
    return (
        Path.home() / ".data-platform-bastion-vm" / config.platform_instance_key / "browser-profile"
    )


def browser_commands(
    profile: Path, port: int, platform: str = sys.platform
) -> list[tuple[str, str]]:
    """Return the Chrome and Edge commands that open the dedicated profile through the proxy."""
    flags = " ".join(
        (
            f'--user-data-dir="{profile}"',
            f'--proxy-server="socks5://{LOOPBACK}:{port}"',
            f'--host-resolver-rules="MAP * ~NOTFOUND , EXCLUDE {LOOPBACK}"',
            "--disable-quic",
        )
    )
    if platform == "win32":
        arguments = flags.replace("'", "''")
        return [
            ("Chrome", f"Start-Process chrome -ArgumentList '{arguments}'"),
            ("Edge", f"Start-Process msedge -ArgumentList '{arguments}'"),
        ]
    if platform == "darwin":
        return [
            ("Chrome", f'open -na "Google Chrome" --args {flags}'),
            ("Edge", f'open -na "Microsoft Edge" --args {flags}'),
        ]
    return [
        ("Chrome", f"google-chrome {flags}"),
        ("Edge", f"microsoft-edge {flags}"),
    ]


def open_tunnel(config: WorkstationConfig, vm_id: str, port: int) -> int:
    check_port(port)
    profile = browser_profile(config)

    def opened() -> None:
        print(f"Tunnel open: a SOCKS proxy on {LOOPBACK}:{port}.")
        print("In another terminal, open a dedicated browser profile that uses it:")
        for browser, command in browser_commands(profile, port):
            print(f"  {browser}: {command}")
        print("Everything that profile opens goes through the VM, and names resolve there.")
        print("To stop: close that browser, then press Ctrl-C here.", flush=True)

    print(
        f"Opening the tunnel to {config.name_prefix}-vm through Azure Bastion "
        f"{config.name_prefix}.",
        flush=True,
    )
    return run(ssh_command(config, vm_id, port), port, on_open=opened)


def run(command: list[str], port: int, *, on_open: Callable[[], None]) -> int:
    """Run the tunnel in the foreground with its own temporary directory, removed on exit.

    The Azure CLI writes each session's Entra SSH key and certificate to a new folder in
    the temporary directory and never removes it; pointing the temporary directory at
    a private folder lets this command remove exactly its own keys.
    """
    directory = tempfile.mkdtemp(prefix="data-platform-bastion-tunnel-")
    environment = {**os.environ, "TMPDIR": directory, "TEMP": directory, "TMP": directory}
    process: subprocess.Popen[bytes] | None = None
    opened = False
    stopped = False
    try:
        try:
            process = subprocess.Popen(command, env=environment)
        except OSError as exc:
            raise CommandError(f"could not start {display_command(command)}: {exc}") from exc
        while True:
            try:
                process.wait(timeout=POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if not opened and _listening(port):
                    opened = True
                    on_open()
    except KeyboardInterrupt:
        stopped = True
    finally:
        if process is not None:
            _stop(process)
        removed = _remove(directory)
    # The Azure CLI exits 0 whatever ssh does, so its exit code says nothing here.
    if removed:
        print("Removed this tunnel's temporary directory and any keys in it.")
    if stopped:
        print("Tunnel closed.")
    elif opened:
        print("ERROR: The tunnel closed. Run tunnel again to reopen it.", file=sys.stderr)
    else:
        print(
            "ERROR: The tunnel did not open. The messages above give the reason.",
            file=sys.stderr,
        )
    return 0 if stopped and removed else 1


def _listening(port: int) -> bool:
    try:
        with socket.create_connection((LOOPBACK, port), timeout=1):
            return True
    except OSError:
        return False


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Give the tunnel time to exit on the operator's Ctrl-C, then end it."""
    try:
        process.wait(timeout=STOP_SECONDS)
        return
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        pass
    process.terminate()
    try:
        process.wait(timeout=STOP_SECONDS)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        process.kill()
        process.wait()


def _remove(directory: str) -> bool:
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        print(
            f"ERROR: Could not remove {directory}, which holds this session's Entra SSH key "
            f"({exc}). Delete it.",
            file=sys.stderr,
        )
        return False
    return True
