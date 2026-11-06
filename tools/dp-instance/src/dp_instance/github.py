"""The pull requests of a prepared change, through the GitHub CLI with the operator's sign-in,
each command naming its repository with `--repo`: one pull request per changed repository,
into `main`, from the change's branch. Their review and merge are GitHub's; `dp-instance` opens
and describes them, and reads their state."""

from __future__ import annotations

import json
from pathlib import Path

from . import gitops, processes
from .outcome import failed

# A pull request's state as `gh` names it, as dp-instance names it.
STATES = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}


def repository(url: str) -> str:
    """`gh`'s `--repo` for a repository's URL: `HOST/OWNER/NAME`."""
    host, path = gitops.parse_repository_url(url)
    return f"{host}/{path}" if host else path


def _gh(folder: Path, url: str, *arguments: str, body: str | None = None) -> str:
    return processes.run("gh", *arguments, "--repo", repository(url), cwd=folder, input=body).stdout


def open_for(folder: Path, url: str, branch: str) -> dict | None:
    """The open pull request from ``branch`` in the repository at ``url``, `{"number", "url"}`,
    or None."""
    found = json.loads(
        _gh(
            folder,
            url,
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number,url",
            "--limit",
            "1",
        )
        or "[]"
    )
    return found[0] if found else None


def create(folder: Path, url: str, branch: str, title: str, body: str) -> dict:
    """Open a pull request from ``branch`` into `main`: `{"number", "url"}`."""
    created = _gh(
        folder,
        url,
        "pr",
        "create",
        "--base",
        "main",
        "--head",
        branch,
        "--title",
        title,
        "--body-file",
        "-",
        body=body,
    ).strip()
    number = created.rsplit("/", 1)[-1]
    if not number.isdigit():
        raise failed(f"gh pr create named no pull request: {created or 'nothing'}.")
    return {"number": int(number), "url": created}


def describe(folder: Path, url: str, number: int, title: str, body: str) -> None:
    _gh(folder, url, "pr", "edit", str(number), "--title", title, "--body-file", "-", body=body)


def state(folder: Path, url: str) -> str:
    """A pull request's state, by its URL: open, merged or closed."""
    where = gitops.pull_request_repository(url)
    if where is None:
        raise failed(f"{url} isn't a pull request's URL dp-instance reads.")
    found = json.loads(_gh(folder, where, "pr", "view", url, "--json", "state") or "{}").get("state")
    if found not in STATES:
        raise failed(f"gh pr view {url} named no state dp-instance reads.")
    return STATES[found]
