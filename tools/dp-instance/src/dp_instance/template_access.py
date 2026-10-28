"""Template access: one dedicated read-only SSH key per Platform Instance and template.

`setup` restores the Template Read Private Key from Key Vault or creates a new keypair with
`ssh-keygen`; checks the local key against the vault and stores it there if it's missing;
restricts the key file to its owner on Windows and POSIX; writes the dedicated SSH host
alias, `dm-<instance>-<template>`, and an `Include` at the top of the user's SSH
configuration, checking the alias uses only that key; and, for a key it has just created,
prints the non-secret request for Data Methods. The vault is the Platform Infrastructure Key
Vault the Client Infrastructure Repository's configuration names: the mapped one's or, before
the instance is mapped, as at a first deployment, that of the checkout the command runs in,
whose record and instance key it reads as a map's, refusing an `--instance` that differs.
Before the configuration names a vault, or before Azure has it, the key stays on the
workstation alone. With no template named, `setup`
restores and sets up every template in `dp-instance.json` whose key the vault holds, and names
any the instance has no access to yet. `check` confirms the key, the vault match, the SSH
configuration and, with `git ls-remote`, every mapped template's reach through its alias.
Canary alone also keeps a key for each development repository, under `<template>-dev`, whose
alias the engine derives the same way: `setup --development` sets up one, and `setup` with no
template restores each the vault holds.

For a template whose source record names plain or HTTPS github.com, the rule sending exactly
that URL through the alias, Git's `url.<alias URL>.insteadOf`, reaches only the Git commands
and component CLIs dp-instance starts, through Git's environment configuration: a checkout of
the template's own repository, such as its development repository's on a workstation that
also develops it, keeps its own access. `setup` removes the global rules earlier versions
added.

The SSH configuration names its files in `~/.ssh`, the `Include` relative to it and each key
under `~`, which every OpenSSH resolves in its own home alike, Git for Windows' bundled one
included, where a Windows absolute path reads as relative. On Windows the absolute `Include`
of the same file follows, for Windows OpenSSH whatever `HOME` Git gives it. `setup` and
`check` prove reach through Git itself, not only `ssh -G`; when Git still can't use an alias,
such as with a `HOME` outside the user profile, they name the `core.sshCommand` that has Git
run the OpenSSH they checked, and never set it themselves.

A new keypair is only for an instance's first access to a template: `setup` refuses to create
one for a template a mapped repository already reads through its alias, and says to restore
the existing key from the Platform Infrastructure Key Vault.

It never prints a private key, reads or writes only the named Key Vault secrets and never
lists the vault; the private key passes to and from the Azure CLI in a file, one download
telling both whether the vault holds it and what it holds. Each template is one part of the
command, shown with its time.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text

from . import gitops, processes
from .instance import INFRASTRUCTURE, Instance, SourceRecord
from .outcome import Next, Result, Stop, failed
from .screen import Screen

KEY = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
VAULT = re.compile(r"^[a-zA-Z0-9-]{3,24}$")
FOUNDATION = Path("config/infrastructure/platform_foundation.json")
SETUP = Path("config/infrastructure/component_setup.json")
DEVELOPMENT = gitops.DEVELOPMENT_SUFFIX
# A source record naming GitHub itself rather than the instance's alias, and its path.
GITHUB_URL = re.compile(r"(?:git@github\.com:|https://github\.com/)(.+)")
# A global Git rule earlier setups wrote, as Git lists it: through an alias, `dm-<instance>-
# <template>`, and the path its GitHub URL names.
EARLIER_RULE = re.compile(r"url\.git@(dm-[a-z0-9]+(?:-[a-z0-9]+)*):(.+)\.insteadof")
UNMAPPED = "not checked: no mapped repository uses this template"
# What Azure answers, reading a secret, for a vault name it may have no vault for: a name that
# doesn't resolve, named in the error with the vault's host, or a vault it reports not found. A
# private DNS zone without the vault's record, or no network, answers the same, so Azure Resource
# Manager confirms it.
NO_SUCH_HOST = re.compile(
    r"Name or service not known|No address associated with hostname|nodename nor servname provided"
    r"|No such host is known|\[Errno 11001\] getaddrinfo failed",
    re.IGNORECASE,
)


def _key(value: str, label: str) -> str:
    if not KEY.fullmatch(value) or len(value) > 63:
        raise failed(f"{value!r} isn't a valid {label}: lowercase letters, digits and single hyphens.")
    return value


def ssh_root() -> Path:
    """Every instance's template access keys on this workstation, in `<instance>/<template>/`."""
    return Path.home() / ".ssh" / "data-platform"


