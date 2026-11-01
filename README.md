# dp-instance

`dp-instance` upgrades a Platform Instance, applies its configuration changes and manages its
secrets and template access. It runs every component's steps across the instance's repositories
in order, showing each plan and asking before each change to the platform. Read a tool before
running it.

- [Release Manifests](platform-releases/README.md): one per Platform Version.
- [Linux Bastion VM](tools/bastion-vm-linux/README.md): a private Linux VM for platform work.

## Requirements

- A workstation inside the instance's private network, such as its Azure Virtual Desktop or
  Bastion VM: steps stop outside it.
- uv, Git 2.31 or newer, OpenSSH, the Azure CLI, the GitHub CLI, Terraform 1.15.x, `kubectl`
  and `kubelogin`.
- The Azure CLI signed in to the instance's tenant and subscription. On a shared machine, set
  `AZURE_CONFIG_DIR` to this instance's own folder in every shell before `az login`.
- The rights your client gives you to deploy the platform.
- The GitHub CLI signed in (`gh auth login`, then `gh auth setup-git`), a Git user name and email,
  and your GitHub account in the instance's Platform Operators team.
- No administrator rights. On Windows, the OpenSSH client.
- On Windows, Git runs its own bundled `ssh`, which reads `.ssh` in Git's `HOME`, normally your
  user profile. When Git can't use the template aliases, such as with a `HOME` outside your
  user profile, `template-access setup` and `check` stop and name the command that has Git run
  Windows' OpenSSH instead:
  `git config --global core.sshCommand "C:/Windows/System32/OpenSSH/ssh.exe"`.

## Install

Install the newest `dp-instance`, then start a new shell:

```bash
uv tool install --reinstall "git+https://github.com/Data-Methods/data-platform-public#subdirectory=tools/dp-instance"
uv tool update-shell
```

## Set Up

In a new, empty folder that no sync client manages, clone the Client Infrastructure Repository
(whoever asked for the work has its URL) and run `dp-instance setup`. Each command names the
next.

```bash
git clone <Client Infrastructure Repository URL>
dp-instance setup
```

A checkout's Python environment, its `.venv`, doesn't survive a move: after moving a checkout,
delete its `.venv` and run `uv sync --locked` in it, with the extras its own docs use, such as
`--extra workspace` in the Managed Repository.

## Use

```bash
dp-instance status                           # where the instance stands; changes nothing
dp-instance upgrade                          # to the next stop: the next Platform Version, or a newer fix
dp-instance plan                             # preview a configuration change
dp-instance plan --step <repo/component/phase/step>    # preview one step
dp-instance apply                            # apply main's configuration, once its change merges
dp-instance resume                           # carry on where the run stopped
dp-instance verify                           # check every step
dp-instance verify --step <repo/component/phase/step>  # check one step
dp-instance secret list [<name>]             # every secret, or one, and its state
dp-instance secret set <name>                # at a terminal, replace the values you supply of one secret
dp-instance secret reload [<name>]           # restart what reads a secret changed in Key Vault
dp-instance secret rotate <name>             # replace a platform-generated secret
dp-instance update                           # install the newest dp-instance
```

## Upgrade

`dp-instance status` names what comes next. Run only what `dp-instance` names. The notes it
copies say what changed.

The code part's upgrade steps change only branches, each shown in a draft pull request, and
apply without asking unless one restores an edited template file or removes a file. Every other
step with a change shows its plan file, under `.dp-instance/runs/<run>/plans/`: `y` applies
exactly that plan, `n` stops. Approval is the call of the person accountable for the instance;
stop and ask about any destroy (`-`) or replace (`-/+`) of data, or a change the release notes
don't explain.

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
- **Pending:** a step still waits on an earlier change or outside work; `dp-instance resume`
  checks it again.

A run lives on the workstation that started it. Never stop one while a step applies. A run
stopped inside Data Engineering's hold queues new Dagster runs: resume promptly.

A completed run may recommend optional handoffs: outside work that nothing waits on, each on
its page under `.dp-instance/runs/<run>/handoffs/`.

When an upgrade opens the Managed Repository's adoption pull request, its Production Data
Engineers merge it, deploying Integration, and release it, deploying Production.

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
| 8 | A step still pending |
