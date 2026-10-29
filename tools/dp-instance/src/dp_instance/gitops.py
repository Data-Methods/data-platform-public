"""Reading Git: checkouts, remotes and tags. dp-instance commits, pushes and merges nothing in a
checkout; the components do. It observes, fetches a Managed Repository's tags to read its
release, and fetches release manifests and template sources into its own folder. A read from
a remote that fails for a moment is retried."""

from __future__ import annotations

import ntpath
import os
import re
import shutil
import stat
import sys
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


def remote_git(*arguments: str, cwd: Path | None = None) -> processes.Completed:
    """A Git command that reaches a remote. A failure that looks temporary, such as a dropped
    connection, is retried after a wait, up to three times; the last failure stops, saying
    whether it looked temporary."""
    retried = 0
    while True:
        found = git(*arguments, cwd=cwd, check=False)
        if found.ok:
            return found
        temporary = transient.classify(found.stderr)
        if not transient.retries(temporary, retried, label=f"git {arguments[0]}"):
            break
        retried += 1
    words = " ".join(("git", *arguments[:2]))
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


def uncommitted(path: Path) -> list[str]:
    """The tracked files in a checkout with changes not committed yet, staged or not."""
    entries = iter(git("status", "--porcelain=v1", "-z", "--untracked-files=no", cwd=path).stdout.split("\0"))
    files = []
    for entry in entries:
        if not entry:
            continue
        files.append(entry[3:])
        # A rename's or copy's entry is followed by its old path.
        if entry[0] in "RC":
            next(entries, None)
    return files


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


def run_branch(change: str) -> str:
    """The branch a run's change names, as the components name it: `Platform 2026.3` is
    `platform-2026.3`."""
    return re.sub(r"[^a-z0-9.]+", "-", change.lower()).strip("-.")


def has_branch(path: Path, name: str) -> bool:
    """Whether the checkout has the local branch."""
    return git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", cwd=path, check=False).ok


def on_origin(path: Path, name: str) -> bool:
    """Whether the checkout's origin has the branch, asked of origin itself, once: a failure
    stops, naming Git's own first line of error."""
    return bool(git("ls-remote", "--heads", "origin", f"refs/heads/{name}", cwd=path).stdout.strip())


def applied_checkout(source: Path, commit: str, destination: Path) -> Path:
    """A throwaway checkout of one commit of a local repository, sharing its objects, with the
    same origin URL, so a component reads it as that repository. Nothing is fetched."""
    git("clone", "--quiet", "--shared", "--no-checkout", str(source), str(destination))
    git("checkout", "--quiet", "-B", "main", commit, cwd=destination)
    remote = origin(source)
    if remote:
        git("remote", "set-url", "origin", remote, cwd=destination)
    return destination


def contains(path: Path, ancestor: str, descendant: str) -> bool:
    return git("merge-base", "--is-ancestor", ancestor, descendant, cwd=path, check=False).ok


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