def key_folders() -> list[tuple[str, str]]:
    """Each instance and template with a key folder on this workstation."""
    root = ssh_root()
    if not root.is_dir():
        return []
    return [
        (instance.name, template.name)
        for instance in sorted(root.iterdir())
        if instance.is_dir() and KEY.fullmatch(instance.name)
        for template in sorted(instance.iterdir())
        if template.is_dir() and KEY.fullmatch(template.name)
    ]


@dataclass(frozen=True)
class Access:
    """One instance's access to one template repository: its key files, alias and secret."""

    instance: str
    template: str

    @property
    def ssh_root(self) -> Path:
        return ssh_root()

    @property
    def user_config(self) -> Path:
        return Path.home() / ".ssh" / "config"

    @property
    def managed_config(self) -> Path:
        return self.ssh_root / "template-access.conf"

    @property
    def include(self) -> str:
        """The managed configuration as the user's `Include` names it: relative to `~/.ssh`."""
        return self.managed_config.relative_to(self.user_config.parent).as_posix()

    @property
    def include_lines(self) -> list[str]:
        """The `Include` lines setup writes first in the user's SSH configuration: the relative
        one, which every OpenSSH resolves in its own home; on Windows also the absolute one,
        which Windows OpenSSH reads whatever `HOME` Git gives it and Git's bundled ssh, reading
        it as relative to `~/.ssh`, skips."""
        lines = [f"Include {self.include}"]
        if sys.platform == "win32":
            lines.append(f'Include "{self.managed_config.as_posix()}"')
        return lines

    @property
    def directory(self) -> Path:
        return self.ssh_root / self.instance / self.template

    @property
    def private(self) -> Path:
        return self.directory / "template-read"

    @property
    def identity(self) -> str:
        """The private key as the SSH configuration names it: under `~`."""
        return "~/" + self.private.relative_to(Path.home()).as_posix()

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


def fingerprint(access: Access) -> str:
    fields = processes.run("ssh-keygen", "-l", "-E", "sha256", "-f", str(access.public)).stdout.split()
    if len(fields) < 2 or not fields[1].startswith("SHA256:"):
        raise failed(f"Couldn't derive the public key's fingerprint from {access.public}.")
    return fields[1]


# Key Vault --------------------------------------------------------------------------------


def secret_state(vault: str, name: str, subscription: str | None = None) -> bool | None:
    """Whether the vault holds the named secret, reading only its ID; None when Azure has no
    such vault, as Azure Resource Manager confirms in ``subscription``. Any other failure, such
    as a missing right or a network error, stops."""
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
    text = found.stdout + found.stderr
    host = f"{vault.casefold()}.vault."
    if ("VaultNotFound" in text or (host in text.casefold() and NO_SUCH_HOST.search(text))) and _no_vault(
        vault, subscription
    ):
        return None
    if re.search(r"SecretNotFound|not found", text, re.IGNORECASE):
        return False
    raise failed(f"Couldn't tell whether Key Vault secret {vault}/{name} exists: {found.first_error_line()}")


def _no_vault(vault: str, subscription: str | None) -> bool:
    """Whether Azure Resource Manager has no vault of this name: ResourceNotFound. Any other
    answer, the vault included, leaves it to the secret read's own failure."""
    found = processes.run(
        "az",
        "keyvault",
        "show",
        "--name",
        vault,
        *(("--subscription", subscription) if subscription else ()),
        "--query",
        "id",
        "--output",
        "tsv",
        "--only-show-errors",
        check=False,
    )
    return (
        not found.ok
        and re.search(r"ResourceNotFound|not found within subscription", found.stdout + found.stderr)
        is not None
    )


def vault_has(vault: str, name: str) -> bool:
    """Whether the vault holds the named secret, reading only its ID."""
    held = secret_state(vault, name)
    if held is None:
        raise failed(f"Key Vault {vault} doesn't exist.")
    return held


def existing_vault(vault: str | None, name: str, subscription: str | None) -> str | None:
    """The vault the configuration names, unless Azure has no such vault yet, as before
    `platform_foundation` creates it: then template access works as with no vault. One secret's
    ID read tells, and Azure Resource Manager confirms a vault it can't reach."""
    return vault if vault and secret_state(vault, name, subscription) is not None else None


