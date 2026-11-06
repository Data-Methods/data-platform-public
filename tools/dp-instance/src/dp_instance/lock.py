"""The run lock, as Terraform's state lock works: one per instance, a 60-second lease on the `lock`
blob in the instance's shared run record.

`prepare`, `apply`, `resume`, `plan`, `verify` and the `secret` commands each take it as they
start and release it as they exit, holding it the whole time, a question's wait included. While
another command holds it, a command refuses, naming the holder and since when; the blob names
its holder while the lease is held. A background thread renews the lease every 15 to 20
seconds, starting no process: the token it signs in with is read again only about hourly.
Before each component call and at each step boundary the command reads the renewer's last
result, never the network, and stops once the lock is lost: a renewal the service refused, or
none that succeeded within the lease's minute, measured by both the monotonic and the wall
clock, so a workstation that slept counts the time it slept. A process that crashes or is
killed leaves its lease to lapse within a minute. `dp-instance unlock RUN` breaks the lease the
named run's command holds, for a process that hangs while alive, and notes who cleared it in
the run's record.
"""

from __future__ import annotations

import json
import random
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

from .blobs import BlobError
from .outcome import Next, Result, Stop, failed
from .record import now
from .shared import LOCK, SCHEMA_VERSION, Shared

if TYPE_CHECKING:
    from .session import Session

LEASE_SECONDS = 60
RENEW_EVERY = (15, 20)
# How long releasing waits for a renewal under way.
JOIN_SECONDS = 15
# What the service answers a renewal of a lease that's no longer this command's.
LOST = (
    "LeaseIdMismatchWithLeaseOperation",
    "LeaseNotPresentWithLeaseOperation",
    "LeaseIsBrokenAndCannotBeRenewed",
    "LeaseLost",
    "BlobNotFound",
)
EMPTY = (json.dumps({"schema_version": SCHEMA_VERSION}) + "\n").encode()


@dataclass(frozen=True)
class Holder:
    """Who holds the run lock: the command, its run once it names one, the person and the
    workstation, and since when."""

    command: str
    run: str | None
    who: str
    host: str
    since: str

    def describe(self) -> str:
        run = f" for run {self.run}" if self.run else ""
        return f"dp-instance {self.command}{run}, by {self.who} on {self.host}, since {self.since}"

    @classmethod
    def read(cls, body: bytes) -> Holder | None:
        """The holder the lock blob names; None while a command that has just taken it hasn't
        named itself yet."""
        try:
            data = json.loads(body)
            return cls(data["command"], data.get("run"), data["who"], data["host"], data["since"])
        except (ValueError, KeyError, TypeError):
            return None


def operator(shared: Shared) -> dict[str, str]:
    """Who runs this command, and where: the account the storage token's own claims name, the
    one that signs every request, and the workstation's name."""
    return {**shared.container.token.identity(), "host": socket.gethostname()}


def held(shared: Shared) -> tuple[Holder | None, str] | None:
    """Who holds the run lock, with the lock blob's ETag; None when nothing does."""
    try:
        found = shared.container.get(LOCK)
    except BlobError as error:
        raise shared.unavailable(error, "read the run lock in") from None
    if found is None or found.header("x-ms-lease-state") not in ("leased", "breaking"):
        return None
    return Holder.read(found.body), found.header("etag") or ""


def refusal(holder: Holder | None) -> Stop:
    if holder is None:
        return Stop(
            "failed",
            "Another dp-instance command has just taken the instance's run lock; nothing has changed.",
            reason="locked",
            next=(Next("dp-instance status", "names the command that holds it"),),
        )
    return Stop(
        "failed",
        f"Another dp-instance command holds the instance's run lock: {holder.describe()}. Nothing has "
        "changed; run this command again once it has finished. A command that ended without releasing "
        "the lock, such as one that crashed, leaves it within a minute.",
        reason="locked",
        holder=asdict(holder),
        next=(
            *(
                (
                    Next(
                        f"dp-instance unlock {holder.run}",
                        "clears it, only if that command hangs while its process is still alive",
                    ),
                )
                if holder.run
                else ()
            ),
        ),
    )


