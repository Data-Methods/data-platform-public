"""One keypress, with the standard library only."""

from __future__ import annotations

import os
import sys


def read_key() -> str:
    """Return one key, lower-cased. Ctrl-C, or the terminal closing, raises KeyboardInterrupt."""
    if sys.platform == "win32":
        import msvcrt

        key = msvcrt.getwch()
    else:
        import termios
        import tty

        descriptor = sys.stdin.fileno()
        saved = termios.tcgetattr(descriptor)
        try:
            # Switch at once: waiting for pending output to drain can stall the key behind a
            # terminal that is still drawing.
            tty.setcbreak(descriptor, termios.TCSANOW)
            read = os.read(descriptor, 1)
            key = read.decode(errors="ignore") if read else "\x04"
        finally:
            termios.tcsetattr(descriptor, termios.TCSANOW, saved)
    if key in ("\x03", "\x04"):
        raise KeyboardInterrupt
    return key.lower()


def choose(allowed: str, default: str | None = None) -> str:
    """Wait for one of the allowed keys; Enter picks the default, if there is one."""
    while True:
        key = read_key()
        if key in ("\r", "\n") and default is not None:
            return default
        if key and key in allowed:
            return key