def download(vault: str, name: str, destination: Path) -> bool:
    """Download a secret's value into a file only its owner reads; False when the vault holds
    no such secret."""
    write_atomically(destination, "", private=True)
    try:
        found = processes.run(
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
            check=False,
        )
    finally:
        if destination.is_file():
            restrict_file(destination, private=True)
    if found.ok:
        return True
    if re.search(r"SecretNotFound|not found", found.stdout + found.stderr, re.IGNORECASE):
        return False
    raise failed(f"az keyvault secret failed: {found.first_error_line()}")


def _scratch(access: Access, suffix: str) -> Path:
    return access.directory / f".template-read.{uuid.uuid4().hex}.{suffix}"


def vault_copy(access: Access, vault: str) -> Path | None:
    """The vault's copy of the key, downloaded once into a scratch file beside it, which the
    caller removes; None when the vault holds none. A key folder made for the download goes
    again when nothing came, so it never reads as a template set up here."""
    made = not access.directory.is_dir()
    ensure_directory(access.directory)
    fetched = _scratch(access, "download")
    held = False
    try:
        held = download(vault, access.secret, fetched)
    finally:
        if not held:
            fetched.unlink(missing_ok=True)
            if made:
                try:
                    access.directory.rmdir()
                except OSError:
                    pass  # something else is in it now
    return fetched if held else None


def restore_from(access: Access, copy: Path) -> None:
    """Make the vault's copy, once it proves a private key, this workstation's key."""
    candidate = _scratch(access, "candidate")
    try:
        os.replace(copy, candidate)
        restrict_file(candidate, private=True)
        derived_public(candidate)
        os.replace(candidate, access.private)
        restrict_file(access.private, private=True)
        ensure_public(access)
    finally:
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
    for instance, template in key_folders():
        entry = Access(instance, template)
        if entry.private.is_file():
            lines += [
                "",
                f"Host {entry.alias}",
                "    HostName github.com",
                "    User git",
                f"    IdentityFile {entry.identity}",
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
        or identities != [_posix(access.identity)]
    ):
        return f"the effective SSH configuration for {access.alias} doesn't use only its dedicated key"
    return None


def ensure_include(access: Access) -> None:
    """Put the `Include` lines first in the user's SSH configuration, keeping every other line
    and replacing any earlier `Include` of the same file."""
    config = access.user_config
    ensure_directory(config.parent)
    if config.is_symlink():
        raise failed(
            f"The SSH configuration {config} is a symlink; add the Include by hand. It wasn't replaced."
        )
    current = config.read_text(encoding="utf-8") if config.is_file() else ""
    newline = "\r\n" if "\r\n" in current else "\n"
    replaced = {_posix(access.include), _posix(access.managed_config.as_posix())}
    kept = []
    for line in re.split(r"\r?\n", current):
        content = line.split("#", 1)[0].strip()
        match = re.fullmatch(r"(?i)include\s+(.+)", content)
        if match and _posix(match.group(1).strip()) in replaced:
            continue
        kept.append(line)
    while kept and kept[-1] == "":
        kept.pop()
    updated = newline.join(access.include_lines) + newline
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


# Git --------------------------------------------------------------------------------------


def alias_url(access: Access, url: str) -> str | None:
    """The same repository through the alias, for a record naming plain or HTTPS github.com."""
    match = GITHUB_URL.fullmatch(url)
    return f"git@{access.alias}:{match.group(1)}" if match else None


def rewrites(key: str, instance: Instance) -> list[tuple[str, str]]:
    """The rules for each mapped template, as `_rules` gives them."""
    return _rules(key, {template: mapped.path for template, mapped in instance.components.items()})


def _rules(key: str, checkouts: dict[str, Path]) -> list[tuple[str, str]]:
    """The rules fetching each template's record URL that names github.com itself through the
    instance's alias, its release repository's and its development repository's, for each whose
    key is on this workstation: Git's `url.<alias URL>.insteadOf`, as Git's environment
    configuration takes it."""
    found = []
    for template, checkout in checkouts.items():
        record = SourceRecord.read(checkout)
        if record is None:
            continue
        for name, url in ((template, record.release_url), (template + DEVELOPMENT, record.development_url)):
            access = Access(key, name)
            base = alias_url(access, url)
            if base is not None and access.private.is_file():
                found.append((f"url.{base}.insteadOf", url))
    return found


def _pass_rules(key: str, start: Path) -> None:
    """Pass the rules of the templates `_checkouts` finds, for the keys on this workstation now,
    to the Git commands that follow."""
    gitops.pass_configuration(_rules(key, _checkouts(start)))


