"""`dp-instance setup`: map the instance's repositories.

It scans the current folder and any paths already in the repository map, recognising each
Component Repository by its template source record. Like every command that finds an
instance, it first refuses one on a release before this dp-instance's floor, as the checkouts it
found record it. It reads `config/infrastructure/components.json` in the Client Infrastructure
Repository, which names every repository the instance has, each component's own and the Managed
Repositories it lists. It checks each repository alike; shows what
it found, what's missing, duplicated or on another remote; and on approval writes the map,
`.dp-instance/repositories.json`. It refuses while a repository the instance has is missing or
on another remote, naming the `git clone` for each, and in a folder with no Component
Repository it names the one to clone first: the Client Infrastructure Repository. It never
moves, clones or deletes a repository, and refuses a folder a sync client manages.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from rich.text import Text

from . import gitops, synced, targets
from .instance import (
    INFRASTRUCTURE,
    MAP_FILE,
    Instance,
    Mapped,
    SourceRecord,
    map_path,
    stored_path,
)
from .outcome import Next, Result, Stop, failed
from .screen import Screen
from .session import releases_of

START = (
    Next(
        "git clone <Client Infrastructure Repository URL>",
        "clones the instance's Client Infrastructure Repository into this folder; whoever asked for "
        "the work has its URL",
    ),
    Next("dp-instance setup", "maps it, and prints a git clone command for each other repository"),
)


@dataclass
class Found:
    root: Path
    components: dict[str, list[Path]] = field(default_factory=dict)
    others: list[Path] = field(default_factory=list)
    keys: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Compared:
    """The checkouts against the repositories the instance lists."""

    missing: list[str] = field(default_factory=list)
    wrong: list[str] = field(default_factory=list)
    unlisted: list[str] = field(default_factory=list)


def _json(path: Path) -> dict | None:
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def _candidates(root: Path, existing: Instance | None) -> list[Path]:
    paths = [child for child in sorted(root.iterdir()) if child.is_dir() and not child.name.startswith(".")]
    if existing is not None:
        paths += [item.path for item in existing.repositories]
    unique: dict[Path, Path] = {}
    for path in paths:
        unique.setdefault(path.resolve(), path)
    return [path for path in unique if gitops.is_checkout_root(path)]


def scan(root: Path, existing: Instance | None) -> Found:
    found = Found(root)
    for path in _candidates(root, existing):
        record = SourceRecord.read(path)
        if record is None:
            found.others.append(path)
            continue
        found.components.setdefault(record.template, []).append(path)
        for setup in sorted(path.glob("config/*/component_setup.json")):
            key = (_json(setup) or {}).get("platform_instance_key")
            if isinstance(key, str):
                found.keys.setdefault(key, []).append(f"{path.name}/{setup.relative_to(path).as_posix()}")
    return found


COMPONENTS = "config/infrastructure/components.json"


@dataclass
class Named:
    """The repositories the instance has, as its list names them."""

    components: list[str]
    # Each Managed Repository's URL and the component that lists it.
    managed: dict[str, str]
    said: str


def _listed(found: Found) -> tuple[Named | None, str | None]:
    """The repositories `components.json` in the Client Infrastructure Repository names, or
    None and why it can't be read."""
    paths = found.components.get(INFRASTRUCTURE) or []
    if len(paths) != 1:
        return None, "no single Client Infrastructure Repository to read it in"
    if not (paths[0] / COMPONENTS).is_file():
        return None, f"the Client Infrastructure Repository holds no {COMPONENTS}"
    document = _json(paths[0] / COMPONENTS)
    members = document.get("components") if isinstance(document, dict) else None
    if not isinstance(members, dict):
        raise failed(f"{paths[0] / COMPONENTS} can't be read; nothing was written.")
    return Named(
        sorted({member["repository"] for member in members.values()}),
        dict(
            sorted(
                {
                    item["url"]: key
                    for key, member in members.items()
                    for item in member.get("managed_repositories", ())
                }.items()
            )
        ),
        f"{len(members)} components listed in {COMPONENTS}",
    ), None


def _check(
    named: list[str], checkouts: list[Mapped], compared: Compared
) -> tuple[dict[str, Mapped], list[Mapped]]:
    """Each named repository's checkout, by its URL, and the checkouts nothing names. One that
    isn't checked out is missing, and a checkout on another remote of the same name is on the
    wrong one."""
    found = {}
    for url in named:
        match = next((item for item in checkouts if gitops.same_repository(url, item.remote)), None)
        if match is not None:
            found[url] = match
    loose = [item for item in checkouts if item not in found.values()]
    for url in named:
        if url in found:
            continue
        namesake = next(
            (
                item
                for item in loose
                if gitops.repository_name(item.remote).casefold() == gitops.repository_name(url).casefold()
            ),
            None,
        )
        if namesake is None:
            compared.missing.append(url)
            continue
        loose.remove(namesake)
        compared.wrong.append(f"{namesake.stored}'s origin is {namesake.remote}; the instance lists {url}")
    return found, loose


def compare(named: Named, components: list[Mapped], others: list[Mapped]) -> tuple[Compared, list[Mapped]]:
    """Every repository the instance names, each component's own and each Managed Repository a
    component lists, must be checked out, on that remote. A checkout nothing names is only noted.
    Returns the comparison and each Managed Repository's checkout, mapped to the component that
    lists it."""
    compared = Compared()
    _, unnamed = _check(named.components, components, compared)
    managed, others_unnamed = _check(list(named.managed), others, compared)
    compared.unlisted = [
        f"{item.stored}: {item.remote} isn't a repository the instance lists yet"
        for item in (*unnamed, *others_unnamed)
    ]
    return compared, [replace(item, managed_by=named.managed[url]) for url, item in managed.items()]


