"""Release manifests: one small file per Platform Version, read through Git.

`platform-releases/<Platform Version>.json` in `data-platform-public`, added by the release's
last commit and tagged `platform-release/<Platform Version>`. `git ls-remote` lists the tags,
with the `dp-instance/X.Y.Z` tags of dp-instance's published versions; a shallow fetch reads
each manifest into `.dp-instance/releases.git`, using Git's own TLS and proxy settings. Tags
are never moved, so a fetched manifest is kept, and a tag that moved stops every command.

A manifest at schema version 2 names the oldest dp-instance version it runs. Platform 2026.3's,
at schema version 1, name the dp-instance they run by its commit: this dp-instance reads them
for `status` and as where an upgrade starts, and runs nothing else on them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import gitops, installed
from .outcome import Next, failed

RELEASES_URL = "https://github.com/Data-Methods/data-platform-public.git"
TAG_PREFIX = "refs/tags/platform-release/"
TOOL_TAG_PREFIX = "refs/tags/dp-instance/"
SCHEMA_VERSION = 2
# Platform 2026.3's manifests, which name the dp-instance they run by its commit. Removed at the
# 2026.5 stop.
COMMIT_SCHEMA = 1
VERSION = re.compile(r"^(\d{4})\.(\d+)(?:\.(\d+))?$")
TEMPLATE_KEY = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
COMPONENT_KEY = re.compile(r"^[a-z][a-z0-9_-]*$")
COMMAND = re.compile(r"^[a-z][a-z0-9-]*$")


def version_key(version: str) -> tuple[int, int, int]:
    match = VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"{version} isn't a Platform Version (YYYY.N or YYYY.N.P).")
    year, number, fix = match.groups()
    return int(year), int(number), int(fix or 0)


def family(version: str) -> tuple[int, int]:
    """A Platform Version and its fix releases share a family: 2026.3 and 2026.3.1."""
    return version_key(version)[:2]


def install_spec(revision: str, *, development: bool = False) -> str:
    """What uv installs dp-instance from: a published version's tag, or a commit, of the public
    repository, or a candidate's commit of its development repository."""
    repository = "data-platform-public-dev" if development else "data-platform-public"
    return f"git+https://github.com/Data-Methods/{repository}@{revision}#subdirectory=tools/dp-instance"


def install_command(revision: str, *, development: bool = False) -> str:
    """The command that installs dp-instance at a revision."""
    return f'uv tool install --reinstall "{install_spec(revision, development=development)}"'


def tool_tag(version: str) -> str:
    """The tag of a published dp-instance version."""
    return f"dp-instance/{version}"


def published(origin: str) -> bool:
    """Whether a manifest came from a release tag rather than a candidate file."""
    return origin.startswith(TAG_PREFIX.removeprefix("refs/tags/"))


def install_of(minimum: str | None, commit: str | None, origin: str) -> str | None:
    """The command that installs the dp-instance a manifest runs: its minimum version's tag, or
    the commit a candidate or a Platform 2026.3 manifest names."""
    candidate = not published(origin)
    if commit is not None and (candidate or minimum is None):
        return install_command(commit, development=candidate)
    return install_command(tool_tag(minimum)) if minimum is not None else None


@dataclass(frozen=True)
class Component:
    key: str
    command: str


@dataclass(frozen=True)
class AfterMerge:
    """A phase that reads its repository's merged commit, so it runs after the merge."""

    component: str
    phase: str


@dataclass(frozen=True)
class Repository:
    key: str
    components: tuple[Component, ...]
    after_merge: tuple[AfterMerge, ...] = ()

    @property
    def owner(self) -> Component:
        """The Repository Owner comes first in its repository."""
        return self.components[0]


@dataclass(frozen=True)
class Manifest:
    schema_version: int
    platform_version: str
    # The oldest dp-instance version the release runs; None at schema version 1.
    minimum: str | None
    # The commit Platform 2026.3's dp-instance installs, which 2026.4's manifests keep once so it
    # points to the first versioned dp-instance; a candidate's names its development commit.
    dp_instance_commit: str | None
    sources: dict[str, str]
    order: tuple[Repository, ...]
    text: str
    origin: str

    @property
    def change(self) -> str:
        """The change a run to this release makes: it names the run's branch and pull requests."""
        return f"Platform {self.platform_version}"

    def repository(self, key: str) -> Repository | None:
        return next((item for item in self.order if item.key == key), None)

    def install(self, then: str = "") -> Next:
        """The command that installs the dp-instance the release runs, and ``then``, what comes
        after it."""
        return Next(
            install_of(self.minimum, self.dp_instance_commit, self.origin),
            f"installs the dp-instance Platform {self.platform_version} runs{then}",
        )

    def runs_on(self, own: str | None) -> bool:
        """Whether dp-instance at version ``own`` runs the release: its minimum or a newer one.
        A Platform 2026.3 manifest names its dp-instance by commit, so no version runs it."""
        return self.schema_version == SCHEMA_VERSION and installed.at_least(own, self.minimum)

    def check_install(self, own: str | None) -> None:
        """Stop unless dp-instance at version ``own`` runs the release. On Platform 2026.3, this
        dp-instance runs only `status`, `update` and the upgrade to the next Platform Version."""
        if self.runs_on(own):
            return
        shown = f"dp-instance {own}" if own else "a local copy of dp-instance"
        if self.schema_version == COMMIT_SCHEMA:
            message = (
                f"Platform {self.platform_version} runs dp-instance at commit {self.dp_instance_commit}; "
                f"this is {shown}, which runs on it only status, update and an upgrade to the next "
                "Platform Version."
            )
        else:
            message = (
                f"Platform {self.platform_version} runs dp-instance {self.minimum} or later; this is {shown}."
            )
        raise failed(message, self.install("; then run the same command again"))