class RunLock:
    """The run lock, held by this command."""

    def __init__(self, shared: Shared, holder: Holder):
        self.shared = shared
        self.holder = holder
        self.lease: str | None = None
        self._state = threading.Lock()
        # When the last renewal that succeeded was sent, by the monotonic and the wall clock.
        self._renewed = (0.0, 0.0)
        self._lost: str | None = None
        self._failure: str | None = None
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def acquire(self) -> None:
        """Take the lock, creating its blob the first time, and start renewing it; refuse while
        another command holds it."""
        container = self.shared.container
        lease = str(uuid.uuid4())
        for attempt in range(2):
            sent = (time.monotonic(), time.time())
            try:
                container.lease(LOCK, "acquire", proposed=lease, seconds=LEASE_SECONDS)
                break
            except BlobError as error:
                if error.code == "LeaseAlreadyPresent":
                    found = held(self.shared)
                    raise refusal(found[0] if found else None) from None
                if error.code != "BlobNotFound" or attempt:
                    raise self.shared.unavailable(error, "take the run lock in") from None
            try:
                container.put(LOCK, EMPTY, "application/json", new=True)
            except BlobError as error:
                if error.code != "BlobAlreadyExists":
                    raise self.shared.unavailable(error, "create the run lock in") from None
        self.lease = lease
        self._renewed = sent
        self._thread = threading.Thread(target=self._renew, name="dp-instance run lock", daemon=True)
        self._thread.start()
        self._write()

    def _write(self) -> None:
        """Name the holder on the lock blob, under the lease."""
        body = (
            json.dumps({"schema_version": SCHEMA_VERSION, **asdict(self.holder)}, indent=2) + "\n"
        ).encode()
        try:
            self.shared.container.put(LOCK, body, "application/json", lease=self.lease)
        except BlobError as error:
            raise self.shared.unavailable(error, "name this command on the run lock in") from None

    def name(self, run: str) -> None:
        """Name the run this command walks or records, so `unlock` can name it."""
        if self.holder.run != run:
            self.check()
            self.holder = replace(self.holder, run=run)
            self._write()

    def _renew(self) -> None:
        while not self._stopping.wait(random.uniform(*RENEW_EVERY)):
            sent = (time.monotonic(), time.time())
            try:
                self.shared.container.lease(LOCK, "renew", lease=self.lease)
            except BlobError as error:
                with self._state:
                    if error.code in LOST:
                        self._lost = f"the lease renewal was refused: {error.describe()}"
                        return
                    self._failure = error.describe()
                continue
            except Stop as stop:
                with self._state:
                    self._failure = stop.message
                continue
            with self._state:
                self._renewed, self._failure = sent, None

    def check(self) -> None:
        """Stop the command once the lock is lost, by the renewer's last result alone."""
        with self._state:
            lost, (monotonic, wall), failure = self._lost, self._renewed, self._failure
        if lost is None:
            if max(time.monotonic() - monotonic, time.time() - wall) < LEASE_SECONDS:
                return
            lost = f"no renewal succeeded within the lease's {LEASE_SECONDS} seconds" + (
                f"; the last failed: {failure}" if failure else ""
            )
        raise Stop(
            "failed",
            f"This command lost the instance's run lock, so it stops here; another dp-instance command "
            f"may hold it now. Why: {lost}.",
            reason="lock-lost",
            next=(Next("dp-instance status", "shows who holds the lock and where the run stands"),),
        )

    def release(self) -> None:
        """Stop renewing and release the lock; one that can't be released lapses within the
        lease's minute."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(JOIN_SECONDS)
        if self.lease is None:
            return
        with self._state:
            lost = self._lost
        if lost is None:
            try:
                self.shared.container.put(LOCK, EMPTY, "application/json", lease=self.lease)
                self.shared.container.lease(LOCK, "release", lease=self.lease)
            except (BlobError, Stop):
                pass
        self.lease = None


def unlock(session: Session, run: str) -> Result:
    """`dp-instance unlock RUN`: break the run lock's lease, only while the named run's command
    holds it, and note who cleared it in the run's record."""
    screen = session.screen
    screen.header("unlock", ("instance", session.instance.key), ("run", run))
    shared = session.connect()
    found = held(shared)
    if found is None:
        raise failed(
            f"Nothing holds the instance's run lock, so there's nothing to clear for run {run}.",
            Next("dp-instance status", "shows where the instance stands"),
        )
    holder, etag = found
    if holder is None or holder.run != run:
        shown = holder.describe() if holder is not None else "a command that hasn't named its run yet"
        raise failed(f"Run {run} doesn't hold the instance's run lock: {shown} does. Nothing has changed.")
    try:
        shared.container.lease(LOCK, "break", etag=etag)
    except BlobError as error:
        if error.code in ("ConditionNotMet", "LeaseNotPresentWithLeaseOperation"):
            raise failed(
                f"The run lock changed hands while it was being cleared, so run {run} no longer holds it; "
                "nothing has changed.",
                Next("dp-instance status", "names who holds it now"),
            ) from None
        raise shared.unavailable(error, "clear the run lock in") from None
    shared.note_unlock(run, {**session.operator, "at": now(), "holder": asdict(holder)})
    message = (
        f"Cleared the run lock: {holder.describe()}. That command can't renew it now, so it stops before "
        "its next step."
    )
    screen.blank()
    screen.note(message, "bold green")
    steps = (Next("dp-instance status", "shows where the instance stands and any run to resume"),)
    screen.continuing(steps)
    return Result("unlock", "done", message, steps, {"run": run, "holder": asdict(holder)})
