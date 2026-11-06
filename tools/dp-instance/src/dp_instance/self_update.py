"""`dp-instance self update`: install the newest published dp-instance, or with `--to` an exact one.

Each published version is a `dp-instance/X.Y.Z` tag of data-platform-public, which `git ls-remote`
lists, and uv installs from its tag. `self update` takes the newest, out of band, with nothing to
resume. `--to` installs the published version it names, to pin it or roll back: each dp-instance
knows only its own floor, so the version installed refuses an instance it can't read when it runs.
In an instance folder, `--to` pins the version, so `status` names no `self update` while it's
installed, and `self update` without `--to` clears the pin. Windows keeps a running program's
files in use, so there dp-instance hands the install to uv in a process of its own, which waits
for dp-instance and the launchers that started it to end, and tries again while another program,
such as an antivirus scan, still holds a file open. Its log's last line says when the install
has finished, and so does the window that started dp-instance, when it has one; elsewhere uv
replaces dp-instance while it runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import gitops, installed, manifest, processes
from .instance import Instance, state_folder
from .outcome import Next, Result, failed
from .screen import Screen

# Windows process creation flags: no console, and out of the console's Ctrl+C group.
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
# The programs that start dp-instance on Windows, each in use while it runs: uv's launcher,
# `dp-instance.exe`, and the Python of dp-instance's own tool environment.
LAUNCHER = "dp-instance.exe"
PYTHONS = ("python.exe", "pythonw.exe")
# Waits, in the base Python that isn't uv's to replace, for each process named to end, then runs
# a command with its output in a log file, starting no console window of its own. An attempt that
# finds a file still in use, as uv's "os error 32", "5" or "1224" say, is tried again, twice. Its
# last line, the log's too, says whether the install finished, and it writes that line in the
# console of the process named, the one that started dp-instance, when that console is still open.
WAITER = r"""
import ctypes, subprocess, sys, time
from ctypes import wintypes
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.OpenProcess.restype = wintypes.HANDLE
kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel.AttachConsole.argtypes = (wintypes.DWORD,)
waited, log, console, installed, failed = sys.argv[1:6]
command = sys.argv[6:]
for process in filter(None, waited.split(",")):
    handle = kernel.OpenProcess(0x00100000, False, int(process))
    if handle:
        kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
        kernel.CloseHandle(handle)
IN_USE = (
    "os error 32", "os error 5", "os error 1224", "being used by another process",
    "Access is denied", "user-mapped section open",
)
PAUSES = (2, 5)
with open(log, "w", encoding="utf-8") as output:
    for attempt in range(len(PAUSES) + 1):
        found = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=0x08000000,
        )
        text = found.stdout + found.stderr
        output.write(text)
        output.flush()
        if found.returncode == 0 or attempt == len(PAUSES) or not any(item in text for item in IN_USE):
            break
        output.write("Files were still in use; trying again.\n")
        output.flush()
        time.sleep(PAUSES[attempt])
    line = installed if found.returncode == 0 else failed
    output.write(line + "\n")
if console != "0" and kernel.AttachConsole(int(console)):
    try:
        with open("CONOUT$", "w", encoding="utf-8") as screen:
            screen.write("\n" + line + "\n")
    except OSError:
        pass
"""


def published() -> list[str]:
    """Every published dp-instance version, oldest first."""
    return manifest.tool_versions(gitops.remote_refs(manifest.RELEASES_URL, f"{manifest.TOOL_TAG_PREFIX}*"))


def chain() -> tuple[list[int], int | None]:
    """On Windows, this process and the launchers above it that started dp-instance, innermost
    first: uv's `dp-instance.exe` and the Python of dp-instance's own tool environment, never any
    other program, such as a Python harness running dp-instance. Each keeps its program file in
    use until it ends, a moment after the one it started. Then the process that started them,
    such as the shell, whose console stays open after dp-instance ends; None when dp-instance runs
    with no console window, as an agent's or a service's process may, or when there's none."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.GetConsoleWindow.restype = wintypes.HWND
    kernel.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    environment = Path(sys.prefix).resolve()

    def in_environment(process: int) -> bool:
        handle = kernel.OpenProcess(0x1000, False, process)
        if not handle:
            return False
        try:
            size = wintypes.DWORD(32768)
            path = ctypes.create_unicode_buffer(size.value)
            if not kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                return False
            return Path(path.value).resolve().is_relative_to(environment)
        finally:
            kernel.CloseHandle(handle)

    found = [os.getpid()]
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return found, None
    parents: dict[int, tuple[int, str]] = {}
    entry = Entry()
    entry.dwSize = ctypes.sizeof(Entry)
    try:
        listed = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while listed:
            parents[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.casefold())
            listed = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    parent = parents.get(found[0], (0, ""))[0]
    while (
        parent
        and parent not in found
        and (
            parents.get(parent, (0, ""))[1] == LAUNCHER
            or (parents.get(parent, (0, ""))[1] in PYTHONS and in_environment(parent))
        )
    ):
        found.append(parent)
        parent = parents[parent][0]
    console = parent and parent in parents and parent not in found and kernel.GetConsoleWindow()
    return found, (parent if console else None)


