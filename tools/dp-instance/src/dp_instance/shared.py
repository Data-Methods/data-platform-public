"""The instance's shared run record: the `platform-runs` container in its Terraform state account,
beside Component Outputs. dp-instance creates it the first time it needs it and alone writes it;
Terraform never manages it.

    lock                      the run lock: a blob lease, and while it's held, who holds it
    current.json              the instance's unfinished run, if it has one
    change.json               the instance's open change, which `prepare` recorded, if it has one
    unfinished.json           each component whose walk an apply left part way, with its commit
    runs/<run>/               each run's record, as `.dp-instance/runs/<run>/` holds it
    runs/<run>/unlocked.json  who cleared the run lock the run held, and when

`.dp-instance/runs/` stays the working copy. While its command holds the run lock, a run uploads
its record at every step boundary: each other file once for each content it has, and the
removal of each handoff document a step deleted, then what's new in `log.txt`, an append blob,
which the state account's blob versioning keeps as one version however often it's appended to,
then `run.json`. `run.json` is written only while the shared
copy is still the one this workstation last wrote or read, and `log.txt` appended only where the
shared copy ends; the other files are written as they are, so after `unlock` of a command that's
still alive, they can mix. `status` and `resume` fetch the instance's unfinished run into the
working copy whenever the shared copy, its `run.json` or its log, is another's, before anything
here writes to it, and who cleared its lock every time. `current.json` names a run only while it
names no other unfinished run, conditional on its ETag; until it names a run this workstation
walked, the run is still findable here, and the next command that holds the lock names it. The
account is the one each component's `config/<component>/component_setup.json` names as
`state_storage_account_id`, and the container's metadata names the Platform Instance, as
Component Outputs' does, so another instance's is refused.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import blobs, processes
from .blobs import BlobError
from .instance import Instance
from .outcome import Next, Stop, failed
from .record import ACTIVE, Record, records, remove, replace
from .sequence import parse_step

CONTAINER = "platform-runs"
LOCK = "lock"
CURRENT = "current.json"
CHANGE = "change.json"
UNFINISHED = "unfinished.json"
# Each step's inputs hash as an apply last left it, by entry.
STEP_HASHES = "step-hashes.json"
RUNS = "runs/"
# A run's handoff documents and pages, and the end of each document's name there.
HANDOFFS = "handoffs/"
HANDOFF = "-handoff.json"
UNLOCKED = "unlocked.json"
LOG = "log.txt"
# Which shared copy of a run this workstation's working copy last matched: its `run.json`'s
# ETag, each file's hash, how many bytes of `log.txt` it holds, and whether `current.json` has
# named the run. It stays on this workstation. Until `current.json` names a run this workstation
# walked, it's still findable here, whichever upload failed.
SYNCED = ".shared.json"
SCHEMA_VERSION = 1
ACCOUNT = re.compile(
    r"/subscriptions/([0-9a-fA-F-]{36})/resourceGroups/([^/]+)"
    r"/providers/Microsoft\.Storage/storageAccounts/([a-z0-9]{3,24})"
)
RUN = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]*")
TYPES = {".json": "application/json", ".md": "text/markdown; charset=utf-8"}


@dataclass(frozen=True)
class Location:
    """The state account and the Platform Instance it belongs to."""

    instance: str
    tenant: str
    subscription: str
    group: str
    account: str

    @property
    def account_id(self) -> str:
        return (
            f"/subscriptions/{self.subscription}/resourceGroups/{self.group}"
            f"/providers/Microsoft.Storage/storageAccounts/{self.account}"
        )

    @property
    def metadata(self) -> dict[str, str]:
        return {
            "platform_key": self.instance,
            "platform_tenant_id": self.tenant,
            "platform_subscription_id": self.subscription,
        }


def location(instance: Instance) -> Location:
    """The state account every mapped repository's `config/<component>/component_setup.json`
    names, with its tenant: they must agree, and name this instance."""
    found: dict[tuple[str, str, str], list[str]] = {}
    for mapped in instance.components.values():
        for path in sorted(mapped.path.glob("config/*/component_setup.json")):
            shown = f"{mapped.stored}/{path.relative_to(mapped.path).as_posix()}"
            try:
                setup = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pointer = setup.get("state_storage_account_id") if isinstance(setup, dict) else None
            tenant = setup.get("tenant_id") if isinstance(setup, dict) else None
            if not isinstance(pointer, str) or not isinstance(tenant, str):
                continue
            match = ACCOUNT.fullmatch(pointer)
            if match is None:
                raise failed(f"{shown}'s state_storage_account_id isn't a storage account's resource ID.")
            key = setup.get("platform_instance_key")
            if key not in (None, instance.key):
                raise failed(f"{shown} belongs to Platform Instance {key}, and the map names {instance.key}.")
            found.setdefault((tenant.lower(), match[1].lower(), match[2], match[3]), []).append(shown)
    if not found:
        raise failed(
            "No mapped repository's config/<component>/component_setup.json names the instance's state "
            "storage account, its state_storage_account_id, which holds the instance's shared run record."
        )
    if len(found) > 1:
        shown = "; ".join(f"{', '.join(files)} name {account}" for (*_, account), files in found.items())
        raise failed(f"The components name different state storage accounts: {shown}.")
    (tenant, subscription, group, account), _ = next(iter(found.items()))
    return Location(instance.key, tenant, subscription, group, account)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _synced(folder: Path) -> dict:
    try:
        found = json.loads((folder / SYNCED).read_text(encoding="utf-8"))
        if (
            isinstance(found, dict)
            and isinstance(found.get("files"), dict)
            and isinstance(found.get("log"), int)
        ):
            return found
    except (OSError, ValueError):
        pass
    return {"etag": None, "files": {}, "log": 0}


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    replace(temporary, path)


def _save_synced(folder: Path, synced: dict) -> None:
    _write(folder / SYNCED, (json.dumps(synced, indent=2) + "\n").encode())


def _local_files(folder: Path) -> dict[str, Path]:
    """The run folder's files by their path inside it, as the shared record names them."""
    return {
        path.relative_to(folder).as_posix(): path
        for path in sorted(folder.rglob("*"))
        if path.is_file() and path.name != SYNCED and not path.name.endswith(".tmp")
    }


