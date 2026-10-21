"""Reading Git: checkouts, remotes and tags. dp-instance does no Git work in a checkout; the
components commit, push and merge. It only observes, and fetches release manifests and
template sources into its own folder."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from . import processes
from .outcome import failed

COMMIT = re.compile(r"^[0-9a-f]{40}$")
DEVELOPMENT_SUFFIX = "-dev"


def git(*arguments: str, cwd: Path | None = None, check: bool = True) -> processes.Completed:
    return processes.run("git", *arguments, cwd=cwd, check=check)


def is_checkout_root(path: Path) -> bool:
    """Whether ``path`` is the top of a Git checkout (a clone or a linked worktree)."""
    if not (path / ".git").exists():
        return False
    found = git("rev-parse", "--show-toplevel", cwd=path, check=False)
    return found.ok and Path(found.stdout.strip()).resolve() == path.resolve()


def head(path: Path) -> str:
    return git("rev-parse", "HEAD", cwd=path).stdout.strip()


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


def file_history(path: Path, since: str, file: str) -> list[str]:
    """The commits on the checked-out branch since ``since`` that changed ``file``."""
    found = git("log", "--format=%H", f"{since}..HEAD", "--", file, cwd=path, check=False)
    return found.stdout.split() if found.ok else []


def run_branch(change: str) -> str:
    """The branch a run's change names, as the components name it: `Platform 2026.3` is
    `platform-2026.3`."""
    return re.sub(r"[^a-z0-9.]+", "-", change.lower()).strip("-.")


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
    found = git("ls-remote", url, *patterns, check=False)
    if not found.ok:
        raise failed(f"git ls-remote {url} failed: {found.first_error_line()}")
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
    git("fetch", "--quiet", "--depth=1", "--no-tags", url, commit, cwd=destination)
    git("-c", "advice.detachedHead=false", "checkout", "--quiet", "--force", commit, cwd=destination)