def hand_off(
    command: list[str], waited: list[int], log: Path, *, console: int | None, done: str, broken: str
) -> subprocess.Popen:
    """On Windows, run ``command`` once each process in ``waited`` ends, from a process of its own
    that outlives them, with the command's output in ``log``; its last line, ``done`` or
    ``broken``, also goes to the console of ``console``, the process that started dp-instance."""
    python = Path(sys.base_prefix) / "python.exe"
    return subprocess.Popen(
        [
            str(python),
            "-I",
            "-c",
            WAITER,
            ",".join(str(item) for item in waited),
            str(log),
            str(console or 0),
            done,
            broken,
            *command,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        env=processes.environment(),
    )


def windows() -> bool:
    return sys.platform == "win32"


def self_update(screen: Screen, start: Path, to: str | None) -> Result:
    own = installed.version()
    root = Instance.locate(start)
    state = state_folder(root) if root is not None else None
    screen.header("self update")
    screen.begin("the published versions")
    versions = published()
    if not versions:
        raise failed("No dp-instance version is published yet.")
    newest = versions[-1]
    if to is not None and to not in versions:
        raise failed(f"dp-instance {to} isn't published. Published: {', '.join(versions)}.")
    wanted = to or newest
    detail = {"installed": own, "newest": newest, "target": wanted}
    screen.blank()
    screen.labelled("Installed:", own or "a local copy, not a published version")
    screen.labelled("Newest Published:", newest)
    if to is not None:
        screen.labelled("Asked For:", to)
    if state is not None:
        # In an instance folder, `--to` pins the version it installs, and any other clears it.
        if to is None:
            installed.unpin(state)
        else:
            installed.pin(state, to)
            screen.labelled("Pinned:", f"{to}, until dp-instance self update without --to")
        detail["pinned"] = to
    if own == wanted or (to is None and installed.at_least(own, newest)):
        message = (
            f"dp-instance {own} is the newest published version; nothing to install."
            if to is None
            else f"dp-instance {own} is installed already; nothing to install."
        )
        screen.end()
        screen.blank()
        screen.note(message, "bold green")
        return Result("self update", "done", message, detail=detail)
    tag = manifest.tool_tag(wanted)
    arguments = ["tool", "install", "--reinstall", manifest.install_spec(tag)]
    again = manifest.install_command(tag)
    screen.begin("the install")
    if windows():
        log = Path(tempfile.gettempdir()) / "dp-instance-self-update.log"
        waited, console = chain()
        hand_off(
            [processes.executable("uv"), *arguments],
            waited,
            log,
            console=console,
            done=f"dp-instance {wanted} is installed.",
            broken=(
                f"uv couldn't install dp-instance {wanted}, and dp-instance may not run until it does: "
                f"its output is in {log}. Install it again in a new window: {again}"
            ),
        )
        detail.update(log=str(log), console=console is not None)
        message = (
            f"uv installs dp-instance {wanted} once this command ends, and dp-instance doesn't run "
            f"until it has. The last line of {log} says when: 'dp-instance {wanted} is installed.'"
            + (" This window says it too." if console is not None else "")
        )
    else:
        found = processes.run("uv", *arguments, check=False)
        if not found.ok:
            raise failed(
                f"uv couldn't install dp-instance {wanted}: {found.first_error_line()}",
                Next(again, f"installs dp-instance {wanted} again"),
            )
        message = f"Installed dp-instance {wanted}."
    screen.end()
    screen.blank()
    screen.note(message, "bold green")
    return Result("self update", "done", message, detail=detail)
