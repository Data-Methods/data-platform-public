"""Before a prepared upgrade's code part: each repository's target source, the development keys
a candidate reads it with, and the release notes."""

from __future__ import annotations

import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import gitops, template_access
from ..instance import SourceRecord
from ..manifest import version_key
from ..outcome import Next, Stop, failed
from ..screen import Block
from .base import Base


class Sources(Base):
    def source(self, repository: str) -> Path:
        """The owner's source at the target, fetched with the instance's template access into
        `.dp-instance/src/`. A prepared upgrade runs its CLI against the repository's copy for
        the preflights and the owner's `upgrade`, so a fix to either works in the upgrade that
        ships it. Only the target's source is kept: an earlier target's goes, environment and
        all."""
        return self.fetch(repository)[0]

    def fetch(self, repository: str) -> tuple[Path, bool]:
        """The owner's source at the target, and whether this fetched it. A fetch that fails
        leaves no folder behind, so the next one starts clean."""
        commit = self.manifest.sources[repository]
        folder = self.instance.own("src", repository)
        with self._fetching[repository]:
            if gitops.is_checkout_root(folder) and gitops.commit_of(folder) == commit:
                return folder, False
            try:
                gitops.remove(folder)
                record = SourceRecord.read(self.checkout(repository))
                url = record.release_url if self.channel == "stable" else record.development_url
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
        runs its preflights and owners' `upgrade` phases from them; every fetch that failed stops
        the run, which `resume` fetches again."""
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
        it: before anything is fetched, the run stops for each template whose development key
        isn't on this workstation."""
        if self.channel != "canary":
            return
        missing = [
            repository.key
            for repository in self.repositories
            if not template_access.Access(
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

    def notes_line(self) -> str | None:
        """The release notes the run copied, on one line: each file, in version order, and their
        folder."""
        notes = self.record.data.get("notes") or []
        if not notes:
            return None
        folder = self.relative(self.record.folder / "notes")
        files = ", ".join(item.removeprefix(folder + "/") for item in notes)
        return f"{files} · notes/"

    def notes_detail(self) -> dict:
        """The release notes the run copied, for --json: each file's path in the instance folder."""
        return {"notes": self.record.data.get("notes") or []} if self.record.data.get("target") else {}

    def show_notes(self) -> None:
        """Name the release notes as the code part starts."""
        line = self.notes_line()
        if line is None or self._notes_shown:
            return
        self._notes_shown = True
        self.screen.labelled("Release Notes:", line)
        self.screen.emit({"event": "notes", "run": self.record.id, **self.notes_detail()})
