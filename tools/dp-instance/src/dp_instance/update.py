"""`dp-instance update`: install the newest published dp-instance.

Each published version is a `dp-instance/X.Y.Z` tag of data-platform-public, which
`git ls-remote` lists, and uv installs from its tag. A release that needs a newer one names it;
`update` takes the newest, out of band, with nothing to resume. Windows keeps a running
program's files in use, so there dp-instance hands the install to uv in a process of its own,
which waits for dp-instance and the launchers that started it to end, and exits; elsewhere uv
replaces it while it runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import gitops, installed, manifest, processes
from .outcome import Result, failed
from .screen import Screen

# Windows process creation flags: no console, and out of the console's Ctrl+C group.
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
# The programs that start dp-instance on Windows, each in use while it runs: uv's launcher,
# `dp-instance.exe`, and the Python of dp-instance's own tool environment.
LAUNCHER = "dp-instance.exe"
PYTHONS = ("python.exe", "pythonw.exe")
# Waits, in the base Python that isn't uv's to replace, for each process named to end, then runs
# a command with its output in a log file, starting no console window of its own. A first
# attempt that finds files still in use is tried once more.
WAITER = r"""
import ctypes, subprocess, sys, time
from ctypes import wintypes
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.OpenProcess.restype = wintypes.HANDLE
kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
waited, log, command = sys.argv[1], sys.argv[2], sys.argv[3:]
for process in waited.split(","):
    handle = kernel.OpenProcess(0x00100000, False, int(process))
    if handle:
        kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
        kernel.CloseHandle(handle)
IN_USE = ("os error 32", "os error 5", "being used by another process", "Access is denied")
with open(log, "w", encoding="utf-8") as output:
    for attempt in (1, 2):
        found = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=0x08000000,
        )
        text = found.stdout + found.stderr
        output.write(text)
        output.flush()
        if found.returncode == 0 or attempt == 2 or not any(item in text for item in IN_USE):
            break
        output.write("Files were still in use; trying again.\n")
        time.sleep(2)
"""


def published() -> list[str]:
    """Every published dp-instance version, oldest first."""
    return manifest.tool_versions(gitops.remote_refs(manifest.RELEASES_URL, f"{manifest.TOOL_TAG_PREFIX}*"))


def chain() -> list[int]:
    """On Windows, this process and the launchers above it that started dp-instance, innermost
    first: uv's `dp-instance.exe` and the Python of dp-instance's own tool environment, never any
    other program, such as a Python harness running dp-instance. Each keeps its program file in
    use until it ends, a moment after the one it started."""
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
        return found
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
    return found


def hand_off(command: list[str], waited: list[int], log: Path) -> None:
    """On Windows, run ``command`` once each process in ``waited`` ends, from a process of its own
    that outlives them, with the command's output in ``log``."""
    python = Path(sys.base_prefix) / "python.exe"
    subprocess.Popen(
        [str(python), "-I", "-c", WAITER, ",".join(str(item) for item in waited), str(log), *command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        env=processes.environment(),
    )


def windows() -> bool:
    return sys.platform == "win32"


def update(screen: Screen) -> Result:
    own = installed.version()
    screen.header("update")
    screen.begin("the newest version")
    versions = published()
    if not versions:
        raise failed("No dp-instance version is published yet.")
    newest = versions[-1]
    detail = {"installed": own, "newest": newest}
    screen.blank()
    screen.labelled("Installed:", own or "a local copy, not a published version")
    screen.labelled("Newest Published:", newest)
    if installed.at_least(own, newest):
        message = f"dp-instance {own} is the newest published version; nothing to install."
        screen.end()
        screen.blank()
        screen.note(message, "bold green")
        return Result("update", "done", message, detail=detail)
    arguments = ["tool", "install", "--reinstall", manifest.install_spec(manifest.tool_tag(newest))]
    screen.begin("the install")
    if windows():
        log = Path(tempfile.gettempdir()) / "dp-instance-update.log"
        hand_off([processes.executable("uv"), *arguments], chain(), log)
        message = (
            f"uv installs dp-instance {newest} as soon as this command ends, writing its output to "
            f"{log}, whose last line says when it's installed."
        )
    else:
        found = processes.run("uv", *arguments, check=False)
        if not found.ok:
            raise failed(f"uv couldn't install dp-instance {newest}: {found.first_error_line()}")
        message = f"Installed dp-instance {newest}."
    screen.end()
    screen.blank()
    screen.note(message, "bold green")
    return Result("update", "done", message, detail=detail)
