"""dp-instance's own folders for each Component Repository: your checkout is only an input.

    .dp-instance/prepare/<hash>/  the change's branch, which `prepare` builds
    .dp-instance/plan/<hash>/     the change's branch, which `plan` builds and previews
    .dp-instance/apply/<hash>/    `origin/main`, or the commit a stopped apply planned, which `apply`
                                  and `verify` run, and `status` reads under the run lock
    .dp-instance/applied/<hash>/  the commit a component's walk last applied, which the drift check
                                  plans, one component's commit after another's
    .dp-instance/src/<hash>/      the repository's template source at an upgrade's target

Each but `src/` is a clone of the checkout, its objects hard-linked, with the checkout's origin
URL and its user, credential, SSH, HTTP, signing, include and long-path configuration, which
follow the checkout's each time the copy is used. It keeps its environment between commands
and is brought to its commit with Git alone: what a step leaves untracked goes, and what Git
ignores, such as the environment, Terraform's providers and the engine's machine-local files,
stays.

A prepare copy's pushes, such as a component's commit on the change's branch, land in its own
outbox, `.git/outbox.git`, which each build starts empty: nothing reaches GitHub until the
change's pull request step pushes the branch. `tidy` removes every folder nothing uses: a
repository's the map no longer holds, and those of earlier walks, `.dp-instance/main/` and
`.dp-instance/sources/`.
"""

from __future__ import annotations

from pathlib import Path

from . import gitops, transient
from .instance import Instance, Mapped, SourceRecord
from .outcome import Next, failed

PREPARE = "prepare"
PLAN = "plan"
APPLY = "apply"
APPLIED = "applied"
SOURCE = "src"
OWN = (PREPARE, PLAN, APPLY, APPLIED, SOURCE)
# The folders of earlier walks.
RETIRED = ("main", "sources")
OUTBOX = Path(".git", "outbox.git")
# The checkout's configuration a copy carries: who commits, how it signs in, reaches GitHub and
# signs, the files it includes, and Windows' long paths.
CARRIED = (
    r"^(user\.|credential\.|gpg\.|http\.|include\.|includeif\.|commit\.gpgsign$|tag\.gpgsign$"
    r"|core\.longpaths$|core\.sshcommand$)"
)


def clone(instance: Instance, kind: str, mapped: Mapped) -> Path:
    """The repository's copy of this kind, cloned from its checkout the first time, with the
    checkout's origin URL and carried configuration."""
    folder = instance.own(kind, mapped.template or mapped.name)
    if not gitops.is_checkout_root(folder):
        gitops.remove(folder)
        folder.parent.mkdir(parents=True, exist_ok=True)
        gitops.git("clone", "--quiet", "--no-checkout", str(mapped.path), str(folder))
    gitops.git("remote", "set-url", "origin", gitops.origin(mapped.path) or mapped.remote, cwd=folder)
    carry(mapped.path, folder)
    return folder


def carry(checkout: Path, folder: Path) -> None:
    """Give the copy the checkout's own carried configuration, in place of what it had."""
    for key, _ in configured(folder):
        gitops.git("config", "--local", "--unset-all", key, cwd=folder, check=False)
    for key, value in configured(checkout):
        gitops.git("config", "--local", "--add", key, value, cwd=folder)


def configured(path: Path) -> list[tuple[str, str]]:
    """Each carried configuration entry the repository sets itself, in order."""
    found = gitops.git("config", "--local", "--null", "--get-regexp", CARRIED, cwd=path, check=False)
    entries = []
    for item in found.stdout.split("\0") if found.ok else []:
        if item:
            key, _, value = item.partition("\n")
            entries.append((key, value if "\n" in item else "true"))
    return entries


def hold_pushes(folder: Path) -> None:
    """Send every push from the prepare copy to its outbox, which each build starts empty, so a
    component's push of the change's branch lands there and the pull request step pushes it."""
    box = folder / OUTBOX
    if not (box / "HEAD").is_file():
        gitops.remove(box)
        gitops.git("init", "--quiet", "--bare", str(box))
        # The copy's objects, which the outbox reads, so a push sends only what's new.
        (box / "objects" / "info" / "alternates").write_text(
            (folder / ".git" / "objects").resolve().as_posix() + "\n", encoding="utf-8"
        )
    held = gitops.git("--git-dir", str(box), "for-each-ref", "--format=%(refname)", check=False)
    for ref in held.stdout.split():
        gitops.git("--git-dir", str(box), "update-ref", "-d", ref, check=False)
    # Git rewrites every push to origin's URL into the outbox, while the URLs origin stores, which
    # a component reads, stay GitHub's.
    url = gitops.origin(folder)
    if url is None:
        raise failed(f"{folder.name} has no origin to hold its pushes from.")
    gitops.git("config", "--local", "--unset-all", "remote.origin.pushurl", cwd=folder, check=False)
    gitops.git(
        "config", "--local", "--replace-all", f"url.{box.resolve().as_posix()}.pushInsteadOf", url, cwd=folder
    )


def fetch(folder: Path) -> None:
    """Bring the copy's view of its origin up to date: every branch, as GitHub holds it."""
    gitops.remote_git("fetch", "--quiet", "--prune", "origin", cwd=folder)


def has(folder: Path, commit: str) -> bool:
    return gitops.git("cat-file", "-e", f"{commit}^{{commit}}", cwd=folder, check=False).ok