def remove_global_rules() -> list[str]:
    """Remove every global Git rule of the shape earlier setups wrote, whichever instance's and
    whatever keys remain here, since one left there would outrank the instance's own: what was
    removed. No other rule is touched."""
    listed = gitops.git("config", "--global", "--get-regexp", r"^url\..*\.insteadof$", check=False)
    found = []
    for line in listed.stdout.splitlines():
        rule, _, url = line.partition(" ")
        match = EARLIER_RULE.fullmatch(rule)
        source = GITHUB_URL.fullmatch(url)
        if match and source and source.group(1) == match.group(2):
            found.append((rule, match.group(1), url))
    if found:
        gitops.require_configuration_environment()
    removed = []
    for rule, alias, url in dict.fromkeys(found):
        gitops.git("config", "--global", "--fixed-value", "--unset-all", rule, url)
        removed.append(f"Removed the global Git rule fetching {url} through {alias}.")
    return removed


def _reads_through_alias(start: Path, access: Access) -> bool:
    """Whether a mapped repository's source record reads the template through the instance's
    alias."""
    return any(url.startswith(f"git@{access.alias}:") for url in _clone_urls(start, access.template))


def _unheld(start: Path, access: Access, key_vault: str) -> tuple[str, Next]:
    """A template whose key the vault doesn't hold: what's wrong, and what sets it up. Only when
    neither this workstation nor a source record shows the instance's access is it a first
    access."""
    template = access.template.removesuffix(DEVELOPMENT)
    flag = " --development" if access.template.endswith(DEVELOPMENT) else ""
    command = f"dp-instance template-access setup {template}{flag}"
    if access.private.is_file():
        return (
            f"Key Vault {key_vault} has no secret {access.secret}",
            Next(command, f"stores this workstation's key for {access.template} in {key_vault}"),
        )
    if _reads_through_alias(start, access):
        return (
            f"this instance reads {access.template} through {access.alias}, but neither this "
            f"workstation nor Key Vault {key_vault} holds its key",
            Next(command, f"on the workstation that holds the key: stores it in {key_vault}"),
        )
    return (
        f"{access.template} has no template access for this instance yet: Key Vault {key_vault} holds "
        f"no {access.secret}. Its first access creates a key, stores it in {key_vault} and prints the "
        "request Data Methods registers",
        Next(command, f"creates this instance's first key for {access.template}"),
    )


def ssh_command() -> Next:
    """The command that has Git run the OpenSSH `setup` checks the aliases with; it's named,
    never run."""
    path = Path(processes.executable("ssh")).as_posix()
    value = f"'{path}'" if " " in path else path
    return Next(
        f'git config --global core.sshCommand "{value}"',
        "has Git run the OpenSSH that reads the template access aliases",
    )


def reach(access: Access, url: str) -> str:
    """That Git reaches the record's ``url`` through the alias, after its own rules and with
    the ssh it runs."""
    through = gitops.git("ls-remote", "--get-url", url).stdout.strip()
    if not through.startswith(f"git@{access.alias}:"):
        source = "directly" if through == url else f"from {through}"
        raise failed(f"Git fetches {url} {source}, not through {access.alias}")
    found = gitops.git("ls-remote", url, "HEAD", check=False)
    if found.ok:
        return f"{url} answers through {access.alias}"
    if re.search(f"could not resolve hostname {re.escape(access.alias)}", found.stderr, re.IGNORECASE):
        raise failed(
            f"Git can't use {access.alias}: the ssh Git runs doesn't read the SSH configuration "
            "that names it",
            ssh_command(),
        )
    raise failed(f"{url} doesn't answer: {found.first_error_line()}")


# Commands ---------------------------------------------------------------------------------


def _infrastructure(start: Path) -> Path | None:
    """The Client Infrastructure Repository whose configuration names the vault: the mapped one,
    or, with no map yet, such as at a first deployment before the repository has a remote to map,
    the checkout the command runs in, when it is one."""
    try:
        mapped = Instance.find(start).infrastructure
    except Stop:
        folder = next((item for item in (start, *start.parents) if (item / ".git").exists()), None)
        if folder is None:
            return None
        record = SourceRecord.read(folder)
        return folder if record is not None and record.template == INFRASTRUCTURE else None
    return mapped.path if mapped is not None else None


