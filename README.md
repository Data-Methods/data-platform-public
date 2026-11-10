# dp-instance

`dp-instance` prepares a Platform Instance's changes, upgrades and Configuration Changes alike, as
pull requests, applies them once they merge, and manages its
secrets and template access. It runs every component's steps across the instance's repositories
in order, showing each plan and asking before each change to the platform. Read a tool before
running it.

- [Release Manifests](platform-releases/README.md): one per Platform Version.
- [Linux Bastion VM](tools/bastion-vm-linux/README.md): a private Linux VM for platform work.

## Requirements

- A workstation inside the instance's private network, such as its Azure Virtual Desktop or
  Bastion VM: steps stop outside it.
- uv 0.13.5 or newer, Git 2.31 or newer, OpenSSH, the Azure CLI, the GitHub CLI, Terraform 1.15.x, `kubectl`
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

From then on, `dp-instance self update` installs the newest, and `dp-instance self update --to
<version>` an exact one, such as the version a command names, which stays pinned in the instance
folder until `self update` without `--to`. On Windows the install runs once the command ends, and
`dp-instance` doesn't run until the install log it names ends saying it's installed.

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
dp-instance preflight                        # each component's own checks of this workstation
dp-instance preflight --to <version>         # the checks of that Platform Version's templates
dp-instance prepare                          # turn your branch's commits into pull requests
dp-instance prepare --to <version>           # an upgrade to the next required stop, as pull requests
dp-instance prepare --no-preview             # pull requests without the preview; apply asks at each change
dp-instance plan                             # plan every step on main, the open change and your branch
dp-instance plan --step <repo/component/phase/step>    # plan one step
dp-instance apply                            # bring the platform to main, once the pull requests merge
dp-instance resume                           # carry on where the run stopped, a prepare or an apply
dp-instance verify                           # check every step
dp-instance verify --step <repo/component/phase/step>  # check one step
dp-instance secret list [<name>]             # every secret, or one, and its state
dp-instance secret set <name>                # at a terminal, replace the values you supply of one secret
dp-instance secret reload [<name>]           # restart what reads a secret changed in Key Vault
dp-instance secret rotate <name>             # replace a platform-generated secret
dp-instance unlock <run>                     # clear the run lock a hung command holds
dp-instance self update                      # install the newest dp-instance
dp-instance self update --to <version>       # install an exact dp-instance version
dp-instance --version                        # the installed dp-instance version
```

## Prepare, Then Apply

`dp-instance status` names what comes next. Run only what `dp-instance` names.

`dp-instance preflight` runs each component's own checks of this workstation: its tools, its
sign-in and its access, all at once, from `main`'s code, or with `--to <version>` from that
Platform Version's templates, before you prepare it. It changes nothing, starts no run and holds
no run lock, so it runs beside any command, signed out or before the state account exists. No
other command runs a preflight; a failure that isn't temporary names it.

A change is prepared, reviewed, merged, then applied. Edit configuration on a branch of your
checkout, commit it, and run `dp-instance prepare`; an upgrade is `dp-instance prepare --to
<version>`, the next required stop, once `main` is applied, whose notes say what changed.
`prepare` builds the change in its own copy of each repository, on `platform-change` or
`platform-<version>`, adds the commits on your branch that `main` doesn't hold, renders the files
that depend on configuration, previews every step, then pushes each changed repository's branch
and opens its pull request with its part of the preview. With nothing to push, it ends before
the preview. With `--no-preview`, it opens the pull requests without the preview, and `apply`
asks about each change; a plain `prepare` afterwards previews the change again for `apply`.
`dp-instance plan` plans every step, read only, as `prepare` would preview it, with no pull
request and no run; a step that skips and that nothing reaches isn't planned, so drift there
shows only in `apply`'s drift check. Your checkout is only an input: edits
you haven't committed are left out, or committed to your branch when you say so at a terminal.
An instance has one open change; running `prepare` again adds to it while a pull request of it
is open. A change whose pull requests were all closed is no longer open; one partly merged waits
for its closed pull requests to be reopened and merged.
An upgrade starts from what the platform applied, so `prepare --to` stops on commits your branch has
that `main` doesn't; run it from `main`, then add your edits with a plain `dp-instance prepare`.

Review and merge each pull request in GitHub, then run `dp-instance apply`, which works from
`origin/main` in its own copy. Each step with a change shows its plan file, under
`.dp-instance/runs/<run>/plans/`, beside what the review showed: a change the review showed
applies as approved by review, and any other, or any destroy or replacement, asks: `y` applies
exactly that plan, `n` stops. `apply --auto-approve` approves each change that destroys
nothing without asking, with a preview or without one; a destroy or replacement, or a change
that doesn't say whether it destroys, still asks, and without a terminal stops for `dp-instance
resume --approve <ID>`. On an instance whose code is older than Platform 2026.8.0, a
component's own engine still approves destroys under `--auto-approve`. Approval is the call of the person accountable for the instance;
stop and ask about any destroy (`-`) or replace (`-/+`) of data, or a change the release notes
don't explain.

A step that holds a component's work until a later step, such as Data Engineering's `hold` of
Dagster runs until its `release`, applies only just before the first step between them with a
change, whose question asks for both; with no change between them, nothing is held. The later
step lifts the hold in its turn, or each Environment's deploy does when the Adoption PR changes
what it runs.

Every stop ends with **To Continue**, the commands that continue:

- **Failure:** `dp-instance preflight` may name what the workstation lacks; once it's fixed,
  `dp-instance resume`.
- **Decision:** review the plan, then `dp-instance resume`.
- **Drift:** a change made outside a run; nothing has changed. The accountable person writes it
  into the configuration, with `dp-instance prepare`, or reverts it; then apply again. A renewal
  the platform owns, such as a certificate's when it's due, isn't drift: its step plans and asks
  in its turn.
- **Conflict:** your branch conflicts with the open change's branch, `origin/<branch>`, or with
  `origin/main`, naming the files; rebase it onto the one named, then prepare again. When the
  open change's branch itself conflicts with `origin/main`, rebase it, resolve the conflicts and
  push it, then prepare again.
- **External handoff:** whoever holds the rights does the work on
  `.dp-instance/runs/<run>/handoffs/<system>.md`; then `c`, or `dp-instance resume`.
- **Values:** a person runs `dp-instance resume` at a terminal and types them.
- **Sign-in:** run the command it names, such as `az login`, then `dp-instance resume`.
- **Runs to finish:** Dagster runs still going; `dp-instance resume` waits again.
- **Pending:** a step still waits on an earlier change or outside work; `dp-instance resume`
  checks it again.

A run's record is in the instance's shared run record, in its Terraform state account, and in
`.dp-instance/runs/<run>/` on the workstation walking it, so `status` and `resume` work from any
workstation. A resumed prepare starts again with the commits it took. Resuming a run someone
else walked last, or replacing a stopped run with a new `apply` or `prepare --to`, asks first;
`prepare`, `plan` and the `secret` commands run beside a stopped run, naming it. On a first
deployment, a run keeps its record on its workstation until the step that creates the state
account; there, merge the configuration by hand and run `dp-instance apply`.

One command at a time holds the instance's run lock, while it runs; another refuses, naming who
holds it and since when. A command that crashes leaves the lock within a minute. `dp-instance
unlock <run>` clears it, only for a command that hangs while its process is alive.

Never stop a run while a step applies. A run that stops while Data Engineering's hold is on
queues new Dagster runs: resume promptly, or lift the hold with the command its **To Continue** names.

A completed run may recommend optional handoffs: outside work that nothing waits on, each on
its page under `.dp-instance/runs/<run>/handoffs/`.

When an apply opens the Managed Repository's adoption pull request, its Production Data
Engineers merge it, deploying Integration, and release it, deploying Production; the next
upgrade is prepared only once it's released.

## Troubleshooting

- **A command names an install:** run it, then your command again.
- **`os error 1224` on Windows:** another program, such as an antivirus scan, held a file open
  while uv installed. `self update` tries again on its own; if the install still fails, no
  `dp-instance` may be left, so run the install line again in a new shell.
- **uv certificate errors behind a proxy:** set `UV_NATIVE_TLS=1`.
- **Private endpoints resolve publicly:** work from inside the instance's network.
- **Azure DevOps refuses after you activate a role:** re-evaluate your permissions in its User
  settings, Permissions.
- **A path too long on Windows:** move the folder near the drive root, or turn on Windows long
  paths and run `git config --global core.longpaths true`.
- **An instance before Platform 2026.7.0:** dp-instance 5.0.1 walks it, which every command
  names.

## For Agents

Every command takes `--json`: one JSON object per line on stdout, ending with a result naming the
stop and the next command; nothing is asked. Run in the background, never under a timeout. Bring
each decision's plan file and ID to the accountable person; on their word, run
`dp-instance resume --approve <ID>`; `--auto-approve` only when they say so. Taking over another
person's run and replacing a stopped run are decisions too, each answered with the command and
ID it names.

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
