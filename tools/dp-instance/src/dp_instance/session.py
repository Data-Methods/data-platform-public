"""What every command shares: its options, the screen, the instance and the releases, and the
instance's shared run record and run lock.

Every command that finds an instance first refuses one on a release before this dp-instance's
floor, or with a run an earlier dp-instance left unfinished. The commands that read or change the
platform hold the run lock from start to exit, but on a first deployment, before the state
account exists:
until a step creates it, a run keeps its record here and takes no lock, as Terraform's state
starts local, and from that step on it takes the lock and uploads its record so far. Only an
account Azure Resource Manager says doesn't exist goes without the lock; one that exists and
doesn't answer stops the command. While they hold it, the instance's stopped run is the shared
record's unfinished run, or else an unfinished run this workstation kept before the state account
existed: `verify` refuses while there is one, `plan`, `prepare` and the `secret` commands name
it, and a new `apply` or prepared upgrade replaces it only after a question. A stopped run an
earlier walk started is refused, naming the dp-instance that carries it on.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import components, gitops, lock, targets, template_access
from .instance import (
    INFRASTRUCTURE,
    Instance,
    state_folder,
    unmapped_infrastructure,
)
from .manifest import Manifest, Releases, read_file
from .outcome import Next, Stop
from .record import Record, now, runs_folder, under_way
from .screen import Screen
from .shared import Shared, unnamed


def terminal() -> bool:
    """Whether a person is at this command: both stdin and stdout are a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def releases_of(root: Path) -> Releases:
    """The published releases, cached in the instance folder's `.dp-instance/releases.git`."""
    return Releases(state_folder(root) / "releases.git")


def ready(instance: Instance) -> Releases:
    """A found instance's first steps: refuse it when it's on a release before this
    dp-instance's floor or has a run an earlier dp-instance left unfinished."""
    releases = releases_of(instance.root)
    targets.check_floor({key: item.path for key, item in instance.components.items()}, releases)
    targets.check_earlier_run(instance.state)
    return releases


def ready_for_template_access(start: Path) -> None:
    """The same first steps for `template-access`, which also runs before the instance is mapped:
    on the instance a map it reads names or, without one, on the Client Infrastructure Repository
    checkout it runs in, whose folder holds the release cache."""
    root = Instance.locate(start)
    instance = None
    if root is not None:
        try:
            instance = Instance.load(root)
        except Stop:
            instance = None
    if instance is not None:
        ready(instance)
        return
    checkout = unmapped_infrastructure(start.resolve())
    if checkout is not None:
        targets.check_floor({INFRASTRUCTURE: checkout}, releases_of(checkout.parent))


@dataclass(frozen=True)
class Options:
    json: bool = False
    auto_approve: bool = False
    approvals: tuple[str, ...] = ()


