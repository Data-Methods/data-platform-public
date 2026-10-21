"""Template access: one dedicated read-only SSH key per Platform Instance and template.

`setup` restores the Template Read Private Key from Key Vault or creates a new keypair with
`ssh-keygen`; checks the local key against the vault and stores it there if it's missing;
restricts the key file to its owner on Windows and POSIX; writes the dedicated SSH host
alias, `dm-<instance>-<template>`, and an `Include` at the top of the user's SSH
configuration, checking the alias uses only that key; and prints the non-secret request for
Data Methods. `check` confirms the key, the vault match, the SSH configuration and the
template's reach. Canary alone also keeps a key for each development repository, under
`<template>-dev`, whose alias the engine derives the same way.

A new keypair is only for an instance's first access to a template: `setup` refuses to create
one for a template a mapped repository already reads through its alias, and names the
Platform Infrastructure Key Vault to restore the existing key from.

It never prints a private key, reads or writes only the named Key Vault secret and never
lists the vault; the private key passes to and from the Azure CLI in a file.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text

from . import gitops, processes
from .instance import INFRASTRUCTURE, Instance, SourceRecord
from .outcome import Next, Result, Stop, failed
from .screen import Screen

KEY = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
VAULT = re.compile(r"^[a-zA-Z0-9-]{3,24}$")
LEGACY_SECRET = "dm-template-read-key"
FOUNDATION = Path("config/infrastructure/platform_foundation.json")
DEVELOPMENT = gitops.DEVELOPMENT_SUFFIX
PRIVATE_KEY_HEADER = "-----BEGIN OPENSSH PRIVATE KEY-----"


def _key(value: str, label: str) -> str:
    if not KEY.fullmatch(value) or len(value) > 63:
        raise failed(f"{value!r} isn't a valid {label}: lowercase letters, digits and single hyphens.")
    return value


@dataclass(frozen=True)
class Access:
    """One instance's access to one template repository: its key files, alias and secret."""

    instance: str
    template: str

    @property
    def ssh_root(self) -> Path:
        return Path.home() / ".ssh" / "data-platform"

    @property
    def user_config(self) -> Path:
        return Path.home() / ".ssh" / "config"

    @property
    def managed_config(self) -> Path:
        return self.ssh_root / "template-access.conf"

    @property
    def directory(self) -> Path:
        return self.ssh_root / self.instance / self.template

    @property
    def private(self) -> Path:
        return self.directory / "template-read"

    @property
    def public(self) -> Path:
        return self.directory / "template-read.pub"

    @property
    def secret(self) -> str:
        return f"dm-template-read-{self.template}"

    @property
    def alias(self) -> str:
        return f"dm-{self.instance}-{self.template}"

    @property
    def legacy_private(self) -> Path:
        return self.ssh_root / self.instance / "template-read"

    @property
    def comment(self) -> str:
        return f"{self.instance} {self.template} template-read"


# Files and permissions --------------------------------------------------------------------


def _identity() -> str:
    domain, user = os.environ.get("USERDOMAIN"), os.environ.get("USERNAME")
    return f"{domain}\\{user}" if domain and user else (user or "")


def restrict_directory(path: Path) -> None:
    if sys.platform == "win32":
        processes.run("icacls", str(path), "/inheritance:r", "/grant:r", f"{_identity()}:(OI)(CI)(F)")
        return
    path.chmod(stat.S_IRWXU)


def restrict_file(path: Path, *, private: bool) -> None:
    if sys.platform == "win32":
        processes.run("icacls", str(path), "/inheritance:r", "/grant:r", f"{_identity()}:(F)")
        return
    path.chmod(0o600 if private else 0o644)


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    restrict_directory(path)


def write_atomically(path: Path, value: str, *, private: bool) -> None:
    ensure_directory(path.parent)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            stream.write(value)
        restrict_file(temporary, private=private)
        os.replace(temporary, path)
        restrict_file(path, private=private)
    finally:
        temporary.unlink(missing_ok=True)


# Keys -------------------------------------------------------------------------------------


