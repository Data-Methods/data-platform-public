# Linux Bastion VM

This tool creates and manages a private Ubuntu VM for platform deployment and
operational work. Operators reach the VM through Azure Bastion with their own
Microsoft Entra identity. Read the tool before running it.

## Requirements

- The object ID of the platform's existing Infra Admin Group. Its members sign
  in to the VM; the tool creates no directory roles.
- Permission to create a dedicated resource group in the selected subscription
  and to deploy its resources and Deployment Stack. Permissions on the
  platform's primary resource group do not cover this separate group.
- An RBAC administrator to grant `Virtual Machine Administrator Login` on that
  group to the Infra Admin Group after deployment.
- PowerShell 7, Git, Python 3.10 or newer, Azure CLI with Bicep, and OpenSSH
  `ssh-keygen`. Azure Cloud Shell in PowerShell mode has all of them. The tool
  installs nothing on the machine that runs it.
- A VNet range that overlaps no network the VM must reach. The defaults use
  `10.253.0.0/24`.

## Download And Verify

Each version is a Git tag named `bastion-vm-linux/<version>`. In Azure Cloud
Shell, in PowerShell mode, clone this repository and check out a version:

```powershell
$Checkout = "$HOME/data-platform-public"
$Tag = "bastion-vm-linux/1.0.0"
git clone --quiet https://github.com/Data-Methods/data-platform-public.git $Checkout
git -C $Checkout -c advice.detachedHead=false checkout --quiet $Tag
```

`git -C $Checkout tag --list "bastion-vm-linux/*"` lists the published
versions. Before you run the tool, check that the checkout is exactly the
tag's commit, with no changed or added files:

```powershell
$Commit = git -C $Checkout rev-parse HEAD
if ($Commit -ne (git -C $Checkout rev-parse "$Tag^{commit}") -or (git -C $Checkout status --porcelain)) {
    throw "$Checkout is not an unmodified checkout of $Tag."
}
"$Tag is commit $Commit"
```

The commit hash identifies the exact content of every file in the checkout.

## Configure And Deploy

Keep the configuration in its own directory, outside the checkout:

```powershell
$BastionVm = "$Checkout/tools/bastion-vm-linux/bastion-vm-linux.ps1"
New-Item -ItemType Directory -Force "$HOME/bastion-vm" | Out-Null
Set-Location "$HOME/bastion-vm"

pwsh -NoProfile -File $BastionVm configure
pwsh -NoProfile -File $BastionVm apply --dry-run
pwsh -NoProfile -File $BastionVm apply
pwsh -NoProfile -File $BastionVm authorize --dry-run
pwsh -NoProfile -File $BastionVm authorize
pwsh -NoProfile -File $BastionVm status
pwsh -NoProfile -File $BastionVm connection
```

`configure` writes the non-secret `bastion-vm.json` in the current directory.
`--file PATH` selects another configuration file. An existing
`bastion-vm.json` works unchanged: run the commands from its directory, or
pass its path with `--file`. Every Azure command uses the subscription
recorded in the file.

The default resource prefix is `dp-bastion`:

| Resource | Default name |
| --- | --- |
| Resource group | `RG-DataPlatform-Bastion` |
| VM and Deployment Stack | `dp-bastion-vm` |
| Azure Bastion | `dp-bastion` |
| VNet | `dp-bastion-vnet` |
| NIC | `dp-bastion-nic` |
| OS disk | `dp-bastion-osdisk` |
| NSG | `dp-bastion-nsg` |
| NAT gateway | `dp-bastion-nat` |
| Public IPs | `dp-bastion-pip`, `dp-bastion-nat-pip` |

The Bastion subnet has the name Azure requires, `AzureBastionSubnet`.

`configure` changes nothing in Azure. `apply --dry-run` lists what `apply`
creates or updates, including a new resource group. `authorize` assigns the
login role at the configured resource group's scope; it never creates the
group, and it changes nothing when the assignment exists. An operator without
permission to assign roles asks an RBAC administrator to run `authorize`. The
VM deploys without that assignment.

The stack owns the private VM, disk, NIC, VNet, NSG, outbound NAT, Azure
Bastion and their public IPs. The VM has no public IP and does not accept
public SSH. These resources incur Azure charges, including while the VM is
deallocated. The guest installs pinned development and deployment tools at
first boot. The tool discards the temporary SSH key that VM provisioning
requires; operators sign in with Entra SSH.

`connection` prints the subscription-selection and native Azure CLI SSH
commands. The Bastion extension authenticates the tunnel with the Azure CLI
profile's active subscription; `--subscription` on the SSH command alone is
not enough when that profile defaults to another tenant. On shared machines,
use a dedicated `AZURE_CONFIG_DIR` for this Platform Instance, sign in there,
and run both printed commands in that same profile. Do not switch another
session's shared default.

For private platform access, set the printed VNet ID in the platform's
infrastructure configuration and apply its workstation connectivity. This tool
does not manage platform peerings or private DNS.

`status` checks the stack and the login-role assignment. It does not prove
that first-boot setup succeeded, that your identity can sign in over SSH, or
that private platform endpoints resolve. Verify those before relying on the
VM.

## Upgrade

The tool has no self-upgrade command. To use a newer version, fetch the tags,
check out the new tag and verify the checkout again:

```powershell
git -C $Checkout fetch --quiet --tags
$Tag = "bastion-vm-linux/<version>"
git -C $Checkout -c advice.detachedHead=false checkout --quiet $Tag
```

A newer version uses the same `bastion-vm.json`. It does not patch an existing
VM's guest or rerun first-boot setup: `apply` updates an existing stack only
when the recorded deployment inputs differ from the configuration or the last
deployment did not succeed. Use `reconfigure` to change deployment settings;
it cannot change an existing workstation's tenant, subscription, resource
group or prefix. For a replacement, create a separate configuration with a new
prefix and a non-overlapping address space.

## Retirement

Before you delete a VM, preserve its repository changes, local state and
working files, and verify a fresh connection and real platform commands from
its replacement. Disconnect the old VM's platform peering and DNS bridge. Then,
with the old configuration:

```powershell
pwsh -NoProfile -File $BastionVm delete --dry-run
pwsh -NoProfile -File $BastionVm delete
```

Deletion removes the exact marked stack and its managed resources. For a
resource group that the tool created, it then removes the empty group and its
VM login assignment. It stops while the VNet has peerings, or while unrelated
resources, locks or other role assignments remain in a group it would remove;
review those before retrying.

A Bastion VM deployed in the platform's primary resource group stays there,
and its configuration and stack keep working. Deleting its stack preserves the
primary group and the group's login assignment. To replace it, create a
separate configuration and obtain permission for the new resource group first.
Deletion neither preserves files nor checks which VM you meant to retire.

## Troubleshooting

To print the full Azure CLI error, with tokens and secrets redacted, set
`$env:BASTION_VM_DEBUG = "1"` before running the tool.

## Tests

From the repository root, run `uv run --locked pytest`. The tests use a fake
Azure CLI and need no Azure sign-in. One test runs the PowerShell entry script
when `pwsh` is installed.