def _configured(start: Path, file: Path, *keys: str) -> str | None:
    """A setting the Client Infrastructure Repository's configuration holds, when it's there to
    read."""
    try:
        infrastructure = _infrastructure(start.resolve())
        if infrastructure is None:
            return None
        found = json.loads((infrastructure / file).read_text(encoding="utf-8"))
        for key in keys:
            found = found[key]
    except (Stop, OSError, ValueError, KeyError, TypeError):
        return None
    return found if isinstance(found, str) else None


def _configured_vault(start: Path) -> str | None:
    """The Platform Infrastructure Key Vault the configuration names."""
    name = _configured(start, FOUNDATION, "infrastructure_key_vault", "name")
    return name if name and VAULT.fullmatch(name) else None


def _subscription(start: Path) -> str | None:
    """The instance's Azure subscription, as the configuration names it."""
    return _configured(start, SETUP, "subscription_id")


def _refuse_a_second_key(
    start: Path, access: Access, template: str, development: bool, key_vault: str | None
) -> None:
    """A new keypair is only for an instance's first access to a template: one a mapped
    repository already reads through its alias has a key, and a new one would need registering
    again."""
    if not _reads_through_alias(start, access):
        return
    command = f"dp-instance template-access setup {template}" + (" --development" if development else "")
    if key_vault:
        raise failed(
            f"This instance already reads {access.template} through {access.alias}, so its key exists, "
            f"but neither this workstation nor Key Vault {key_vault} holds it; a new one would need "
            "registering again. Nothing was created.",
            Next(command, "on the workstation that holds the key: stores it in the vault"),
        )
    raise failed(
        f"This instance already reads {access.template} through {access.alias}, so its key exists, in the "
        "Platform Infrastructure Key Vault; a new one would need registering again. Nothing was created.",
        Next(
            command,
            "restores the instance's key and its SSH host alias, once dp-instance.json maps the Client "
            f"Infrastructure Repository, whose {FOUNDATION.as_posix()} names the vault",
        ),
    )


def _instance_key(given: str | None, start: Path) -> str:
    """The Platform Instance Key: the one `--instance` names, else the map's or, before the
    instance is mapped, the one the Client Infrastructure Repository checkout the command runs in
    records, whose vault this command reads. A checkout another instance's key names is refused,
    so no instance's key goes to another's vault."""
    named = _key(given, "Platform Instance Key") if given else None
    try:
        mapped = Instance.find(start).key
    except Stop:
        mapped = None
    if mapped is not None:
        return named or mapped
    found = _configured(start, SETUP, "platform_instance_key")
    recorded = _key(found, "Platform Instance Key") if found else None
    if named and recorded and named != recorded:
        raise failed(
            f"--instance names {named}, but this Client Infrastructure Repository's "
            f"{SETUP.as_posix()} records {recorded}; nothing changed."
        )
    if named or recorded:
        return named or recorded
    raise failed("Name the Platform Instance with --instance, or run this in a mapped instance folder.")


@dataclass
class Report:
    """What setup did for one template's access, and what's wrong with it."""

    access: Access
    template: str
    done: list[str] = field(default_factory=list)
    reach: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    next: list[Next] = field(default_factory=list)
    created: bool = False
    public: str | None = None
    fingerprint: str | None = None
    # How long its setup took.
    seconds: float = 0.0

    @property
    def development(self) -> bool:
        return self.access.template.endswith(DEVELOPMENT)

    def describe(self, key_vault: str | None) -> dict:
        return {
            "template": self.template,
            "development": self.development,
            "alias": self.access.alias,
            "private_key_path": str(self.access.private),
            "public_key": self.public,
            "fingerprint": self.fingerprint,
            "key_vault_secret": f"{key_vault}/{self.access.secret}" if key_vault else None,
            "created": self.created,
            "reach": self.reach,
            "problems": self.problems,
        }


