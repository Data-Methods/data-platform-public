"""Git: checkouts, remotes and tags. dp-instance commits and pushes only in its own copies, and
merges nothing; in your checkout it commits only what you say to. It fetches a Managed
Repository's tags to read its release, and release manifests and template sources into its own
folder. A read from a remote that fails for a moment is retried."""

from __future__ import annotations

import ntpath
import os
import re
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from . import processes, transient
from .outcome import Stop, failed

COMMIT = re.compile(r"^[0-9a-f]{40}$")
DEVELOPMENT_SUFFIX = "-dev"
# The first Git that reads configuration from its environment, `GIT_CONFIG_COUNT`.
CONFIGURATION_ENVIRONMENT = (2, 31)


def git(*arguments: str, cwd: Path | None = None, check: bool = True) -> processes.Completed:
    return processes.run("git", *arguments, cwd=cwd, check=check)


def remote_git(
    *arguments: str,
    cwd: Path | None = None,
    answer: Callable[[processes.Completed], bool] | None = None,
) -> processes.Completed:
    """A Git command that reaches a remote. A failure ``answer`` accepts is the remote's answer,
    returned as it is. Any other that looks temporary, such as a dropped connection, is retried
    after a wait, up to three times; the last failure stops, saying whether it looked
    temporary."""
    # What the messages name: the command, after any `-c` setting.
    command = arguments[2:] if arguments[:1] == ("-c",) else arguments
    retried = 0
    while True:
        found = git(*arguments, cwd=cwd, check=False)
        if found.ok or (answer is not None and answer(found)):
            return found
        temporary = transient.classify(found.stderr)
        if not transient.retries(temporary, retried, label=f"git {command[0]}"):
            break
        retried += 1
    words = " ".join(("git", *command[:2]))
    message = transient.exhausted(f"{words} failed: {found.first_error_line()}", temporary, retried)
    if temporary is not None:
        raise Stop("failed", message, reason="transient")
    raise failed(message)


def require_configuration_environment() -> None:
    """Git reads configuration from its environment, `GIT_CONFIG_COUNT`, from 2.31 on; an
    older Git ignores it without a word, so it stops the command."""
    found = git("--version").stdout.strip()
    match = re.search(r"(\d+)\.(\d+)(?:\.\d+)?", found)
    if match is None or (int(match.group(1)), int(match.group(2))) < CONFIGURATION_ENVIRONMENT:
        version = match.group(0) if match else found or "unknown"
        raise failed(
            f"Git {version} is older than 2.31, the first Git that takes template access from "
            "dp-instance. Install Git 2.31 or newer, then run the command again."
        )


def pass_configuration(entries: list[tuple[str, str]]) -> None:
    """Pass these Git configuration entries to every tool dp-instance starts from now on."""
    if entries:
        require_configuration_environment()
    processes.configure_git(entries)


def extended_length(folder: str | os.PathLike[str]) -> str:
    """On Windows, the folder's extended-length path, `\\\\?\\C:\\...` or
    `\\\\?\\UNC\\server\\share\\...`, which Windows' file functions take beyond 260 characters
    whether long paths are on or off; elsewhere, the folder's path."""
    if sys.platform != "win32":
        return os.path.abspath(folder)
    path = ntpath.abspath(folder)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def remove(folder: Path) -> None:
    """Remove a folder of dp-instance's own, such as a fetched source and its environment, if
    there is one, through its extended-length path on Windows, so a file deeper than Windows'
    260 characters goes too. Git leaves its object files read only, which Windows won't delete
    until they're made writable."""

    def writable(function, path, _):
        os.chmod(path, stat.S_IWRITE)
        function(path)

    if folder.exists():
        shutil.rmtree(extended_length(folder), onexc=writable)


def is_checkout_root(path: Path) -> bool:
    """Whether ``path`` is the top of a Git checkout (a clone or a linked worktree)."""
    if not (path / ".git").exists():
        return False
    found = git("rev-parse", "--show-toplevel", cwd=path, check=False)
    return found.ok and Path(found.stdout.strip()).resolve() == path.resolve()


def head(path: Path) -> str:
    return git("rev-parse", "HEAD", cwd=path).stdout.strip()


