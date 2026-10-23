# dp-instance

`dp-instance` upgrades a Platform Instance, applies its configuration changes and manages its
secrets and template access. It runs every component's steps across the instance's repositories
in order, showing each plan and asking before each change. Read a tool before running it.

- [Release Manifests](platform-releases/README.md): one per Platform Version.
- [Linux Bastion VM](tools/bastion-vm-linux/README.md): a private Linux VM for platform work.

## Requirements

- A workstation inside the instance's private network, such as its Azure Virtual Desktop or
  Bastion VM: steps stop outside it.
- uv, Git, OpenSSH, the Azure CLI, the GitHub CLI, Terraform 1.15.x, `kubectl` and `kubelogin`.
- The Azure CLI signed in to the instance's tenant and subscription. On a shared machine, set
  `AZURE_CONFIG_DIR` to this instance's own folder in every shell before `az login`.
- Membership of the instance's Platform Operators group and Infra Admin Group, every role
  activated before you start: a missing right shows only at the step that needs it.
- The GitHub CLI signed in (`gh auth login`, then `gh auth setup-git`), a Git user name and email,
  and your GitHub account in the instance's Platform Operators team.
- No administrator rights. On Windows, the OpenSSH client.
- On Windows, Git runs its own bundled `ssh`, which reads `.ssh` in Git's `HOME`, normally your
  user profile. When Git can't use the template aliases, such as with a `HOME` outside your
  user profile, `template-access setup` and `check` stop and name the command that has Git run
  Windows' OpenSSH instead:
  `git config --global core.sshCommand "C:/Windows/System32/OpenSSH/ssh.exe"`.

## Install

Install the commit a release's manifest names as `dp_instance_commit`, then start a new shell:

```bash
uv tool install --reinstall "git+https://github.com/Data-Methods/data-platform-public@<commit>#subdirectory=tools/dp-instance"
uv tool update-shell
```

In a new, empty folder, outside any folder OneDrive, iCloud Drive, Dropbox or Google Drive syncs:

```bash
git clone <Client Infrastructure Repository URL>
dp-instance setup     # prints a git clone for each other repository: clone them, run setup again
dp-instance template-access setup --key-vault <vault>   # every template in dp-instance.json
dp-instance template-access check
```

`<vault>` is `infrastructure_key_vault.name` in `config/infrastructure/platform_foundation.json`:
`setup` restores each template's key from it and names any template it couldn't set up. At a
first deployment, before the vault exists, run `dp-instance template-access setup <template>`
for each template instead, and send the public key it prints to Data Methods, never the
private key.

A checkout's Python environment, its `.venv`, doesn't survive a move: after moving a checkout,
delete its `.venv` and run `uv sync --locked` in it, with the extras its own docs use, such as
`--extra workspace` in the Managed Repository.

## Use

```bash
dp-instance status                           # where the instance stands; changes nothing
dp-instance upgrade                          # to the next stop: the next Platform Version, or a newer fix
dp-instance upgrade --to <Platform Version>  # to a release you name
dp-instance plan                             # preview a configuration change
dp-instance apply --change "<what changes>"  # apply this branch's committed configuration
dp-instance resume                           # carry on where the run stopped
dp-instance verify                           # check every step
dp-instance secret list [<name>]             # every secret, or one, and its state
dp-instance secret set <name>                # at a terminal, replace the values you supply of one secret
dp-instance secret reload [<name>]           # restart what reads a secret changed in Key Vault
dp-instance secret rotate <name>             # replace a platform-generated secret
```

Before an upgrade, read each changed template's release notes, at its commit in the manifest's
`sources`, outside the instance folder:

```bash
git init release-notes && cd release-notes
git fetch --depth=1 git@dm-<instance>-<template>:Data-Methods/<template>-template.git <commit>
git show FETCH_HEAD:docs/releases/<Platform Version>.md
```

A step with a change shows its plan file, under `.dp-instance/runs/<run>/plans/`: `y` applies
exactly that plan, `n` stops. Approval is the call of the person accountable for the instance;
stop and ask about any destroy (`-`) or replace (`-/+`) of data, or a change the release notes
don't explain. Until the first platform step is approved, only branches have changed.

Every stop ends with **Next**, the commands that continue:

- **Decision:** review the plan, then `dp-instance resume`.
- **Drift:** the upgrade has changed nothing. The accountable person fixes it with a
  configuration change; then upgrade again.
- **External handoff:** whoever holds the rights does the work on
  `.dp-instance/runs/<run>/handoffs/<system>.md`; then `c`, or `dp-instance resume`.
- **Values:** a person runs `dp-instance resume` at a terminal on the run's workstation and
  types them.
- **Sign-in:** run the command it names, such as `az login`, then `dp-instance resume`.
- **Runs to finish:** Dagster runs still going; `dp-instance resume` waits again.

A run lives on the workstation that started it. Never stop one while a step applies. A run
stopped inside Data Engineering's hold queues new Dagster runs: resume promptly, or lift the hold
in the Client Infrastructure Repository:
`uv run --locked dp-data-engineering apply --step release`.

When `dp-instance resume` can't get past one step, and the accountable person has reviewed
its plan, apply that step alone with its component CLI. Run it in the step's repository, on
the run's branch, such as `platform-2026.3.1` for the change `Platform 2026.3.1`, with the
arguments `dp-instance` passes; the run's log, `.dp-instance/runs/<run>/log.txt`, shows them:

```bash
uv run --locked <component CLI> apply --phase <phase> --step <step> --platform-version <Platform Version> --change "<change>"
```

At a terminal it shows the step's plan and asks before applying it. It applies only that step,
publishes the step's outputs, commits and pushes on the run's branch any files the step
writes, and verifies it. Then `dp-instance resume` carries on: it plans that step again, finds
no change and goes on. If the step committed files, `resume` says the repository moved and
names the command that continues.

An upgrade ends with the Managed Repository's adoption pull request open, for its Production
Data Engineers to merge, deploying Integration, and release, deploying Production:
`gh release create vX.Y.Z --target <main commit> --generate-notes`.

## Troubleshooting

- **A command prints an install command:** run it, then your command again.
- **uv certificate errors behind a proxy:** set `UV_NATIVE_TLS=1`.
- **Private endpoints resolve publicly:** work from inside the instance's network.
- **Azure DevOps refuses after you activate a role:** re-evaluate your permissions in its User
  settings, Permissions.
- **A path too long on Windows:** move the folder near the drive root, or turn on Windows long
  paths and run `git config --global core.longpaths true`.
- **Branch `platform-<version>` already exists:** if its pull request is open, run
  `dp-instance resume`; otherwise delete the branch locally and on GitHub, then upgrade.

## For Agents

Every command takes `--json`: one JSON object per line on stdout, ending with a result naming the
stop and the next command; nothing is asked. Run in the background, never under a timeout. Bring
each decision's plan file and ID to the accountable person; on their word, run
`dp-instance resume --approve <ID>`; `--auto-approve` only when they say so.

| Exit code | Stop |
| --- | --- |
| 0 | Done |
| 1 | Failed, drift, or a repository missing from the map |
| 2 | Usage error |
| 3 | External handoff |
| 4 | Values only a person supplies |
| 5 | Decision |
| 6 | Sign-in to renew |
| 7 | Runs to finish |