def setup(
    screen: Screen,
    start: Path,
    template: str | None,
    *,
    instance: str | None,
    development: bool,
) -> Result:
    """One template's access, or, with none named, every template's in `dp-instance.json` whose
    key the vault holds, with each development repository's key the vault holds. The vault is
    the one the Client Infrastructure Repository's configuration names, the mapped one's or,
    before the instance is mapped, the checkout's setup runs in; without one, or before the vault
    exists, setup keeps the key on this workstation alone."""
    if template is not None:
        template = _key(template, "Template Repository Key")
    if development and template is None:
        raise failed(
            "--development sets up one template's development repository key: name the template. "
            "Setup with no template restores every development repository key the vault holds."
        )
    key_vault = _configured_vault(start)
    key = _instance_key(instance, start)
    every = None if template else _every_template(start, key, key_vault)
    for tool in (
        "ssh-keygen",
        "ssh",
        "git",
        *(("icacls",) if sys.platform == "win32" else ()),
        *(("az",) if key_vault else ()),
    ):
        processes.executable(tool)
    removed = remove_global_rules()
    if every is None:
        started = time.monotonic()
        access = Access(key, template + (DEVELOPMENT if development else ""))
        held = secret_state(key_vault, access.secret, _subscription(start)) if key_vault else None
        if held is None:
            key_vault = None
        copy = vault_copy(access, key_vault) if held else None
        if held and copy is None:
            # Never a new key over one the vault holds.
            raise failed(
                f"Key Vault {key_vault} holds {access.secret}, but downloading it found nothing; nothing "
                "changed. Run the command again."
            )
        report = _set_up(start, access, template, key_vault=key_vault, copy=copy, development=development)
        report.seconds = time.monotonic() - started
        reports = [report]
    else:
        reports = [report for name in every for report in _restore(start, key, name, key_vault)]
    _show(screen, key, reports, removed, single=every is None, key_vault=key_vault)

    detail = (
        reports[0].describe(key_vault)
        if every is None
        else {"templates": [report.describe(key_vault) for report in reports]}
    )
    failing = [report.access.template for report in reports if report.problems]
    if failing:
        steps = tuple(dict.fromkeys(item for report in reports for item in report.next))
        if steps:
            screen.next_block(steps)
        return Result(
            "template-access setup",
            "failed",
            f"Template access has problems for {', '.join(failing)}.",
            steps,
            {"instance": key, **detail},
        )
    return Result(
        "template-access setup",
        "done",
        "Template access is set up locally.",
        detail={"instance": key, **detail},
    )


def _every_template(start: Path, key: str, key_vault: str | None) -> list[str]:
    """With no template named: every template `dp-instance.json` maps, restored from the vault,
    which must exist."""
    if not key_vault:
        raise failed(
            "Name a template, or run this in the instance folder once dp-instance.json maps the Client "
            f"Infrastructure Repository, whose {FOUNDATION.as_posix()} names the Platform Infrastructure "
            "Key Vault to restore every template's key from.",
            Next("dp-instance template-access setup <template>", "sets up one template's access"),
        )
    templates = _mapped_templates(start)
    if not templates:
        raise failed("Name a template, or run this in a mapped instance folder: dp-instance.json names them.")
    if existing_vault(key_vault, Access(key, templates[0]).secret, _subscription(start)) is None:
        raise failed(
            f"Key Vault {key_vault}, which {FOUNDATION.as_posix()} names, doesn't exist yet, so there are "
            "no keys to restore from it.",
            Next("dp-instance template-access setup <template>", "sets up one template's access"),
        )
    return templates


def _restore(start: Path, instance: str, template: str, key_vault: str) -> list[Report]:
    """One template of a setup for every template, from the vault: its release repository key,
    set up when the vault holds it and otherwise named with what sets it up, and its development
    repository key when the vault holds one, as only Data Methods' own pre-release instance's
    does."""
    reports = []
    for development in (False, True):
        started = time.monotonic()
        access = Access(instance, template + (DEVELOPMENT if development else ""))
        try:
            copy = vault_copy(access, key_vault)
            if copy is not None:
                reports.append(
                    _set_up(start, access, template, key_vault=key_vault, copy=copy, development=development)
                )
            elif not development:
                problem, step = _unheld(start, access, key_vault)
                reports.append(Report(access, template, problems=[problem], next=[step]))
        except Stop as stop:
            reports.append(Report(access, template, problems=[stop.message], next=list(stop.next)))
        if reports and reports[-1].access == access:
            reports[-1].seconds = time.monotonic() - started
    return reports


def _set_up(
    start: Path,
    access: Access,
    template: str,
    *,
    key_vault: str | None,
    copy: Path | None,
    development: bool,
) -> Report:
    """The key, its vault copy and the SSH host alias for one template, and Git reaching it
    through the alias once the key can be registered. ``copy`` is the vault's copy of the key,
    downloaded once, which this removes."""
    try:
        done, created = _key_files(
            start, access, template, key_vault=key_vault, copy=copy, development=development
        )
    finally:
        if copy is not None:
            copy.unlink(missing_ok=True)
    update_managed_config(access)
    ensure_include(access)
    report = Report(
        access,
        template,
        done,
        created=created,
        public=access.public.read_text(encoding="utf-8").strip(),
        fingerprint=fingerprint(access),
    )
    urls = _clone_urls(start, access.template)
    if not urls:
        report.reach.append(UNMAPPED)
    elif created:
        report.reach.append("not checked until Data Methods registers the new key")
    else:
        _pass_rules(access.instance, start)
        for url in urls:
            try:
                report.reach.append(reach(access, url))
            except Stop as stop:
                report.problems.append(stop.message)
                report.next.extend(stop.next)
    return report


