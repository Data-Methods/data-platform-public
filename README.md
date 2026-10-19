# Data Platform Public

Public client-side utilities from Data Methods:

- [`tools/template-access/`](tools/template-access/template-access.ps1) prepares an
  SSH key for read-only access to one private Component Template Repository.
- [`tools/bastion-vm-linux/`](tools/bastion-vm-linux/README.md) creates and manages
  a private Linux VM for platform work, reached through Azure Bastion.

Each tool has its own versions, published as Git tags named `<tool>/<version>`,
such as `template-access/1.0.0`. Read a tool before running it.

## Template access

The tool prepares an SSH key for read-only access to one private Component Template
Repository for one Platform Instance. It does **not** grant access by itself and
does not send the private key to Data Methods. Read the script before running it.

Requirements: PowerShell 7, OpenSSH (`ssh` and `ssh-keygen`), and an approved
workstation. Azure CLI is needed only when using `-KeyVaultName`. The signed-in
Azure identity must have access to the selected Key Vault secret.

Clone this public repository, inspect the script, and run it from that checkout:

```powershell
git clone https://github.com/Data-Methods/data-platform-public.git
cd data-platform-public
pwsh -NoProfile -File ./tools/template-access/template-access.ps1 `
  -PlatformInstanceKey <platform-instance-key> `
  -TemplateRepositoryKey <template-repository-key>
```

The keys above are supplied by Data Methods during onboarding. The script does
not contain a private-repository allowlist or guess a clone URL. It prints the
public key, its fingerprint, and the exact Platform Instance and Template
Repository keys. Send only those **non-secret** fields through your established
authenticated channel. Data Methods registers the public key for that exact
repository and returns its SSH clone URL. Do not send the private key.

Once the Platform Infrastructure Key Vault exists, run the same command with
`-KeyVaultName <vault-name>`. The tool verifies or stores the **same** private
key in `dm-template-read-<template-repository-key>`. On another approved
workstation, the same invocation can restore it. If local and vault keys
disagree, the tool stops instead of rotating either key. The tool reads or
writes only the named secret; it does not list the vault.

The tool creates a dedicated Ed25519 key at
`~/.ssh/data-platform/<platform-instance-key>/<template-repository-key>/template-read`
and an SSH host alias of the form
`dm-<platform-instance-key>-<template-repository-key>`. It places an `Include`
at the beginning of the user's SSH config, preserving other entries, and
checks the effective SSH settings before replacing that config. It refuses
conflicting identity rules. It never edits a symlinked SSH config. The key has
**no passphrase** so approved Git operations can run unattended. Protect the
workstation and Key Vault accordingly; this key grants read-only access to the
specific template repository after Data Methods registers it. The script
restricts private files to the current user.

For an existing Infrastructure key in the older location or older Key Vault
secret `dm-template-read-key`, use `-MigrateLegacyInfrastructureKey` with
`-TemplateRepositoryKey data-platform-infrastructure`. Adoption is explicit:
the tool compares available copies, refuses mismatches, and leaves the legacy
file and secret in place. Do not use this switch to replace or rotate a key.

The script prints paths and non-secret public-key information, never the
private-key value. It does not contact Data Methods; Azure CLI calls go only
to the Key Vault you name. To test locally without Azure, run
`pwsh -NoProfile -File ./tests/template-access.ps1`.

## Linux Bastion VM

See the [Linux Bastion VM README](tools/bastion-vm-linux/README.md) for its
requirements, download and verification commands, and lifecycle.