class Session:
    def __init__(self, options: Options, start: Path, *, screen: Screen | None = None):
        self.options = options
        self.screen = screen or Screen(json_mode=options.json, interactive=terminal())
        self.instance = Instance.find(start)
        self.instance.check()
        # The instance's template access reaches the Git commands and component CLIs it starts.
        gitops.pass_configuration(template_access.rewrites(self.instance.key, self.instance))
        self.releases = ready(self.instance)
        # The instance's shared run record, located from configuration, and once reached; the
        # run lock while this command holds it, which it releases as it exits.
        self._located: Shared | None = None
        self.shared: Shared | None = None
        self.lock: lock.RunLock | None = None
        self._operator: dict[str, str] | None = None
        self._command: str | None = None
        self._held = ExitStack()
        # The prepare runs read from the shared run record, by ID.
        self._prepared: dict[str, Record | None] = {}

    @property
    def interactive(self) -> bool:
        return self.screen.interactive

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.instance.root).as_posix()
        except ValueError:
            return str(path)

    def located(self) -> Shared:
        """The instance's shared run record, located from configuration, not yet reached."""
        if self._located is None:
            self._located = Shared.of(self.instance)
        return self._located

    @property
    def operator(self) -> dict[str, str]:
        """Who runs this command, by the account the storage token's claims name, and on which
        workstation."""
        if self._operator is None:
            self._operator = lock.operator(self.located())
        return self._operator

    def connect(self) -> Shared:
        """The instance's shared run record, its container created the first time it's needed."""
        if self.shared is None:
            found = self.located()
            found.ready()
            self.shared = found
        return self.shared

    def _account_ready(self) -> bool:
        """Whether the state account exists, reaching the shared run record in it; False only
        when Azure Resource Manager says there's no account yet."""
        try:
            self.connect()
        except Stop as stop:
            if stop.detail.get("reason") != "unanswered" or self.located().account_exists():
                raise
            return False
        return True

    @contextmanager
    def locked(self, command: str) -> Iterator[None]:
        """Hold the instance's run lock while the command runs, unless the state account doesn't
        exist yet, and release it as the command exits."""
        self._command = command
        try:
            if self._account_ready():
                self._take_lock(None)
            yield
        finally:
            self._held.close()
            if self.shared is not None:
                self.shared.lock = None
            self.lock = None

    def _take_lock(self, run: str | None) -> None:
        held = lock.RunLock(self.shared, lock.Holder(self._command, run, *self._holder(), now()))
        self._held.callback(held.release)
        held.acquire()
        self.lock = self.shared.lock = held
        self._held.enter_context(components.guarded(held.check))

    def _holder(self) -> tuple[str, str]:
        return self.operator["who"], self.operator["host"]

    def name(self, run: str) -> None:
        """Name the run or record this command walks on the run lock, when it holds the lock."""
        if self.lock is not None:
            self.lock.name(run)

    def join(self, run: Record) -> None:
        """At a step boundary of a run that started before the state account existed: once a step
        has created it, take the lock, creating the container, and upload the record so far."""
        if not self.located().account_exists():
            return
        self.connect()
        self._take_lock(run.id)
        run.shared, run.join = self.shared, None
        self.shared.publish(run)
        self.shared.name(run)

    def stopped(self) -> Record | None:
        """The instance's stopped run, in this workstation's working copy, while this command holds
        the run lock: the one `current.json` names, or else this workstation's run that it hasn't
        named yet, which is uploaded and named now. Before the state account exists, this
        workstation's own."""
        state = self.instance.state
        if self.lock is None:
            return unnamed(state)
        found = self.shared.unfinished(runs_folder(state))
        if found is not None:
            targets.refuse_earlier_walk(found)
            return found
        left = unnamed(state)
        if left is not None:
            self.shared.publish(left)
            self.shared.name(left)
        return left

    def prepared(self, run: str | None) -> Record | None:
        """A prepare run's record, as the instance's shared run record holds it, fetched into
        this workstation's working copy: the open change's, whose preview `apply` reads."""
        if run is None or self.shared is None:
            return None
        if run not in self._prepared:
            self._prepared[run] = self.shared.fetch(run, runs_folder(self.instance.state))
        return self._prepared[run]

    def candidates(self) -> list[Manifest]:
        """The candidate manifest the open change's prepare ran, when it has one: what `main`
        records once its pull requests are merged, from any workstation."""
        if self.shared is None:
            return []
        found = self.prepared((self.shared.change() or {}).get("run"))
        if found is None or not found.manifest_path.is_file() or found.data.get("channel") != "canary":
            return []
        try:
            return [read_file(found.manifest_path)]
        except Stop:
            return []

    def mark_replaced(self, identifier: str, by: Record) -> None:
        """Mark the stopped run replaced, here and in the shared run record, then make the run
        after it the instance's unfinished run."""
        found = under_way(self.instance.state, identifier)
        if found is not None:
            found.data["status"] = "replaced"
            found.save()
            if self.lock is not None:
                self.shared.publish(found)
        if self.lock is not None:
            self.shared.name(by, replacing=identifier)

    def refuse_while_stopped(self, command: str) -> None:
        """Stop a command that reads the platform as a whole while an apply is stopped, part way
        through; a stopped prepare changed nothing on the platform."""
        run = self.stopped()
        if run is None or run.kind != "apply":
            return
        raise Stop(
            "failed",
            f"Run {run.id} ({run.data['goal']}) {run.stopped_at().rstrip('.')}. Nothing has changed: "
            f"{command} doesn't start while an apply is stopped, part way through.",
            reason="stopped-run",
            run=run.id,
            next=(Next("dp-instance resume", "carries the stopped run on from where it stopped"),),
        )

    def show_stopped(self) -> None:
        """Name the instance's stopped run, if it has one, under the command's first line."""
        run = self.stopped()
        if run is None:
            return
        self.screen.note(
            f"Run {run.id} ({run.data['goal']}) {run.stopped_at().rstrip('.')}. dp-instance resume "
            "carries it on.",
            "bold orange1",
        )
        self.screen.emit({"event": "stopped_run", "run": run.id, "goal": run.data["goal"]})