def _parts(name: str) -> list[str]:
    """A file's path inside a run folder, part by part; a name that would leave it is refused."""
    parts = name.split("/")
    if any(part in ("", ".", "..") or "\\" in part or ":" in part for part in parts):
        raise failed(f"The shared run record holds a file dp-instance doesn't write: {name}.")
    return parts


def _reached(run: dict) -> set[str]:
    """The components a run's record says its walk reached: each with a step it finished or
    stopped at."""
    stopped = (run.get("stop") or {}).get("entry") or ""
    return {found[1] for found in map(parse_step, [*(run.get("steps") or {}), stopped]) if found}


def _owner(document: str, run: dict) -> str | None:
    """The component of the run whose handoff document this is, `<component>-<authority>-handoff.json`:
    the longest key that names it, as one key can start another."""
    keys = {item.get("component") for item in run.get("entries") or [] if item.get("component")}
    return max((key for key in keys if document.startswith(f"{key}-")), key=len, default=None)


class Shared:
    """The instance's shared run record. ``lock`` is the run lock while this command holds it:
    only then does a run upload."""

    def __init__(self, found: Location, container: blobs.Container):
        self.location = found
        self.container = container
        self.lock = None
        # A completed run whose record this command finished uploading: its last upload had failed.
        self.completed: Record | None = None

    @classmethod
    def of(cls, instance: Instance) -> Shared:
        found = location(instance)
        return cls(found, blobs.Container(found.account, CONTAINER, blobs.Token(found.tenant)))

    @property
    def where(self) -> str:
        account = self.location.account
        return f"the instance's shared run record, container {CONTAINER} in storage account {account}"

    def unavailable(self, error: BlobError, doing: str) -> Stop:
        """A failed call, as a command's stop: what it was doing, where, and Blob Storage's words.
        One that got no answer says so in its reason."""
        said = error.describe().rstrip(".")
        if error.status is None:
            return Stop(
                "failed",
                f"Couldn't {doing} {self.where}: {said}. Work from inside the instance's network.",
                reason="unanswered",
            )
        return failed(f"Couldn't {doing} {self.where}: {said}.")

    def account_exists(self) -> bool:
        """Whether the state account exists, as Azure Resource Manager says, which answers from
        anywhere: a first deployment's runs start before a step creates it."""
        found = processes.run(
            "az",
            "storage",
            "account",
            "show",
            "--ids",
            self.location.account_id,
            "--query",
            "id",
            "--output",
            "tsv",
            "--only-show-errors",
            check=False,
            timeout=blobs.TOKEN_SECONDS,
        )
        if found.ok:
            return True
        if any(code in found.stderr for code in ("ResourceNotFound", "ResourceGroupNotFound")):
            return False
        raise failed(
            f"Couldn't tell whether storage account {self.location.account}, which holds the instance's "
            f"shared run record, exists: {found.first_error_line()}"
        )

    def ready(self, *, create: bool = True) -> bool:
        """Whether the container is there, creating it the first time it's needed unless only
        reading; one another Platform Instance's metadata names is refused."""
        try:
            metadata = self.container.metadata()
            if metadata is None and not create:
                return False
            if metadata is None:
                self.container.create(self.location.metadata)
                metadata = self.container.metadata() or {}
        except BlobError as error:
            raise self.unavailable(error, "reach") from None
        if any(metadata.get(key) != value for key, value in self.location.metadata.items()):
            owner = metadata.get("platform_key") or "no Platform Instance"
            raise failed(
                f"Container {CONTAINER} in storage account {self.location.account} belongs to {owner}, and "
                f"the map names {self.location.instance}; nothing has changed."
            )
        return True

    # The instance's unfinished run --------------------------------------------------------

    def current(self) -> str | None:
        """The run `current.json` names: the instance's unfinished run, once it has started."""
        try:
            found = self.container.get(CURRENT)
        except BlobError as error:
            raise self.unavailable(error, "read") from None
        if found is None:
            return None
        try:
            run = json.loads(found.body).get("run")
        except (ValueError, AttributeError):
            return None
        return run if isinstance(run, str) and RUN.fullmatch(run) else None

    def _write_current(self, run: str | None, found) -> None:
        """Write `current.json`, only while it's as ``found`` read it."""
        body = json.dumps({"schema_version": SCHEMA_VERSION, "run": run}, indent=2) + "\n"
        try:
            self.container.put(
                CURRENT,
                body.encode(),
                TYPES[".json"],
                etag=found.header("etag") if found is not None else None,
                new=found is None,
            )
        except BlobError as error:
            if error.code in ("ConditionNotMet", "BlobAlreadyExists"):
                raise Stop(
                    "failed",
                    "Another dp-instance changed which run is the instance's unfinished one while this one "
                    "was naming it; nothing more runs here.",
                    reason="another-run",
                    next=(Next("dp-instance status", "names the instance's unfinished run"),),
                ) from None
            raise self.unavailable(error, "write") from None

    def _read_current(self):
        try:
            found = self.container.get(CURRENT)
        except BlobError as error:
            raise self.unavailable(error, "read") from None
        try:
            named = json.loads(found.body).get("run") if found is not None else None
        except (ValueError, AttributeError):
            named = None
        return found, named if isinstance(named, str) and RUN.fullmatch(named) else None

    def name(self, record: Record, *, replacing: str | None = None) -> None:
        """Name the run in `current.json` as the instance's unfinished run, only while it names no
        run, this one, the run it replaces, or a run that's no longer unfinished; otherwise stop,
        naming the other run. Once named, the working copy says so."""
        found, named = self._read_current()
        if named not in (None, record.id, replacing):
            other = self.container.get(f"{RUNS}{named}/run.json")
            try:
                status = json.loads(other.body).get("status") if other is not None else None
            except (ValueError, AttributeError):
                status = None
            if status in ACTIVE:
                raise Stop(
                    "failed",
                    f"The instance's unfinished run is run {named}, so run {record.id} can't be named in its "
                    "place; nothing more runs here.",
                    reason="another-run",
                    run=named,
                    next=(Next("dp-instance status", f"shows run {named}"),),
                )
        self._write_current(record.id, found)
        synced = _synced(record.folder)
        synced["named"] = True
        _save_synced(record.folder, synced)

    def finished(self, record: Record) -> None:
        """The run is no longer the instance's unfinished one: `current.json` names no run, if it
        still names this one."""
        found, named = self._read_current()
        if named == record.id:
            self._write_current(None, found)

    def unfinished(self, runs: Path) -> Record | None:
        """The instance's unfinished run, in this workstation's working copy. While this command
        holds the run lock, a working copy that got further than the shared copy, as when an
        upload failed as the run stopped or completed, is uploaded first, and a run it shows
        complete is no longer the unfinished one."""
        run = self.current()
        found = self.fetch(run, runs) if run is not None else None
        if found is None:
            return None
        if self.lock is not None:
            synced = _synced(found.folder)
            written = _digest((found.folder / "run.json").read_bytes())
            logged = found.log_path.stat().st_size if found.log_path.is_file() else 0
            if synced["files"].get("run.json") != written or logged > synced["log"]:
                self.publish(found)
            if found.status not in ACTIVE:
                self.finished(found)
                self.completed = found
        return found if found.status in ACTIVE else None

    # The open change ---------------------------------------------------------------------

    def change(self) -> dict | None:
        """The instance's open change, as `prepare` last recorded it: its kind, branch, title and
        target, the prepare run whose preview it holds, its pull requests and the merged parts
        before it no run has applied; None when there is none."""
        try:
            found = self.container.get(CHANGE)
        except BlobError as error:
            raise self.unavailable(error, "read") from None
        try:
            document = json.loads(found.body) if found is not None else {}
        except ValueError:
            raise failed(f"{CHANGE} in {self.where} isn't a change dp-instance reads.") from None
        change = document.get("change") if isinstance(document, dict) else None
        if isinstance(document, dict) and document.get("schema_version", 1) != SCHEMA_VERSION:
            raise Stop(
                "failed",
                f"{CHANGE} in {self.where} is at a schema this dp-instance doesn't read.",
                next=(Next("dp-instance self update", "installs the newest dp-instance, which reads it"),),
            )
        return change if isinstance(change, dict) else None

    def handoffs(self, before: str) -> dict[str, bytes]:
        """The handoff documents each component's walk last left, by name: a component's from the
        newest apply before run ``before`` that reached it, whether or not that apply completed,
        so an apply that rewrote one and stopped is never seeded over by an older one. A completed
        apply reached every component, and any other each component with a step it finished or
        stopped at; each of its documents is `<component>-<authority>-handoff.json`."""
        documents: dict[str, bytes] = {}
        settled: set[str] = set()
        try:
            names = self.container.names(RUNS)
            runs = sorted(
                {name[len(RUNS) :].split("/", 1)[0] for name in names if name.endswith("/run.json")},
                reverse=True,
            )
            for run in (item for item in runs if item < before and "-apply" in item):
                found = self.container.get(f"{RUNS}{run}/run.json")
                data = json.loads(found.body) if found is not None else {}
                complete = data.get("status") == "complete"
                reached = _reached(data)
                prefix = f"{RUNS}{run}/{HANDOFFS}"
                for name in names:
                    document = name[len(prefix) :]
                    if not name.startswith(prefix) or "/" in document or not document.endswith(HANDOFF):
                        continue
                    owner = _owner(document, data)
                    if owner in settled or not (complete or owner in reached):
                        continue
                    blob = self.container.get(name)
                    if blob is not None:
                        documents[document] = blob.body
                if complete:
                    break
                settled |= reached
        except BlobError as error:
            raise self.unavailable(error, "read") from None
        except ValueError:
            return {}
        return documents

    def optional_handoffs(self) -> tuple[str, dict, dict[str, bytes]]:
        """The newest completed apply: its run, the optional outside work it recorded and each of
        that work's pages, by name."""
        try:
            names = self.container.names(RUNS)
            runs = sorted(
                {name[len(RUNS) :].split("/", 1)[0] for name in names if name.endswith("/run.json")},
                reverse=True,
            )
            for run in (item for item in runs if "-apply" in item):
                found = self.container.get(f"{RUNS}{run}/run.json")
                data = json.loads(found.body) if found is not None else {}
                if data.get("status") != "complete":
                    continue
                optional = data.get("optional") or {}
                prefix = f"{RUNS}{run}/handoffs/"
                pages = {}
                for name in names if optional else ():
                    if name.startswith(prefix) and name.endswith(".md"):
                        blob = self.container.get(name)
                        if blob is not None:
                            pages[name[len(prefix) :]] = blob.body
                return run, optional, pages
        except BlobError as error:
            raise self.unavailable(error, "read") from None
        except ValueError:
            pass
        return "", {}, {}

    def record_change(self, change: dict | None) -> None:
        """Record the instance's open change, or that it has none, while this command holds the run
        lock."""
        body = json.dumps({"schema_version": SCHEMA_VERSION, "change": change}, indent=2) + "\n"
        try:
            self._checked()
            self.container.put(CHANGE, body.encode(), TYPES[".json"])
        except BlobError as error:
            raise self.unavailable(error, "write") from None

    # The walks left part way -------------------------------------------------------------

    def unfinished_walks(self) -> dict[str, dict]:
        """Each component whose walk an apply left part way, by its key: the commit it applied,
        the run and the first step it applied; none once its walk completes."""
        try:
            found = self.container.get(UNFINISHED)
        except BlobError as error:
            raise self.unavailable(error, "read") from None
        try:
            document = json.loads(found.body) if found is not None else {}
        except ValueError:
            raise failed(f"{UNFINISHED} in {self.where} isn't a record dp-instance reads.") from None
        walks = document.get("walks") if isinstance(document, dict) else None
        if isinstance(document, dict) and document.get("schema_version", 1) != SCHEMA_VERSION:
            raise Stop(
                "failed",
                f"{UNFINISHED} in {self.where} is at a schema this dp-instance doesn't read.",
                next=(Next("dp-instance self update", "installs the newest dp-instance, which reads it"),),
            )
        return walks if isinstance(walks, dict) else {}

    def record_unfinished_walks(self, walks: dict[str, dict]) -> None:
        """Record each component whose walk an apply left part way, while this command holds the
        run lock."""
        body = json.dumps({"schema_version": SCHEMA_VERSION, "walks": walks}, indent=2) + "\n"
        try:
            self._checked()
            self.container.put(UNFINISHED, body.encode(), TYPES[".json"])
        except BlobError as error:
            raise self.unavailable(error, "write") from None

    # What each step last applied ---------------------------------------------------------

    def step_hashes(self) -> dict[str, str]:
        """Each step's inputs hash as an apply last left it, by its entry; a hash it can't read
        just means the step plans."""
        try:
            found = self.container.get(STEP_HASHES)
        except BlobError as error:
            raise self.unavailable(error, "read") from None
        try:
            document = json.loads(found.body) if found is not None else {}
        except ValueError:
            return {}
        hashes = document.get("steps") if isinstance(document, dict) else None
        return hashes if isinstance(hashes, dict) else {}

    def record_step_hashes(self, hashes: dict[str, str]) -> None:
        """Record what each step last applied, while this command holds the run lock."""
        body = json.dumps({"schema_version": SCHEMA_VERSION, "steps": hashes}, indent=2) + "\n"
        try:
            self._checked()
            self.container.put(STEP_HASHES, body.encode(), TYPES[".json"])
        except BlobError as error:
            raise self.unavailable(error, "write") from None

    # A run's record ----------------------------------------------------------------------

    def fetch(self, run: str, runs: Path) -> Record | None:
        """The run `current.json` names, in the working copy: as it is when the shared copy, its
        `run.json` and its log alike, is the one this workstation last wrote or read, with who
        cleared its lock read again; else fetched whole, before anything here writes to it. None
        when the shared record has none."""
        prefix = f"{RUNS}{run}/"
        folder = runs / run
        try:
            found = self.container.get(f"{prefix}run.json")
            if found is None:
                return None
            etag = found.header("etag") or ""
            synced = _synced(folder)
            if (
                (folder / "run.json").is_file()
                and synced["etag"] == etag
                and synced["log"] == (self.container.size(prefix + LOG) or 0)
            ):
                unlocked = self.container.get(prefix + UNLOCKED)
                if unlocked is not None:
                    _write(folder / UNLOCKED, unlocked.body)
                return Record.load(folder)
            _save_synced(folder, {"etag": None, "files": {}, "log": 0, "named": True})
            synced = {"etag": etag, "files": {"run.json": _digest(found.body)}, "log": 0, "named": True}
            fetched = {"run.json"}
            for name in self.container.names(prefix):
                inside = name[len(prefix) :]
                if inside == "run.json":
                    continue
                blob = self.container.get(name)
                if blob is None:
                    continue
                _write(folder.joinpath(*_parts(inside)), blob.body)
                fetched.add(inside)
                if inside == LOG:
                    synced["log"] = len(blob.body)
                elif inside != UNLOCKED:
                    synced["files"][inside] = _digest(blob.body)
        except BlobError as error:
            raise self.unavailable(error, f"fetch run {run} from") from None
        _write(folder / "run.json", found.body)
        for inside, path in _local_files(folder).items():
            if inside not in fetched:
                remove(path)
        _save_synced(folder, synced)
        return Record.load(folder)

    def _checked(self) -> None:
        """Before each upload: a token in hand, however long the Azure CLI took to issue it, and
        the run lock still held."""
        self.container.token.value()
        self.lock.check()

    def _append(self, name: str, log: bytes, at: int) -> int:
        """Append the next part of ``log`` after its first ``at`` bytes, and return how many the
        shared copy holds now. An append whose answer was lost, so the shared copy already holds
        more of this log, carries on from there; one that holds anything else is refused."""
        try:
            chunk = log[at : at + blobs.APPEND_BYTES]
            self.container.append(name, chunk, at, "text/plain; charset=utf-8")
            return at + len(chunk)
        except BlobError as error:
            if error.code != "AppendPositionConditionNotMet":
                raise
            found = self.container.get(name)
            held = found.body if found is not None else b""
            if len(held) <= at or not log.startswith(held):
                raise
            return len(held)

    def publish(self, record: Record) -> None:
        """Upload the run's record, while this command holds the run lock: each other file whose
        content changed since the working copy last matched the shared copy, with each handoff
        document deleted here deleted there, then what's new in `log.txt`, then `run.json`, only
        while the shared copy is still that one."""
        folder = record.folder
        if not (folder / SYNCED).exists():
            _save_synced(folder, {"etag": None, "files": {}, "log": 0})
        synced = _synced(folder)
        prefix = f"{RUNS}{record.id}/"
        try:
            local = _local_files(folder)
            # A handoff document the run's steps deleted, as a step's last action went, goes from
            # the shared copy too, so no later apply's seed brings it back.
            for inside in [
                name for name in synced["files"] if name.startswith(HANDOFFS) and name not in local
            ]:
                self._checked()
                self.container.delete(prefix + inside)
                del synced["files"][inside]
                _save_synced(folder, synced)
            for inside, path in local.items():
                if inside in ("run.json", LOG, UNLOCKED):
                    continue
                data = path.read_bytes()
                if synced["files"].get(inside) != _digest(data):
                    self._checked()
                    self.container.put(
                        prefix + inside,
                        data,
                        TYPES.get(path.suffix, "text/plain; charset=utf-8"),
                        timeout=120,
                    )
                    synced["files"][inside] = _digest(data)
                    _save_synced(folder, synced)
            log = record.log_path.read_bytes() if record.log_path.is_file() else b""
            if len(log) < synced["log"]:
                raise failed(f"{record.log_path} holds less than its shared copy; it was changed by hand.")
            while len(log) > synced["log"]:
                self._checked()
                synced["log"] = self._append(prefix + LOG, log, synced["log"])
                _save_synced(folder, synced)
            body = (folder / "run.json").read_bytes()
            self._checked()
            synced["etag"] = self.container.put(
                f"{prefix}run.json", body, TYPES[".json"], etag=synced["etag"], new=synced["etag"] is None
            )
            synced["files"]["run.json"] = _digest(body)
        except BlobError as error:
            if error.code in ("ConditionNotMet", "BlobAlreadyExists", "AppendPositionConditionNotMet"):
                raise Stop(
                    "failed",
                    f"Another dp-instance has carried run {record.id} on since this one last wrote its "
                    "record, so this one stops here; nothing more runs from this workstation.",
                    reason="lock-lost",
                    next=(Next("dp-instance status", "shows where the run stands now"),),
                ) from None
            raise Stop(
                "failed",
                self.unavailable(error, f"upload run {record.id}'s record to").message,
                reason="shared-record",
                next=(
                    Next(
                        "dp-instance resume",
                        "uploads the run's record and carries on, once the shared run record answers",
                    ),
                ),
            ) from None
        _save_synced(folder, synced)

    def note_unlock(self, run: str, note: dict) -> None:
        """Add who cleared the run lock to the run's record, `runs/<run>/unlocked.json`."""
        name = f"{RUNS}{run}/{UNLOCKED}"
        try:
            found = self.container.get(name)
            notes = json.loads(found.body).get("unlocks", []) if found is not None else []
            body = json.dumps({"schema_version": SCHEMA_VERSION, "unlocks": [*notes, note]}, indent=2) + "\n"
            self.container.put(
                name,
                body.encode(),
                TYPES[".json"],
                etag=found.header("etag") if found is not None else None,
                new=found is None,
            )
        except (BlobError, ValueError, AttributeError) as error:
            shown = error.describe() if isinstance(error, BlobError) else "it doesn't read"
            raise failed(
                f"The run lock is cleared, but run {run}'s record of it couldn't be written: {shown}."
            ) from None


def unlocks(record: Record) -> list[dict]:
    """Who cleared the run lock this run held, as its record keeps it."""
    try:
        found = json.loads((record.folder / UNLOCKED).read_text(encoding="utf-8")).get("unlocks", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [item for item in found if isinstance(item, dict)]


def unnamed(state: Path) -> Record | None:
    """This workstation's unfinished run that `current.json` hasn't named: one it kept before the
    state account existed, or one whose naming failed. A fetched run is named; a run that never
    started isn't one of the workstation's runs."""
    found = next(iter(records(state)), None)
    if found is None or found.status not in ACTIVE or _synced(found.folder).get("named"):
        return None
    return found