def commit_of(path: Path) -> str | None:
    """The checked-out commit, or None in a repository with none, such as one a fetch that
    stopped part way left."""
    found = git("rev-parse", "--verify", "--quiet", "HEAD^{commit}", cwd=path, check=False)
    return (found.stdout.strip() or None) if found.ok else None


def uncommitted(path: Path, *, untracked: bool = False) -> list[str]:
    """The tracked files in a checkout with changes not committed yet, staged or not, and with
    ``untracked``, the files Git doesn't track and doesn't ignore."""
    shown = "all" if untracked else "no"
    entries = iter(
        git("status", "--porcelain=v1", "-z", f"--untracked-files={shown}", cwd=path).stdout.split("\0")
    )
    files = []
    for entry in entries:
        if not entry:
            continue
        files.append(entry[3:])
        # A rename's or copy's entry is followed by its old path.
        if entry[0] in "RC":
            next(entries, None)
    return files


def untracked(path: Path) -> list[str]:
    """The files in a checkout Git doesn't track and doesn't ignore."""
    found = git("ls-files", "--others", "--exclude-standard", "-z", cwd=path).stdout
    return [name for name in found.split("\0") if name]


def branch(path: Path) -> str | None:
    """The checked-out branch, or None when detached."""
    found = git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=path, check=False)
    return (found.stdout.strip() or None) if found.ok else None


def origin(path: Path) -> str | None:
    """The stored origin URL, not one a workstation's `insteadOf` rule rewrites."""
    found = git("config", "--get", "remote.origin.url", cwd=path, check=False)
    return (found.stdout.strip() or None) if found.ok else None


def show(path: Path, revision: str, file: str) -> str | None:
    """A file's content at a revision, or None when either is missing locally."""
    found = git("show", f"{revision}:{file}", cwd=path, check=False)
    return found.stdout if found.ok else None


def main_commit(path: Path) -> str | None:
    """The commit of `main` as last fetched, `origin/main`, or the local `main` without one:
    the code last merged, which is what the platform runs."""
    for revision in ("origin/main", "main"):
        found = git("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}", cwd=path, check=False)
        if found.ok:
            return found.stdout.strip()
    return None


def unmerged(path: Path, upstream: str, head: str, merged: tuple[str, ...] = ()) -> list[str]:
    """The commits of ``head`` that ``upstream`` hasn't, oldest first, merges left out. Dropped:
    those reachable from a commit ``merged`` names, as a merged change's prepare took them; every
    commit up to the newest at which all of ``head``'s work since its merge base with ``upstream``
    holds there, as a squash merge of several commits leaves it; one whose change ``upstream``
    holds by its patch, as a rebase merge leaves it; and one every file of which ``upstream``
    already holds as the commit leaves it."""
    found = git("merge-base", upstream, head, cwd=path, check=False)
    base = found.stdout.strip() if found.ok else None
    excluded = [f"^{commit}" for commit in merged]
    listed = git(
        "rev-list", "--reverse", "--no-merges", f"{base}..{head}" if base else head, *excluded, cwd=path
    ).stdout.split()
    cut = 0
    if base is not None:
        for index, commit in enumerate(listed):
            if holds_since(path, upstream, base, commit):
                cut = index + 1
    patched = {
        line[2:]
        for line in git("cherry", upstream, head, cwd=path).stdout.splitlines()
        if line.startswith("+ ")
    }
    return [commit for commit in listed[cut:] if commit in patched and not holds(path, upstream, commit)]


def changed_files(path: Path, *revisions: str) -> set[str]:
    return set(git("diff", "--name-only", "-z", *revisions, cwd=path).stdout.split("\0")) - {""}


def holds_since(path: Path, upstream: str, base: str, commit: str) -> bool:
    """Whether ``upstream`` holds every file changed from ``base`` to ``commit`` as ``commit``
    leaves it."""
    return not changed_files(path, base, commit) & changed_files(path, commit, upstream)


def holds(path: Path, upstream: str, commit: str) -> bool:
    """Whether ``upstream`` holds every file ``commit`` changes as the commit leaves it, so
    adding the commit to it changes nothing."""
    changed = set(
        git(
            "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--root", commit, cwd=path
        ).stdout.split("\0")
    ) - {""}
    return not changed & changed_files(path, commit, upstream)