def setup(start: Path, screen: Screen, approvals: tuple[str, ...]) -> Result:
    root = start.resolve()
    where = synced.provider(root)
    if where is not None:
        raise failed(
            f"{root} is in a folder {where} syncs, which rewrites files under Git and Terraform while "
            "they run.",
            Next(
                "dp-instance setup",
                f"run in an instance folder outside {where}, such as one under your home folder",
            ),
        )
    screen.begin("the checkouts")
    existing = None
    if map_path(root) is not None:
        try:
            existing = Instance.load(root)
        except Stop:
            existing = None
    found = scan(root, existing)
    if not found.components:
        raise Stop(
            "failed",
            "No Component Repository is in this folder. Start from the instance's Client Infrastructure "
            "Repository: clone it here and run setup again, which reads what the instance has published "
            "and prints a git clone command for each other repository.",
            next=START,
            folder=str(root),
        )
    targets.check_floor(
        {template: paths[0] for template, paths in found.components.items()}, releases_of(root)
    )
    problems: list[str] = []
    for path in [*(p for paths in found.components.values() for p in paths), *found.others]:
        provider = synced.provider(path)
        if provider is not None:
            problems.append(f"{path} is in a folder {provider} syncs; move it out, then map it again")
    for template, paths in sorted(found.components.items()):
        if len(paths) > 1:
            problems.append(f"{template} is checked out twice: {', '.join(str(path) for path in paths)}")
    if INFRASTRUCTURE not in found.components:
        problems.append(
            "the Client Infrastructure Repository isn't here: clone it into this folder, since setup "
            "reads what the instance has published through it"
        )
    keys = sorted(found.keys)
    if len(keys) > 1:
        problems.append(
            "the Component Repositories name different Platform Instance Keys: " + ", ".join(keys)
        )
    key = keys[0] if keys else (existing.key if existing else None)
    if key is None and not problems:
        problems.append("no Component Repository here names its Platform Instance Key")

    repositories: list[Mapped] = []
    for template, paths in sorted(found.components.items()):
        remote = gitops.origin(paths[0])
        if remote is None:
            problems.append(f"{paths[0]} has no origin remote")
            continue
        repositories.append(
            Mapped(paths[0].resolve(), stored_path(root, paths[0]), remote, template=template)
        )
    # Every other checkout, which the registrations may name as a Managed Repository.
    others = [
        Mapped(path.resolve(), stored_path(root, path), remote)
        for path in found.others
        if (remote := gitops.origin(path)) is not None
    ]

    screen.header(
        "setup: map this instance's repositories", ("instance", key or "unknown"), ("folder", str(root))
    )
    named, unread = _listed(found)
    compared = Compared()
    if named is not None:
        compared, managed = compare(named, repositories, others)
        repositories += managed
    screen.stage("what setup found")
    for item in repositories:
        what = (
            f"the {item.template} Component Repository"
            if item.template
            else f"the Managed Repository {item.managed_by} maintains"
        )
        screen.labelled("Found:", Text.assemble((item.stored, "bold"), f": {what}, {item.remote}"))
    for url in compared.missing:
        screen.labelled("Missing:", f"{url} (git clone {url} in this folder, then setup again)", "bold red")
    for line in compared.wrong:
        screen.labelled("Wrong Remote:", line, "bold red")
    for line in compared.unlisted:
        screen.labelled("Not Listed:", line)
    for line in problems:
        screen.labelled("Problem:", line, "bold red")
    screen.labelled(
        "Compared With:",
        named.said if named is not None else f"nothing: {unread}",
    )
    detail = {
        "instance": key,
        "repositories": [item.describe() for item in repositories],
        "missing": compared.missing,
        "wrong_remote": compared.wrong,
        "not_listed": compared.unlisted,
        "problems": problems,
        "components_list": unread,
    }
    if problems or compared.missing or compared.wrong:
        clones = tuple(
            Next(f"git clone {url}", "clones a repository the instance has into this folder")
            for url in compared.missing
        )
        if INFRASTRUCTURE not in found.components:
            clones = START[:1] + clones
        raise Stop(
            "failed",
            "Setup found problems to fix first; nothing was written.",
            next=(*clones, Next("dp-instance setup", "maps the repositories again once they're fixed")),
            **detail,
        )
    proposed = Instance(root, key, repositories)
    document = json.dumps([item.describe() for item in repositories] + [key], sort_keys=True)
    identifier = hashlib.sha256(document.encode()).hexdigest()[:12]
    if identifier not in approvals:
        if not screen.interactive:
            command = f"dp-instance setup --approve {identifier}"
            raise Stop(
                "decision",
                "Review what setup found, then approve it to write the map.",
                next=(Next(command, f"writes {MAP_FILE.as_posix()} as shown"),),
                decision={"id": identifier, "summary": f"write {MAP_FILE.as_posix()}", "resume": command},
                **detail,
            )
        if screen.choose([("y", f"write {MAP_FILE.as_posix()}"), ("n", "stop")], "Look good?") != "y":
            raise Stop("decision", "Nothing was written.", **detail)
    path = proposed.save()
    screen.blank()
    screen.labelled("Written:", str(path))
    steps = (
        Next(
            "dp-instance template-access setup",
            "restores each mapped template's key from the vault and sets up its access",
        ),
    )
    screen.continuing(steps)
    return Result("setup", "done", f"Wrote {MAP_FILE.as_posix()}.", steps, detail)