def _key_files(
    start: Path,
    access: Access,
    template: str,
    *,
    key_vault: str | None,
    copy: Path | None,
    development: bool,
) -> tuple[list[str], bool]:
    """Restore or create the Template Read Private Key and keep its vault copy, ``copy`` when
    the vault holds one: what was done, and whether the key is new."""
    local = access.private.is_file()
    if not local and copy is None:
        _refuse_a_second_key(start, access, template, development, key_vault)
    lines: list[str] = []
    created = False
    ensure_directory(access.ssh_root)
    ensure_directory(access.directory)
    if not local and copy is not None:
        restore_from(access, copy)
        lines.append(f"Restored the Template Read Private Key from Key Vault secret {access.secret}.")
    elif not local:
        new_keypair(access)
        lines.append("Created a new Template Read Keypair.")
        created = True

    restrict_file(access.private, private=True)
    ensure_public(access)
    if key_vault:
        if copy is not None:
            if local and not same_key(access.private, copy):
                raise failed(
                    f"The local Template Read Private Key and Key Vault secret {access.secret} don't match. "
                    "Neither copy was changed; recover or rotate the key deliberately."
                )
            lines.append(f"The local key matches Key Vault secret {access.secret}.")
        else:
            save_to_vault(access, key_vault)
            lines.append(f"Stored the Template Read Private Key in Key Vault secret {access.secret}.")
    return lines, created


def _show(
    screen: Screen,
    instance: str,
    reports: list[Report],
    removed: list[str],
    *,
    single: bool,
    key_vault: str | None,
) -> None:
    """The global rules removed, then each template's setup; the request to register a key
    only for one setup just created."""
    rows = [("instance", instance)] + ([("template", reports[0].access.template)] if single else [])
    screen.header("template-access setup", *rows)
    if removed:
        screen.blank()
    for line in removed:
        screen.note(line)
    for report in reports:
        screen.blank()
        if not single:
            screen.labelled("Template:", Text(report.access.template, style="bold"))
        for line in report.done:
            screen.note(line)
        if report.public is not None:
            screen.labelled("Private Key:", str(report.access.private))
            screen.labelled(
                "Key Vault Secret:",
                f"{key_vault}/{report.access.secret}"
                if key_vault
                else "not stored yet: a later setup stores it, once the Client Infrastructure Repository's "
                "configuration names the Platform Infrastructure Key Vault and the vault exists",
            )
            screen.labelled("SSH Host Alias:", report.access.alias)
        for text in report.reach:
            screen.labelled("Reach:", text)
        for text in report.problems:
            screen.labelled("Problem:", text, "bold red")
        if report.created:
            screen.blank()
            screen.print(Text("Send this non-secret request to Data Methods:", style="bold"))
            screen.labelled("Platform Instance Key:", instance)
            screen.labelled(
                "Template Repository Key:",
                report.template + (" (development repository)" if report.development else ""),
            )
            screen.labelled("Template Read Public Key:", report.public)
            screen.labelled("Fingerprint:", report.fingerprint)
            screen.note("Data Methods registers the key on that repository and returns its SSH clone URL.")
        screen.ended(report.access.template, report.seconds)


def _checkouts(start: Path) -> dict[str, Path]:
    """Each Component Repository's checkout, by its template: the map's or, before the instance
    is mapped, the Client Infrastructure Repository checkout the command runs in."""
    try:
        instance = Instance.find(start)
    except Stop:
        infrastructure = _infrastructure(start.resolve())
        return {INFRASTRUCTURE: infrastructure} if infrastructure is not None else {}
    return {template: mapped.path for template, mapped in instance.components.items()}


def _clone_urls(start: Path, template: str) -> list[str]:
    """The clone URLs a Component Repository `_checkouts` finds records for this template's
    access."""
    base = template.removesuffix(DEVELOPMENT)
    checkout = _checkouts(start).get(base)
    record = SourceRecord.read(checkout) if checkout is not None else None
    if record is None or record.template != base:
        return []
    return [record.development_url if template.endswith(DEVELOPMENT) else record.release_url]