def public_identity(value: str, label: str) -> str:
    fields = value.split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise failed(f"{label} isn't a valid Ed25519 public key.")
    return f"{fields[0]} {fields[1]}"


def derived_public(private: Path) -> str:
    found = processes.run("ssh-keygen", "-y", "-P", "", "-f", str(private), check=False)
    if not found.ok:
        raise failed(f"ssh-keygen can't read the private key {private}: {found.first_error_line()}")
    return public_identity(found.stdout, str(private))


def same_key(first: Path, second: Path) -> bool:
    return derived_public(first) == derived_public(second)


def ensure_public(access: Access) -> None:
    derived = derived_public(access.private)
    if access.public.is_file():
        recorded = public_identity(access.public.read_text(encoding="utf-8"), str(access.public))
        if derived != recorded:
            raise failed(
                f"The Template Read Keypair is inconsistent: {access.private} doesn't match {access.public}."
            )
        restrict_file(access.public, private=False)
        return
    write_atomically(access.public, f"{derived} {access.comment}\n", private=False)


def new_keypair(access: Access) -> None:
    ensure_directory(access.directory)
    processes.run(
        "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", access.comment, "-f", str(access.private)
    )
    restrict_file(access.private, private=True)
    restrict_file(access.public, private=False)


def private_key_text(value: str) -> str:
    """ssh-keygen refuses an OpenSSH private key without its final newline."""
    text = value.replace("\r\n", "\n")
    return text if text.endswith("\n") else text + "\n"


def fingerprint(access: Access) -> str:
    fields = processes.run("ssh-keygen", "-l", "-E", "sha256", "-f", str(access.public)).stdout.split()
    if len(fields) < 2 or not fields[1].startswith("SHA256:"):
        raise failed(f"Couldn't derive the public key's fingerprint from {access.public}.")
    return fields[1]


# Key Vault --------------------------------------------------------------------------------


def vault_has(vault: str, name: str) -> bool:
    """Whether the vault holds the named secret, reading only its ID."""
    found = processes.run(
        "az",
        "keyvault",
        "secret",
        "show",
        "--vault-name",
        vault,
        "--name",
        name,
        "--query",
        "id",
        "--output",
        "tsv",
        "--only-show-errors",
        check=False,
    )
    if found.ok:
        return True
    if re.search(r"SecretNotFound|not found", found.stdout + found.stderr, re.IGNORECASE):
        return False
    raise failed(f"Couldn't tell whether Key Vault secret {vault}/{name} exists: {found.first_error_line()}")


def download(vault: str, name: str, destination: Path) -> None:
    write_atomically(destination, "", private=True)
    try:
        processes.run(
            "az",
            "keyvault",
            "secret",
            "download",
            "--vault-name",
            vault,
            "--name",
            name,
            "--file",
            str(destination),
            "--encoding",
            "utf-8",
            "--overwrite",
            "--output",
            "none",
            "--only-show-errors",
        )
    finally:
        if destination.is_file():
            restrict_file(destination, private=True)


