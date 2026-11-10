"""Before a prepared upgrade's code part, or `preflight --to`'s preflights: each repository's
target source, the development keys a candidate reads it with, and the release notes.

A published release's sources come from each template's Release Repository. A candidate's come
from there too where a published Release Manifest names the commit, a Template Release the
candidate keeps, and from the template's Development Repository otherwise, through its
development key."""

from __future__ import annotations

import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rich.text import Text

from .. import gitops, handoffs, places, template_access
from ..instance import SourceRecord
from ..manifest import version_key
from ..outcome import Next, Stop, failed
from ..screen import Block
from .base import Base


class Sources(Base):
    def source(self, repository: str) -> Path:
        """The owner's source at the target, fetched with the instance's template access. A
        prepared upgrade runs its CLI against the repository's copy for the owner's `upgrade`, so
        a fix to it works in the upgrade that ships it, and `preflight --to` for each preflight.
        Only the target's source is kept: an earlier target's goes, environment and all."""
        return self.fetch(repository)[0]

    def source_folder(self, repository: str) -> Path:
        """Where the target's source is fetched: a prepare's `.dp-instance/src/`."""
        return self.instance.own(places.SOURCE, repository)

    def from_development(self, repository: str) -> bool:
        """Whether the target's source comes from the template's Development Repository: a
        candidate's commit that no published Release Manifest names."""
        return self.channel != "stable" and not self.session.releases.released(
            repository, self.manifest.sources[repository]
        )

    def fetch(self, repository: str) -> tuple[Path, bool]:
        """The owner's source at the target, and whether this fetched it. A fetch that fails
        leaves no folder behind, so the next one starts clean."""
        commit = self.manifest.sources[repository]
        folder = self.source_folder(repository)
        with self._fetching[repository]:
            if gitops.is_checkout_root(folder) and gitops.commit_of(folder) == commit:
                return folder, False
            try:
                gitops.remove(folder)
                record = SourceRecord.read(self.checkout(repository))
                url = record.development_url if self.from_development(repository) else record.release_url
                gitops.fetch_commit(folder, url, commit)
            except BaseException as error:
                try:
                    gitops.remove(folder)
                except OSError:
                    pass  # the next fetch removes it before fetching again
                if isinstance(error, Stop):
                    raise Stop(
                        error.status,
                        f"{repository}'s target source couldn't be fetched: {error.message}",
                        next=error.next,
                        **error.detail,
                    ) from None
                if isinstance(error, OSError):
                    raise failed(f"{repository}'s target source couldn't be fetched: {error}") from None
                raise
        return folder, True

    def fetch_sources(self) -> None:
        """Fetch every repository's target source at once, with Git alone, before an upgrade
        runs its owners' `upgrade` phases, or `preflight --to` its preflights, from them; every
        fetch that failed stops the run, which `resume` fetches again."""
        self.check_development_keys()
        block = Block("sources", "", "", "", self.footer)
        block.message = f"Fetching the target source of {len(self.repositories)} repositories at once"
        started = time.monotonic()

        def attempt(repository: str) -> tuple[bool, Stop | None]:
            try:
                return self.fetch(repository)[1], None
            except Stop as stop:
                return False, stop

        keys = [repository.key for repository in self.repositories]
        with ThreadPoolExecutor(max_workers=max(len(keys), 1)) as pool:
            results = self.screen.run_live(block, lambda: list(pool.map(attempt, keys)))
        failures = [stop for _, stop in results if stop is not None]
        if failures:
            said = [stop.message if stop.message.endswith(".") else f"{stop.message}." for stop in failures]
            raise Stop(
                "failed",
                " ".join(said) + " Nothing has changed.",
                reason="transient"
                if any(stop.detail.get("reason") == "transient" for stop in failures)
                else "fetch",
                next=(
                    Next("dp-instance resume", "fetches them again and carries on, once they can be fetched"),
                ),
            )
        fetched = sum(new for new, _ in results)
        if fetched:
            self.timed("fetched", f"{fetched} target source{'s' if fetched != 1 else ''}", started)

    def check_development_keys(self) -> None:
        """A candidate reads each development repository only through the instance's key for
        it: before anything is fetched, the run stops for each template whose source comes from
        its development repository and whose development key isn't on this workstation."""
        missing = [
            repository.key
            for repository in self.repositories
            if self.from_development(repository.key)
            and not template_access.Access(
                self.instance.key, repository.key + gitops.DEVELOPMENT_SUFFIX
            ).private.is_file()
        ]
        if not missing:
            return
        raise Stop(
            "failed",
            "A candidate reads each development repository only through its key, and this workstation "
            f"has none for {', '.join(missing)}; nothing has changed.",
            reason="development-keys",
            missing=missing,
            next=(
                *(
                    Next(
                        f"dp-instance template-access setup {key} --development",
                        f"sets up the key for {key}'s development repository",
                    )
                    for key in missing
                ),
                Next("dp-instance resume", "fetches the target sources and carries on"),
            ),
        )

    def copy_notes(self) -> None:
        """Copy the release notes of each template whose commit the upgrade changes into the
        run's `notes/<template>/`: `docs/releases/<Platform Version>.md` at its target commit,
        for every published Platform Version after the instance's, up to the target, in version
        order; the target's alone when the instance's isn't known."""
        target = self.manifest.platform_version
        start = self.record.data.get("start")
        versions = [
            version
            for version in self.session.releases.published()
            if start is not None and version_key(start) < version_key(version) < version_key(target)
        ] + [target]
        changed = self.record.data.get("upgrading") or []
        copied = []
        for version in versions:
            for repository in changed:
                source = self.source(repository) / "docs" / "releases" / f"{version}.md"
                if source.is_file():
                    destination = self.record.file("notes", repository, f"{version}.md")
                    shutil.copyfile(source, destination)
                    copied.append(self.relative(destination))
        self.record.data["notes"] = copied
        self.record.save()

    def notes_lines(self) -> list[Text]:
        """The release notes the run copied, a line each in version order, by its path in the run,
        `notes/<template>/<version>.md`, which opens the file where the terminal can."""
        folder = self.relative(self.record.folder)
        return [
            handoffs.page_link(item.removeprefix(folder + "/"), self.instance.root / item)
            for item in self.record.data.get("notes") or []
        ]

    def notes_detail(self) -> dict:
        """The release notes the run copied, for --json: each file's path in the instance folder."""
        return {"notes": self.record.data.get("notes") or []} if self.record.data.get("target") else {}

    def show_notes(self) -> None:
        """Name the release notes as the code part starts."""
        lines = self.notes_lines()
        if not lines or self._notes_shown:
            return
        self._notes_shown = True
        for line in lines:
            self.screen.labelled("Release Notes:", line)
        self.screen.emit({"event": "notes", "run": self.record.id, **self.notes_detail()})