def _require(condition: bool, origin: str, message: str) -> None:
    if not condition:
        raise failed(f"The manifest {origin} is invalid: {message}.")


def minimum_of(document: dict) -> str | None:
    """The oldest dp-instance version a manifest's JSON names, when it names a valid one."""
    found = document.get("dp_instance")
    found = found.get("minimum") if isinstance(found, dict) else None
    return found if isinstance(found, str) and installed.VERSION.fullmatch(found) else None


def commit_of(document: dict) -> str | None:
    """The dp-instance commit a manifest's JSON names, when it names a full one."""
    found = document.get("dp_instance_commit")
    return found if isinstance(found, str) and gitops.COMMIT.fullmatch(found) else None


def parse(text: str, origin: str) -> Manifest:
    """Read and check a target manifest; ``origin`` names where it came from."""
    try:
        document = json.loads(text)
    except ValueError:
        raise failed(f"The manifest {origin} isn't JSON.") from None
    _require(isinstance(document, dict), origin, "it isn't a JSON object")
    schema = document.get("schema_version")
    if isinstance(schema, int) and schema > SCHEMA_VERSION:
        command = install_of(minimum_of(document), commit_of(document), origin)
        raise failed(
            f"The manifest {origin} is schema version {schema}; this dp-instance reads versions "
            f"{COMMIT_SCHEMA} and {SCHEMA_VERSION}.",
            *((Next(command, "installs the dp-instance that reads it"),) if command else ()),
        )
    _require(
        schema in (COMMIT_SCHEMA, SCHEMA_VERSION),
        origin,
        f"schema_version must be {COMMIT_SCHEMA} or {SCHEMA_VERSION}",
    )
    version = document.get("platform_version")
    _require(
        isinstance(version, str) and VERSION.fullmatch(version) is not None,
        origin,
        "platform_version must be YYYY.N or YYYY.N.P",
    )
    minimum = minimum_of(document) if schema == SCHEMA_VERSION else None
    _require(
        minimum is not None or schema == COMMIT_SCHEMA,
        origin,
        "dp_instance.minimum must be a dp-instance version, X.Y.Z",
    )
    commit = commit_of(document)
    _require(
        commit is not None or (schema == SCHEMA_VERSION and document.get("dp_instance_commit") is None),
        origin,
        "dp_instance_commit must be a full commit SHA",
    )
    sources = document.get("sources")
    _require(isinstance(sources, dict) and bool(sources), origin, "sources must name templates")
    for key, value in sources.items():
        _require(TEMPLATE_KEY.fullmatch(key) is not None, origin, f"{key!r} isn't a template key")
        _require(
            isinstance(value, str) and gitops.COMMIT.fullmatch(value) is not None,
            origin,
            f"sources.{key} must be a full commit SHA",
        )
    order = document.get("order")
    _require(isinstance(order, list) and bool(order), origin, "order must list repositories")
    repositories = []
    for item in order:
        _require(isinstance(item, dict), origin, "each order entry must be an object")
        key = item.get("repository")
        _require(key in sources, origin, f"order names {key!r}, which sources doesn't")
        components = item.get("components")
        _require(
            isinstance(components, list) and bool(components),
            origin,
            f"{key} must list its components",
        )
        parsed = []
        for component in components:
            _require(
                isinstance(component, dict)
                and isinstance(component.get("key"), str)
                and COMPONENT_KEY.fullmatch(component["key"]) is not None
                and isinstance(component.get("command"), str)
                and COMMAND.fullmatch(component["command"]) is not None,
                origin,
                f"{key}'s components must each have a key and a command",
            )
            parsed.append(Component(component["key"], component["command"]))
        keys = [component.key for component in parsed]
        _require(len(set(keys)) == len(keys), origin, f"{key} lists a component twice")
        after = []
        for entry in item.get("after_merge", []):
            _require(
                isinstance(entry, dict)
                and entry.get("component") in keys
                and isinstance(entry.get("phase"), str),
                origin,
                f"{key}'s after_merge entries must name one of its components and a phase",
            )
            after.append(AfterMerge(entry["component"], entry["phase"]))
        repositories.append(Repository(key, tuple(parsed), tuple(after)))
    keys = [item.key for item in repositories]
    _require(len(set(keys)) == len(keys), origin, "order lists a repository twice")
    _require(set(keys) == set(sources), origin, "order and sources must name the same templates")
    return Manifest(schema, version, minimum, commit, dict(sources), tuple(repositories), text, origin)


