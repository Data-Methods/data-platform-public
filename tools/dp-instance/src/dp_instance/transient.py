"""Bounded retries of a read that failed the way a network or a busy service fails for a
moment: a dropped or refused connection, DNS, a TLS handshake, throttling or a server error.

The engine's classifier, with Git's own words for the same failures, for dp-instance's own Git
network calls and the component calls that only read. A write never retries: a resumed run
plans it again. A sign-in to renew never retries either.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable

# The waits before each retry: three retries over about two minutes.
DELAYS = (10, 30, 60)
_sleep = time.sleep

# HTTP's 429 and 5xx, as the engine's tools say them and as Git over HTTPS does: "The requested
# URL returned error: 502".
_STATUS = re.compile(r"(?:status ?code|status|http|returned error)[ =:]*(?:429|5\d\d)\b")
# Microsoft Entra's challenge to sign in again: a person's to answer, however it reads.
_SIGN_IN = ("aadsts", "interactionrequired")
_DNS = "DNS resolution failed"
# The name a failed lookup names, as OpenSSH, curl (Git over HTTPS) and Go say it. A name with
# no dot, such as an SSH host alias that Git's ssh didn't read from its configuration, never
# resolves however often it's asked.
_LOOKUP = re.compile(r"(?:could not resolve hostname |could not resolve host: |lookup )([^\s:'\"]+)")

# The first kind whose marker the failure's lowercase text holds wins. Each kind holds the
# engine's markers, then Git's own: curl's, for Git over HTTPS, and OpenSSH's.
_KINDS = (
    (
        _DNS,
        (
            "name resolution",
            "name or service not known",
            "nodename nor servname",
            "getaddrinfo failed",
            "no such host",
            "could not resolve host",
            "failed to resolve",
        ),
    ),
    (
        "connection refused",
        (
            "connection refused",
            "failed to establish a new connection",
            "couldn't connect to server",
            "failed to connect to",
        ),
    ),
    (
        "connection dropped",
        (
            "connection reset",
            "connection aborted",
            "connection timed out",
            "read timed out",
            "remote end closed connection",
            "broken pipe",
            "unexpected eof",
            "eof occurred",
            "early eof",
            "connection was reset",
            "operation timed out",
            "transfer closed with outstanding read data remaining",
            "was not closed cleanly",
            "connection was non-properly terminated",
            "connection closed by remote host",
        ),
    ),
    (
        "TLS handshake failed",
        (
            "tls handshake",
            "handshake operation timed out",
            "sslerror",
            "gnutls_handshake() failed",
            "failed to receive handshake",
            "ssl_error_syscall",
        ),
    ),
    (
        "the service was busy",
        (
            "too many requests",
            "throttl",
            "rate limit",
            "retry later",
            "retry after",
            "serverbusy",
            "server busy",
            "service unavailable",
            "serviceunavailable",
            "internal server error",
            "internalservererror",
            "bad gateway",
            "gateway timeout",
            "gatewaytimeout",
        ),
    ),
)


def classify(text: str) -> str | None:
    """The temporary failure ``text`` describes, if it describes one."""
    lowered = (text or "").lower()
    if any(marker in lowered for marker in _SIGN_IN):
        return None
    for description, markers in _KINDS:
        if any(marker in lowered for marker in markers):
            if description == _DNS and any("." not in name for name in _LOOKUP.findall(lowered)):
                return None
            return description
    if _STATUS.search(lowered):
        return "the service was busy"
    return None


def say(line: str) -> None:
    """A retry's line for a person, on stderr: beside `--json`'s events, and above a step's
    live block at a terminal."""
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def retries(found: str | None, retried: int, *, label: str, report: Callable[[str], None] = say) -> bool:
    """After a failure and ``retried`` retries, whether to try again: a temporary failure, with
    retries left. Reports it in one line and waits before returning True."""
    if found is None or retried >= len(DELAYS):
        return False
    delay = DELAYS[retried]
    report(f"{label}: {found}; retrying in {delay}s ({retried + 1} of {len(DELAYS)}).")
    _sleep(delay)
    return True


def exhausted(message: str, found: str | None, retried: int) -> str:
    """A failure's message, saying when it looked temporary that it was retried and that
    nothing changed."""
    if found is None:
        return message
    end = "" if message.endswith(".") else "."
    return (
        f"{message}{end} It looked temporary ({found}) and failed again after {retried} retries; "
        "nothing changed."
    )
