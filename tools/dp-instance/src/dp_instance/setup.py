"""`dp-instance setup`: map the instance's repositories.

It scans the current folder and any paths already in `dp-instance.json`, recognising each
Component Repository by its template source record and the Managed Repository through Data
Engineering's configuration; compares them with the instance's published Component Outputs,
read through the Client Infrastructure Repository; shows what it found, what's missing,
duplicated or on another remote; and on approval writes the map. It refuses while a
repository the instance has is missing or on another remote, naming the `git clone` for each,
and in a folder with no Component Repository it names the one to clone first: the Client
Infrastructure Repository. It never moves, clones or deletes anything, and refuses a folder a
sync client manages.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text

from . import gitops, synced
from .components import Cli, Log
from .instance import (
    INFRASTRUCTURE,
    INFRASTRUCTURE_COMMAND,
    MAP_FILE,
    Instance,
    Mapped,
    SourceRecord,
    registrations,
    stored_path,
)
from .outcome import Next, Result, Stop, failed
from .screen import Screen

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
    managed_names: dict[str, str] = field(default_factory=dict)


@dataclass
class Compared:
    """The checkouts against the instance's published Component Outputs."""

    missing: list[str] = field(default_factory=list)
    wrong: list[str] = field(default_factory=list)
    unregistered: list[str] = field(default_factory=list)


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
        for control in sorted(path.glob("config/*/source_control.json")):
            name = ((_json(control) or {}).get("managed_repository") or {}).get("name")
            if isinstance(name, str):
                found.managed_names[name] = control.parent.name
    return found


def _outputs(root: Path, found: Found, screen: Screen) -> tuple[dict | None, str | None]:
    """The published Component Outputs, read by the Client Infrastructure Repository's own
    CLI, or why they couldn't be read."""
    paths = found.components.get(INFRASTRUCTURE) or []
    if len(paths) != 1:
        return None, "no single Client Infrastructure Repository to read them with"
    cli = Cli(INFRASTRUCTURE_COMMAND, paths[0], Log(None))
    try:
        if screen.interactive:
            with screen.console.status("Reading the published Component Outputs"):
                return cli.document("outputs", "aggregate"), None
        return cli.document("outputs", "aggregate"), None
    except Stop as stop:
        return None, stop.message


def compare(outputs: dict, found: Found, repositories: list[Mapped]) -> Compared:
    """Each repository a registered component names must be checked out, on that remote; a
    Component Repository on another remote of the same name is on the wrong one, and one no
    component registers is only noted, since a component registers once it first publishes."""
    compared = Compared()
    components = [item for item in repositories if item.template]
    registered = sorted({url for _, url in registrations(outputs)})
    loose = [
        item for item in components if not any(gitops.same_repository(url, item.remote) for url in registered)
    ]
    for url in registered:
        if any(gitops.same_repository(url, item.remote) for item in components):
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
        compared.wrong.append(
            f"{namesake.stored}'s origin is {namesake.remote}; the instance registers {url}"
        )
    compared.unregistered = [
        f"{item.stored}: {item.remote} isn't a repository the instance registers yet" for item in loose
    ]
    github = next(
        (
            ((entry.get("outputs") or {}).get("exports") or {}).get("github", {}).get("value")
            for entry in outputs.get("components", [])
            if isinstance(entry, dict)
            and (entry.get("registration") or {}).get("component", {}).get("key") == "infrastructure"
        ),
        None,
    )
    for name, maintainer in sorted(found.managed_names.items()):
        if not any(item.managed_by == maintainer for item in repositories) and isinstance(github, dict):
            compared.missing.append(f"https://{github.get('host')}/{github.get('organization')}/{name}")
    return compared


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
    existing = None
    if (root / MAP_FILE).is_file():
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
    for path in found.others:
        remote = gitops.origin(path)
        if remote is None:
            continue
        maintainer = found.managed_names.get(gitops.repository_name(remote))
        if maintainer is not None:
            repositories.append(
                Mapped(path.resolve(), stored_path(root, path), remote, managed_by=maintainer)
            )

    outputs, unread = _outputs(root, found, screen)
    compared = compare(outputs, found, repositories) if outputs is not None else Compared()

    screen.header(
        "setup: map this instance's repositories", ("instance", key or "unknown"), ("folder", str(root))
    )
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
    for line in compared.unregistered:
        screen.labelled("Not Registered:", line)
    for line in problems:
        screen.labelled("Problem:", line, "bold red")
    screen.labelled(
        "Component Outputs:",
        f"{len(outputs.get('components', []))} registered components compared"
        if outputs is not None
        else f"not compared: {unread}",
    )
    detail = {
        "instance": key,
        "repositories": [item.describe() for item in repositories],
        "missing": compared.missing,
        "wrong_remote": compared.wrong,
        "not_registered": compared.unregistered,
        "problems": problems,
        "outputs": None if outputs is not None else unread,
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
                next=(Next(command, f"writes {MAP_FILE} as shown"),),
                decision={"id": identifier, "summary": f"write {MAP_FILE}", "resume": command},
                **detail,
            )
        if screen.choose([("y", f"write {MAP_FILE}"), ("n", "stop")], "Look good?") != "y":
            raise Stop("decision", "Nothing was written.", **detail)
    path = proposed.save()
    proposed.prepare()
    screen.blank()
    screen.labelled("Written:", str(path))
    steps = (
        Next("dp-instance status", "shows where the instance stands"),
        Next("dp-instance template-access check", "checks the template access each repository uses"),
    )
    screen.next_block(steps)
    return Result("setup", "done", f"Wrote {MAP_FILE}.", steps, detail)