def read_file(path: Path) -> Manifest:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise failed(f"Can't read the manifest {path}: {error.strerror or error}.") from None
    return parse(text, str(path))


def tool_versions(refs: dict[str, str]) -> list[str]:
    """The dp-instance versions a listing of data-platform-public's tags names, oldest first."""
    found = [name.removeprefix(TOOL_TAG_PREFIX) for name in refs if name.startswith(TOOL_TAG_PREFIX)]
    return sorted((item for item in found if installed.VERSION.fullmatch(item)), key=installed.key)


class Releases:
    """The published manifests, cached in a bare repository in `.dp-instance/`."""

    def __init__(self, cache: Path, url: str | None = None):
        self.cache = cache
        self.url = url or RELEASES_URL
        self._refs: dict[str, str] | None = None
        self._published: dict[str, str] | None = None

    def _ready(self) -> None:
        if not (self.cache / "HEAD").exists():
            self.cache.mkdir(parents=True, exist_ok=True)
            gitops.git("init", "--quiet", "--bare", str(self.cache))

    def refs(self) -> dict[str, str]:
        """The release tags and dp-instance's version tags, listed once."""
        if self._refs is None:
            self._refs = gitops.remote_refs(self.url, f"{TAG_PREFIX}*", f"{TOOL_TAG_PREFIX}*")
        return self._refs

    def published(self) -> dict[str, str]:
        """Every published Platform Version and the commit its tag names, oldest first."""
        if self._published is None:
            found = {}
            for name, sha in self.refs().items():
                version = name.removeprefix(TAG_PREFIX)
                if name.startswith(TAG_PREFIX) and VERSION.fullmatch(version):
                    found[version] = sha
            self._published = dict(sorted(found.items(), key=lambda item: version_key(item[0])))
        return self._published

    def tools(self) -> list[str]:
        """Every published dp-instance version, oldest first."""
        return tool_versions(self.refs())

    def text(self, version: str) -> str:
        """The manifest of a published Platform Version, fetched once."""
        published = self.published()
        if version not in published:
            shown = ", ".join(published) or "none yet"
            raise failed(f"Platform {version} isn't published. Published: {shown}.")
        self._ready()
        tag = f"{TAG_PREFIX}{version}"
        local = gitops.git(
            "rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}", cwd=self.cache, check=False
        )
        if local.ok and local.stdout.strip() != published[version]:
            raise failed(
                f"The tag platform-release/{version} moved: it named {local.stdout.strip()} "
                f"and now names {published[version]}. Release tags never move; tell Data Methods."
            )
        if not local.ok:
            gitops.remote_git(
                "fetch", "--quiet", "--depth=1", "--no-tags", self.url, f"{tag}:{tag}", cwd=self.cache
            )
            local = gitops.git("rev-parse", f"{tag}^{{commit}}", cwd=self.cache)
            if local.stdout.strip() != published[version]:
                raise failed(f"The fetched platform-release/{version} isn't the published commit.")
        content = gitops.show(self.cache, tag, f"platform-releases/{version}.json")
        if content is None:
            raise failed(f"platform-release/{version} holds no platform-releases/{version}.json.")
        return content

    def fetch_all(self) -> None:
        """Fetch every published manifest not fetched yet, in one shallow fetch."""
        published = self.published()
        self._ready()
        missing = [
            version
            for version in published
            if not gitops.git(
                "rev-parse",
                "--verify",
                "--quiet",
                f"{TAG_PREFIX}{version}^{{commit}}",
                cwd=self.cache,
                check=False,
            ).ok
        ]
        if missing:
            refspecs = [f"{TAG_PREFIX}{version}:{TAG_PREFIX}{version}" for version in missing]
            gitops.remote_git(
                "fetch", "--quiet", "--depth=1", "--no-tags", self.url, *refspecs, cwd=self.cache
            )

    def documents(self) -> list[tuple[str, dict]]:
        """Every published manifest's JSON, newest first, as published: for matching an
        instance's records, before any is read as a run's target."""
        self.fetch_all()
        found = []
        for version in reversed(list(self.published())):
            try:
                document = json.loads(self.text(version))
            except ValueError:
                continue
            if isinstance(document, dict):
                found.append((version, document))
        return found

    def manifest(self, version: str) -> Manifest:
        manifest = parse(self.text(version), f"platform-release/{version}")
        if manifest.platform_version != version:
            raise failed(f"platform-release/{version} holds the manifest of {manifest.platform_version}.")
        return manifest

    def families(self) -> list[tuple[int, int]]:
        return sorted({family(version) for version in self.published()})

    def latest_of(self, wanted: tuple[int, int]) -> str | None:
        """The newest published release of a family."""
        found = [version for version in self.published() if family(version) == wanted]
        return found[-1] if found else None