def convert_legacy(source: Path, destination: Path) -> None:
    value = source.read_text(encoding="utf-8")
    if value.lstrip().startswith(PRIVATE_KEY_HEADER):
        write_atomically(destination, private_key_text(value), private=True)
        return
    try:
        payload = json.loads(value)
    except ValueError:
        raise failed(
            f"The legacy Key Vault secret {LEGACY_SECRET} is neither an OpenSSH private key nor JSON."
        ) from None
    key = payload.get("private_key") if isinstance(payload, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise failed(f"The legacy Key Vault secret {LEGACY_SECRET} holds no private_key.")
    write_atomically(destination, private_key_text(key), private=True)


def _scratch(access: Access, suffix: str) -> Path:
    return access.directory / f".template-read.{uuid.uuid4().hex}.{suffix}"


def restore_from_vault(access: Access, vault: str, name: str, *, legacy: bool) -> None:
    ensure_directory(access.directory)
    fetched, candidate = _scratch(access, "download"), _scratch(access, "candidate")
    try:
        download(vault, name, fetched)
        if legacy:
            convert_legacy(fetched, candidate)
        else:
            os.replace(fetched, candidate)
            restrict_file(candidate, private=True)
        derived_public(candidate)
        os.replace(candidate, access.private)
        restrict_file(access.private, private=True)
        ensure_public(access)
    finally:
        fetched.unlink(missing_ok=True)
        candidate.unlink(missing_ok=True)


def vault_matches(access: Access, vault: str, name: str, local: Path, *, legacy: bool = False) -> bool:
    fetched, candidate = _scratch(access, "download"), _scratch(access, "candidate")
    try:
        download(vault, name, fetched)
        if legacy:
            convert_legacy(fetched, candidate)
        else:
            os.replace(fetched, candidate)
        return same_key(local, candidate)
    finally:
        fetched.unlink(missing_ok=True)
        candidate.unlink(missing_ok=True)


def save_to_vault(access: Access, vault: str) -> None:
    processes.run(
        "az",
        "keyvault",
        "secret",
        "set",
        "--vault-name",
        vault,
        "--name",
        access.secret,
        "--file",
        str(access.private),
        "--encoding",
        "utf-8",
        "--content-type",
        "application/x-openssh-private-key",
        "--tags",
        f"platform-instance-key={access.instance}",
        f"template-repository-key={access.template}",
        "--output",
        "none",
        "--only-show-errors",
    )


# SSH configuration ------------------------------------------------------------------------


def _posix(path: str | Path) -> str:
    text = str(path).strip('"').replace("\\", "/")
    return text.casefold() if sys.platform == "win32" else text


def update_managed_config(access: Access) -> None:
    """One `Host` per key under the SSH root, each using only its key."""
    lines = ["# Managed by dp-instance template-access. Do not edit by hand."]
    if access.ssh_root.is_dir():
        for instance in sorted(
            path for path in access.ssh_root.iterdir() if path.is_dir() and KEY.fullmatch(path.name)
        ):
            for template in sorted(
                path for path in instance.iterdir() if path.is_dir() and KEY.fullmatch(path.name)
            ):
                key = template / "template-read"
                if key.is_file():
                    lines += [
                        "",
                        f"Host dm-{instance.name}-{template.name}",
                        "    HostName github.com",
                        "    User git",
                        f'    IdentityFile "{key.as_posix()}"',
                        "    IdentitiesOnly yes",
                    ]
    write_atomically(access.managed_config, "\n".join(lines) + "\n", private=True)


def effective_problem(access: Access, config: Path) -> str | None:
    """Why the alias's effective SSH settings don't use only its dedicated key, if they don't."""
    found = processes.run("ssh", "-G", "-F", str(config), access.alias, check=False)
    if not found.ok:
        return f"ssh -G {access.alias} failed: {found.first_error_line()}"
    values: dict[str, list[str]] = {}
    for line in found.stdout.splitlines():
        name, _, value = line.partition(" ")
        if value:
            values.setdefault(name.lower(), []).append(value.strip())
    identities = [_posix(item) for item in values.get("identityfile", [])]
    if (
        values.get("hostname", [""])[0] != "github.com"
        or values.get("user", [""])[0] != "git"
        or values.get("identitiesonly", [""])[0] != "yes"
        or identities != [_posix(access.private.as_posix())]
    ):
        return f"the effective SSH configuration for {access.alias} doesn't use only its dedicated key"
    return None


def ensure_include(access: Access) -> None:
    """Put the `Include` first in the user's SSH configuration, keeping every other line."""
    config = access.user_config
    ensure_directory(config.parent)
    if config.is_symlink():
        raise failed(
            f"The SSH configuration {config} is a symlink; add the Include by hand. It wasn't replaced."
        )
    current = config.read_text(encoding="utf-8") if config.is_file() else ""
    newline = "\r\n" if "\r\n" in current else "\n"
    expected = _posix(access.managed_config.as_posix())
    kept = []
    for line in re.split(r"\r?\n", current):
        content = line.split("#", 1)[0].strip()
        match = re.fullmatch(r"(?i)include\s+(.+)", content)
        if match and _posix(match.group(1).strip()) == expected:
            continue
        kept.append(line)
    while kept and kept[-1] == "":
        kept.pop()
    updated = f'Include "{access.managed_config.as_posix()}"' + newline
    if kept:
        updated += newline + newline.join(kept) + newline
    if current == updated:
        problem = effective_problem(access, config)
        if problem:
            raise failed(problem + ". Review the conflicting Host rules, then run setup again.")
        return
    candidate = config.parent / f".config.{uuid.uuid4().hex}.candidate"
    try:
        write_atomically(candidate, updated, private=True)
        problem = effective_problem(access, candidate)
        if problem:
            raise failed(problem + ". Review the conflicting Host rules, then run setup again.")
        os.replace(candidate, config)
        restrict_file(config, private=True)
    finally:
        candidate.unlink(missing_ok=True)


# Commands ---------------------------------------------------------------------------------


def _configured_vault(start: Path) -> str | None:
    """The Platform Infrastructure Key Vault the mapped Client Infrastructure Repository's
    configuration names, when it's there to read."""
    try:
        infrastructure = Instance.find(start).infrastructure
        if infrastructure is None:
            return None
        document = json.loads((infrastructure.path / FOUNDATION).read_text(encoding="utf-8"))
        name = document["infrastructure_key_vault"]["name"]
    except (Stop, OSError, ValueError, KeyError, TypeError):
        return None
    return name if isinstance(name, str) and VAULT.fullmatch(name) else None


def _refuse_a_second_key(start: Path, access: Access, template: str, development: bool) -> None:
    """A new keypair is only for an instance's first access to a template: one a mapped
    repository already reads through its alias has a key, in the Platform Infrastructure Key
    Vault, and a new one would need registering again."""
    if not any(url.startswith(f"git@{access.alias}:") for url in _clone_urls(start, access.template)):
        return
    vault = _configured_vault(start) or "<Platform Infrastructure Key Vault>"
    command = f"dp-instance template-access setup {template}" + (" --development" if development else "")
    raise failed(
        f"This instance already reads {access.template} through {access.alias}, so its key exists, in the "
        "Platform Infrastructure Key Vault; a new one would need registering again. Nothing was created.",
        Next(f"{command} --key-vault {vault}", "restores the instance's key and its SSH host alias"),
    )


def _instance_key(given: str | None, start: Path) -> str:
    if given:
        return _key(given, "Platform Instance Key")
    try:
        return Instance.find(start).key
    except Stop:
        raise failed(
            "Name the Platform Instance with --instance, or run this in a mapped instance folder.",
        ) from None


def setup(
    screen: Screen,
    start: Path,
    template: str,
    *,
    instance: str | None,
    key_vault: str | None,
    development: bool,
    migrate_legacy: bool,
) -> Result:
    template = _key(template, "Template Repository Key")
    if key_vault is not None and not VAULT.fullmatch(key_vault):
        raise failed(f"{key_vault!r} isn't a Key Vault name.")
    if migrate_legacy and (template != INFRASTRUCTURE or development):
        raise failed(
            f"--migrate-legacy-infrastructure-key applies only to {INFRASTRUCTURE}'s release repository key."
        )
    access = Access(_instance_key(instance, start), template + (DEVELOPMENT if development else ""))
    for tool in (
        "ssh-keygen",
        "ssh",
        *(("icacls",) if sys.platform == "win32" else ()),
        *(("az",) if key_vault else ()),
    ):
        processes.executable(tool)
    legacy_local = template == INFRASTRUCTURE and not development and access.legacy_private.is_file()
    if not access.private.is_file() and not key_vault and not migrate_legacy and not legacy_local:
        _refuse_a_second_key(start, access, template, development)
    lines: list[str] = []
    ensure_directory(access.ssh_root)
    ensure_directory(access.directory)

    local = access.private.is_file()
    vault_current = vault_legacy = False
    if key_vault:
        vault_current = vault_has(key_vault, access.secret)
        if template == INFRASTRUCTURE and not development:
            vault_legacy = vault_has(key_vault, LEGACY_SECRET)
    if migrate_legacy and not legacy_local and not vault_legacy:
        raise failed("No legacy Infrastructure key was found locally or in the selected Key Vault.")
    if not migrate_legacy and not local and not vault_current and (legacy_local or vault_legacy):
        raise failed(
            "A legacy Infrastructure key exists. Run setup again with --migrate-legacy-infrastructure-key "
            "to adopt it without rotating it."
        )
    mismatch = "The Template Read Private Keys don't match; nothing was rotated."
    if (
        migrate_legacy
        and legacy_local
        and vault_legacy
        and not vault_matches(access, key_vault, LEGACY_SECRET, access.legacy_private, legacy=True)
    ):
        raise failed(mismatch)
    if (
        migrate_legacy
        and legacy_local
        and vault_current
        and not vault_matches(access, key_vault, access.secret, access.legacy_private)
    ):
        raise failed(mismatch)
    if migrate_legacy and local and legacy_local and not same_key(access.private, access.legacy_private):
        raise failed(mismatch)

    if not local and vault_current:
        restore_from_vault(access, key_vault, access.secret, legacy=False)
        lines.append(f"Restored the Template Read Private Key from Key Vault secret {access.secret}.")
    elif not local and migrate_legacy and legacy_local:
        ensure_directory(access.directory)
        write_atomically(access.private, access.legacy_private.read_text(encoding="utf-8"), private=True)
        ensure_public(access)
        lines.append(
            "Adopted the existing Infrastructure key without rotation; the legacy file remains in place."
        )
    elif not local and migrate_legacy and vault_legacy:
        restore_from_vault(access, key_vault, LEGACY_SECRET, legacy=True)
        lines.append(f"Restored the infrastructure key from the legacy Key Vault secret {LEGACY_SECRET}.")
    elif not local:
        new_keypair(access)
        lines.append("Created a new Template Read Keypair.")

    restrict_file(access.private, private=True)
    ensure_public(access)
    if (
        migrate_legacy
        and vault_legacy
        and not vault_matches(access, key_vault, LEGACY_SECRET, access.private, legacy=True)
    ):
        raise failed(mismatch)
    if key_vault:
        if vault_current:
            if not vault_matches(access, key_vault, access.secret, access.private):
                raise failed(
                    f"The local Template Read Private Key and Key Vault secret {access.secret} don't match. "
                    "Neither copy was changed; recover or rotate the key deliberately."
                )
            lines.append(f"The local key matches Key Vault secret {access.secret}.")
        else:
            save_to_vault(access, key_vault)
            lines.append(f"Stored the Template Read Private Key in Key Vault secret {access.secret}.")

    update_managed_config(access)
    ensure_include(access)
    public = access.public.read_text(encoding="utf-8").strip()
    print_fingerprint = fingerprint(access)

    screen.header("template-access setup", ("instance", access.instance), ("template", access.template))
    screen.blank()
    for line in lines:
        screen.note(line)
    screen.blank()
    screen.labelled("Private Key:", str(access.private))
    screen.labelled(
        "Key Vault Secret:",
        f"{key_vault}/{access.secret}"
        if key_vault
        else "not stored yet; run setup again with --key-vault once the vault exists",
    )
    screen.labelled("SSH Host Alias:", access.alias)
    screen.blank()
    screen.print(Text("Send this non-secret request to Data Methods:", style="bold"))
    screen.labelled("Platform Instance Key:", access.instance)
    screen.labelled(
        "Template Repository Key:", template + (" (development repository)" if development else "")
    )
    screen.labelled("Template Read Public Key:", public)
    screen.labelled("Fingerprint:", print_fingerprint)
    screen.note("Data Methods registers the key on that repository and returns its SSH clone URL.")
    return Result(
        "template-access setup",
        "done",
        "Template access is set up locally.",
        detail={
            "instance": access.instance,
            "template": template,
            "development": development,
            "alias": access.alias,
            "private_key_path": str(access.private),
            "public_key": public,
            "fingerprint": print_fingerprint,
            "key_vault_secret": f"{key_vault}/{access.secret}" if key_vault else None,
        },
    )


def _clone_urls(start: Path, template: str) -> list[str]:
    """The clone URLs a mapped repository records for this template's access."""
    try:
        instance = Instance.find(start)
    except Stop:
        return []
    base = template.removesuffix(DEVELOPMENT)
    mapped = instance.components.get(base)
    if mapped is None:
        return []
    record = SourceRecord.read(mapped.path)
    if record is None:
        return []
    return [record.development_url if template.endswith(DEVELOPMENT) else record.release_url]


def check(
    screen: Screen,
    start: Path,
    template: str | None,
    *,
    instance: str | None,
    key_vault: str | None,
    development: bool,
) -> Result:
    key = _instance_key(instance, start)
    root = Access(key, "x").ssh_root / key
    if template:
        templates = [_key(template, "Template Repository Key") + (DEVELOPMENT if development else "")]
    else:
        templates = (
            sorted(path.name for path in root.iterdir() if path.is_dir() and KEY.fullmatch(path.name))
            if root.is_dir()
            else []
        )
    if not templates:
        raise failed(
            f"No template access is set up for {key} on this workstation.",
            Next("dp-instance template-access setup <template>", "sets it up"),
        )
    screen.header("template-access check", ("instance", key))
    report = []
    for name in templates:
        access = Access(key, name)
        problems: list[str] = []
        checks: list[tuple[str, str]] = []
        if not access.private.is_file():
            problems.append(f"no private key at {access.private}")
        else:
            try:
                derived = derived_public(access.private)
                if (
                    not access.public.is_file()
                    or public_identity(access.public.read_text(encoding="utf-8"), str(access.public))
                    != derived
                ):
                    problems.append(f"{access.public} doesn't match the private key")
                else:
                    checks.append(("Key:", f"{access.private} and its public key match"))
            except Stop as stop:
                problems.append(stop.message)
            if sys.platform != "win32" and access.private.stat().st_mode & 0o077:
                problems.append(f"{access.private} can be read by other users")
            if key_vault and not problems:
                try:
                    if not vault_has(key_vault, access.secret):
                        problems.append(f"Key Vault {key_vault} has no secret {access.secret}")
                    elif not vault_matches(access, key_vault, access.secret, access.private):
                        problems.append(f"the local key and Key Vault secret {access.secret} don't match")
                    else:
                        checks.append(("Vault:", f"{key_vault}/{access.secret} matches"))
                except Stop as stop:
                    problems.append(stop.message)
        include = access.user_config.is_file() and any(
            _posix(match.group(1).strip()) == _posix(access.managed_config.as_posix())
            for match in re.finditer(
                r"(?im)^\s*include\s+(.+?)\s*$", access.user_config.read_text(encoding="utf-8")
            )
        )
        problem = (
            effective_problem(access, access.user_config)
            if include
            else "the SSH configuration doesn't include the managed host aliases"
        )
        if problem:
            problems.append(problem)
        else:
            checks.append(("SSH:", f"{access.alias} uses only its dedicated key"))
        urls = [url for url in _clone_urls(start, name) if url.startswith(f"git@{access.alias}:")]
        if not urls:
            checks.append(("Reach:", "not checked: no mapped repository clones through this alias"))
        for url in urls:
            reached = gitops.git("ls-remote", url, "HEAD", check=False)
            if reached.ok:
                checks.append(("Reach:", f"{url} answers"))
            else:
                problems.append(f"{url} doesn't answer: {reached.first_error_line()}")
        screen.blank()
        screen.labelled("Template:", Text(name, style="bold"))
        for label, text in checks:
            screen.labelled(label, text)
        for text in problems:
            screen.labelled("Problem:", text, "bold red")
        report.append(
            {
                "template": name,
                "alias": access.alias,
                "problems": problems,
                "checks": [text for _, text in checks],
            }
        )
    bad = [item for item in report if item["problems"]]
    if bad:
        steps = (
            Next(
                "dp-instance template-access setup <template>",
                "repairs the key, the vault copy and the SSH configuration",
            ),
        )
        screen.next_block(steps)
        return Result(
            "template-access check",
            "failed",
            f"Template access has problems for {len(bad)} templates.",
            steps,
            {"templates": report},
        )
    screen.blank()
    screen.note("Template access is in order.", "bold green")
    return Result(
        "template-access check", "done", "Template access is in order.", detail={"templates": report}
    )