def _mapped_templates(start: Path) -> list[str]:
    """The templates of the Component Repositories `dp-instance.json` maps, when it's there."""
    try:
        return list(Instance.find(start).components)
    except Stop:
        return []


def _repairs(start: Path, templates: list[str], key_vault: str | None) -> list[Next]:
    """What sets these templates' access up again: one setup from the vault for the mapped
    templates, and setup by name for any other."""
    mapped = set(_mapped_templates(start))
    restorable = bool(key_vault) and any(name.removesuffix(DEVELOPMENT) in mapped for name in templates)
    steps = (
        [
            Next(
                "dp-instance template-access setup",
                "restores each mapped template's keys from the vault and sets up their access again",
            )
        ]
        if restorable
        else []
    )
    if not key_vault or any(name.removesuffix(DEVELOPMENT) not in mapped for name in templates):
        steps.append(
            Next(
                "dp-instance template-access setup <template>",
                "repairs the key, the vault copy and the SSH configuration",
            )
        )
    return steps


def _no_access_yet(start: Path, access: Access, key_vault: str | None) -> tuple[str, Next] | None:
    """A template the instance has no access to yet: no key here, no source record naming its
    alias, and none in the vault the configuration names. Its first access."""
    if not key_vault or access.private.is_file() or _reads_through_alias(start, access):
        return None
    try:
        held = vault_has(key_vault, access.secret)
    except Stop:
        return None
    return None if held else _unheld(start, access, key_vault)


def check(
    screen: Screen,
    start: Path,
    template: str | None,
    *,
    instance: str | None,
) -> Result:
    """Every template's access set up here or mapped, or the one named, a development
    repository's key by its name, `<template>-dev`; each key against its copy in the vault the
    Client Infrastructure Repository's configuration names, when there's one."""
    key = _instance_key(instance, start)
    if template:
        templates = [_key(template, "Template Repository Key")]
    else:
        local = {template for instance, template in key_folders() if instance == key}
        templates = sorted(local | set(_mapped_templates(start)))
    if not templates:
        raise failed(
            f"No template access is set up for {key} on this workstation.",
            Next("dp-instance template-access setup <template>", "sets it up"),
        )
    key_vault = existing_vault(
        _configured_vault(start), Access(key, templates[0]).secret, _subscription(start)
    )
    screen.header("template-access check", ("instance", key))
    _pass_rules(key, start)
    report = []
    # The commands a problem names for itself, and the templates whose problems setup repairs.
    fixes: list[Next] = []
    repair: list[str] = []
    for name in templates:
        screen.begin(name)
        access = Access(key, name)
        first = _no_access_yet(start, access, key_vault)
        if first is not None:
            problem, step = first
            fixes.append(step)
            screen.blank()
            screen.labelled("Template:", Text(name, style="bold"))
            screen.labelled("Problem:", problem, "bold red")
            report.append({"template": name, "alias": access.alias, "problems": [problem], "checks": []})
            continue
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
                    copy = vault_copy(access, key_vault)
                    if copy is None:
                        problems.append(f"Key Vault {key_vault} has no secret {access.secret}")
                    else:
                        try:
                            matches = same_key(access.private, copy)
                        finally:
                            copy.unlink(missing_ok=True)
                        if matches:
                            checks.append(("Vault:", f"{key_vault}/{access.secret} matches"))
                        else:
                            problems.append(f"the local key and Key Vault secret {access.secret} don't match")
                except Stop as stop:
                    problems.append(stop.message)
        include = access.user_config.is_file() and any(
            _posix(match.group(1).strip()) == _posix(access.include)
            for match in re.finditer(
                r"(?im)^\s*include\s+(.+?)\s*$", access.user_config.read_text(encoding="utf-8")
            )
        )
        problem = (
            effective_problem(access, access.user_config)
            if include
            else f"the SSH configuration has no Include {access.include}"
        )
        if problem:
            problems.append(problem)
        else:
            checks.append(("SSH:", f"{access.alias} uses only its dedicated key"))
        urls = _clone_urls(start, name)
        if not urls:
            checks.append(("Reach:", UNMAPPED))
        fixed = 0
        for url in urls:
            try:
                checks.append(("Reach:", reach(access, url)))
            except Stop as stop:
                problems.append(stop.message)
                fixes.extend(stop.next)
                fixed += bool(stop.next)
        if len(problems) > fixed:
            repair.append(name)
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
    screen.end()
    bad = [item for item in report if item["problems"]]
    if bad:
        steps = (*dict.fromkeys(fixes), *_repairs(start, repair, key_vault))
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