def run_branch(change: str) -> str:
    """The branch a run's change names, as the components name it: `Platform 2026.3` is
    `platform-2026.3`."""
    return re.sub(r"[^a-z0-9.]+", "-", change.lower()).strip("-.")


def blob(path: Path, revision: str, file: str) -> bytes | None:
    """A file's bytes at a revision, exactly as committed, or None when either is missing
    locally."""
    return processes.output("git", "cat-file", "blob", f"{revision}:{file}", cwd=path)


def remote_refs(url: str, *patterns: str) -> dict[str, str]:
    """`git ls-remote`: each matching ref and the object it names, peeled for annotated tags."""
    found = remote_git("ls-remote", url, *patterns)
    refs: dict[str, str] = {}
    for line in found.stdout.splitlines():
        sha, _, name = line.partition("\t")
        if not name:
            continue
        if name.endswith("^{}"):
            refs[name.removesuffix("^{}")] = sha
        else:
            refs.setdefault(name, sha)
    return refs


def parse_repository_url(url: str) -> tuple[str, str]:
    """A GitHub HTTPS or SSH URL's host and OWNER/REPOSITORY, without `.git`."""
    if url.startswith("git@"):
        match = re.fullmatch(r"git@([A-Za-z0-9.-]+):(.+)", url)
        if not match:
            raise failed(f"{url} isn't a repository URL dp-instance reads.")
        host, path = match.groups()
    else:
        parsed = urlsplit(url)
        if parsed.scheme == "file":
            return "", parsed.path.removesuffix(".git").rstrip("/")
        if parsed.scheme not in {"https", "ssh"} or not parsed.hostname:
            raise failed(f"{url} isn't a repository URL dp-instance reads.")
        host, path = parsed.hostname, parsed.path.lstrip("/")
    path = path.removesuffix(".git").rstrip("/")
    return host.lower(), path


def repository_name(url: str) -> str:
    return parse_repository_url(url)[1].rsplit("/", 1)[-1]


def pull_request_repository(url: str) -> str | None:
    """The repository a GitHub pull request's URL, `https://<host>/<owner>/<name>/pull/<number>`,
    is in, as its HTTPS URL; None for any other URL."""
    parsed = urlsplit(url)
    parts = parsed.path.strip("/").split("/")
    if parsed.scheme != "https" or not parsed.hostname or len(parts) != 4 or parts[2] != "pull":
        return None
    return f"https://{parsed.hostname}/{parts[0]}/{parts[1]}"


def same_repository(first: str, second: str) -> bool:
    """Whether two URLs name the same repository, whatever their protocol or SSH alias."""
    return parse_repository_url(first)[1].casefold() == parse_repository_url(second)[1].casefold()


def counterpart_url(url: str, *, development: bool) -> str:
    """The clone URL of a template's other repository, as the engine derives it: its
    development repository (`<name>-dev`), or back to its release repository. An SSH host
    alias takes the same `-dev` suffix, as template access names it."""
    host, path = parse_repository_url(url)
    name = path.removesuffix(DEVELOPMENT_SUFFIX)
    target = name + DEVELOPMENT_SUFFIX if development else name
    if url.startswith("file:"):
        return f"file://{target}.git"
    if url.startswith("git@"):
        alias = host.removesuffix(DEVELOPMENT_SUFFIX)
        if host != "github.com":
            alias = alias + DEVELOPMENT_SUFFIX if development else alias
        return f"git@{alias}:{target}.git"
    return f"https://{host}/{target}.git"


def fetch_commit(destination: Path, url: str, commit: str) -> None:
    """A checkout of exactly one commit, fetched shallowly with the instance's own access."""
    if not COMMIT.fullmatch(commit):
        raise failed(f"{commit} isn't a full commit SHA.")
    destination.mkdir(parents=True, exist_ok=True)
    if not (destination / ".git").exists():
        git("init", "--quiet", cwd=destination)
    remote_git("fetch", "--quiet", "--depth=1", "--no-tags", url, commit, cwd=destination)
    git("-c", "advice.detachedHead=false", "checkout", "--quiet", "--force", commit, cwd=destination)