def checkout(folder: Path, commit: str, branch: str = "main") -> Path:
    """The copy at ``commit``, on ``branch``, the engine's checks needing a branch: the commit
    fetched from its origin when the copy hasn't it, and anything a step left untracked gone."""
    if not has(folder, commit):
        # A failure that doesn't look temporary is the origin's answer: it hasn't the commit.
        gitops.remote_git(
            "fetch",
            "--quiet",
            "origin",
            commit,
            cwd=folder,
            answer=lambda found: transient.classify(found.stderr) is None,
        )
    if not has(folder, commit):
        raise Missing(gitops.origin(folder) or str(folder), commit)
    gitops.git(
        "-c", "advice.detachedHead=false", "checkout", "--quiet", "--force", "-B", branch, commit, cwd=folder
    )
    gitops.git("clean", "--quiet", "-fd", cwd=folder)
    return folder


class Missing(Exception):
    """A commit the copy's origin doesn't have."""

    def __init__(self, origin: str, commit: str):
        super().__init__(f"{origin} has no commit {commit}")
        self.origin, self.commit = origin, commit


def main_of(instance: Instance, mapped: Mapped) -> str:
    """The commit `main` holds on GitHub, fetched into the repository's apply copy; for a
    repository whose origin has no `main` yet, as one that joins before its first push, the
    checkout's own `main`, which its first push sends."""
    folder = clone(instance, APPLY, mapped)
    fetch(folder)
    found = gitops.git(
        "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main", cwd=folder, check=False
    )
    if found.ok:
        return found.stdout.strip()
    local = gitops.git("rev-parse", "--verify", "--quiet", "refs/heads/main", cwd=mapped.path, check=False)
    if not local.ok:
        raise failed(f"{mapped.stored} has no main, on GitHub or in its checkout, to apply.")
    gitops.git("fetch", "--quiet", str(mapped.path), "main", cwd=folder)
    return local.stdout.strip()


def at(instance: Instance, kind: str, mapped: Mapped, commit: str) -> Path:
    """The repository's copy of this kind at ``commit``, on `main`. A commit its origin doesn't
    have stops, naming it."""
    try:
        return checkout(clone(instance, kind, mapped), commit)
    except Missing as missing:
        raise failed(f"{missing}; nothing has changed.") from None


def mains(instance: Instance) -> tuple[dict[str, str], dict[str, Path]]:
    """Each mapped Component Repository's `main` on GitHub, and its apply copy at it."""
    commits = {key: main_of(instance, mapped) for key, mapped in instance.components.items()}
    copies = {key: at(instance, APPLY, instance.components[key], commit) for key, commit in commits.items()}
    return commits, copies


def records(copies: dict[str, Path]) -> dict[str, SourceRecord]:
    """Each copy's template source record."""
    found = {}
    for key, copy in copies.items():
        recorded = SourceRecord.read(copy)
        if recorded is None:
            raise failed(f"{key}'s main has no template source record.")
        found[key] = recorded
    return found


def tidy(instance: Instance) -> None:
    """Remove every folder of dp-instance's own that nothing uses: those of earlier walks, and
    any of a repository the map no longer holds. One Windows holds open goes next time."""
    kept = {instance.own(APPLY, key).name for key in instance.components}
    found = [instance.state / name for name in RETIRED]
    for kind in OWN:
        folder = instance.state / kind
        if folder.is_dir():
            found += [item for item in folder.iterdir() if item.name not in kept]
    for item in found:
        try:
            gitops.remove(item)
        except OSError:
            continue


def missing_commit(instance: Instance, repository: str, component: str, command: str, commit: str) -> Next:
    """What carries on once GitHub no longer has the commit a component's walk last applied:
    record `main` as it, at the accountable person's word that the platform runs it."""
    mapped = instance.components[repository]
    return Next(
        f"uv run --locked {command} outputs mark-applied",
        f"in {mapped.stored}, on main as GitHub has it: records it as the commit {component}'s walk "
        f"last applied, in place of {commit[:12]}, which GitHub no longer has; only once the "
        "accountable person has confirmed the platform runs it",
    )


def edits(
    instance: Instance, commits: dict[str, str], branch: str | None
) -> dict[str, tuple[list[str], int]]:
    """Each Component Repository whose checkout holds edits that aren't committed, or commits
    `main` on GitHub hasn't, by the rule `prepare` takes them by, leaving out those the open
    change's branch, ``branch``, already holds, a squash-merged branch's included: its files not
    committed and how many commits."""
    found = {}
    for key, item in instance.components.items():
        files = gitops.uncommitted(item.path, untracked=True)
        ahead = 0
        if key in commits:
            copy = instance.own(APPLY, key)
            gitops.git("fetch", "--quiet", str(item.path), "HEAD", cwd=copy)
            taken = gitops.unmerged(copy, commits[key], "FETCH_HEAD")
            held = gitops.git(
                "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", cwd=copy, check=False
            )
            if branch and held.ok:
                plus = gitops.git("cherry", f"refs/remotes/origin/{branch}", "FETCH_HEAD", cwd=copy).stdout
                taken = [commit for commit in taken if f"+ {commit}" in plus.splitlines()]
            ahead = len(taken)
        if files or ahead:
            found[key] = (files, ahead)
    return found
