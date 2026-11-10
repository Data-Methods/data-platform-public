"""Release manifests: one small file per Platform Version, read through Git.

`platform-releases/<Platform Version>.json` in `data-platform-public`, added by the release's
last commit and tagged `platform-release/<Platform Version>`. `git ls-remote` lists the tags,
with the `dp-instance/X.Y.Z` tags of dp-instance's published versions; a shallow fetch reads
each manifest into `.dp-instance/releases.git`, using Git's own TLS and proxy settings. Tags
are never moved, so a fetched manifest is kept, and a tag that moved stops every command.

Which dp-instance runs a release is never read from its manifest: each dp-instance knows only
its own floor (`targets.FLOOR`), and a manifest it can't read names the newest dp-instance, or a
candidate's development commit, which canary installs. An instance's records are matched against
every published manifest's raw JSON `sources`, whatever its schema (`Releases.documents`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import gitops, installed
from .outcome import Next, failed

# What a manifest this dp-instance can't read names: the newest, which reads it.
NEWEST = Next("dp-instance self update", "installs the newest dp-instance, which reads it")

RELEASES_URL = "https://github.com/Data-Methods/data-platform-public.git"
TAG_PREFIX = "refs/tags/platform-release/"
TOOL_TAG_PREFIX = "refs/tags/dp-instance/"
SCHEMA_VERSION = 2
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


def reader(document: dict, origin: str) -> Next:
    """The command that installs a dp-instance that reads a manifest this one can't: the
    development commit a candidate names, or the newest published."""
    commit = commit_of(document, origin)
    if commit is None:
        return NEWEST
    return Next(
        install_command(commit, development=True),
        f"installs the candidate's dp-instance, development commit {commit[:12]}, which reads it",
    )


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
    platform_version: str
    sources: dict[str, str]
    order: tuple[Repository, ...]
    text: str
    origin: str

    @property
    def change(self) -> str:
        """The change an upgrade to this release makes: it names its branch and pull requests."""
        return f"Platform {self.platform_version}"

    def repository(self, key: str) -> Repository | None:
        return next((item for item in self.order if item.key == key), None)


def _require(condition: bool, origin: str, message: str) -> None:
    if not condition:
        raise failed(f"The manifest {origin} is invalid: {message}.")


def commit_of(document: dict, origin: str) -> str | None:
    """The development commit of dp-instance a candidate manifest's JSON names, when it names a
    full one; a published manifest's is never read."""
    found = None if published(origin) else document.get("dp_instance_commit")
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
        raise failed(
            f"The manifest {origin} is schema version {schema}; this dp-instance reads version "
            f"{SCHEMA_VERSION}.",
            reader(document, origin),
        )
    _require(schema == SCHEMA_VERSION, origin, f"schema_version must be {SCHEMA_VERSION}")
    version = document.get("platform_version")
    _require(
        isinstance(version, str) and VERSION.fullmatch(version) is not None,
        origin,
        "platform_version must be YYYY.N or YYYY.N.P",
    )
    _require(
        commit_of(document, origin) is not None
        or published(origin)
        or document.get("dp_instance_commit") is None,
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
    return Manifest(version, dict(sources), tuple(repositories), text, origin)


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
        self._documents: list[tuple[str, dict]] | None = None

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
        instance's records, before any is read as a run's target. Read once per command."""
        if self._documents is None:
            self.fetch_all()
            found = []
            for version in reversed(list(self.published())):
                try:
                    document = json.loads(self.text(version))
                except ValueError:
                    continue
                if isinstance(document, dict):
                    found.append((version, document))
            self._documents = found
        return self._documents

    def released(self, template: str, commit: str) -> bool:
        """Whether a published manifest names ``commit`` as ``template``'s: a Template Release's
        commit, in its Release Repository."""
        return any(
            isinstance(document.get("sources"), dict) and document["sources"].get(template) == commit
            for _, document in self.documents()
        )

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
