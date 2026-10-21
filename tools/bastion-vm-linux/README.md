# Linux Bastion VM

This tool creates and manages a private Ubuntu VM for platform deployment and operational work,
reached through Azure Bastion with your own Microsoft Entra identity. At first boot the VM installs
the platform's tools at pinned versions. Its resources incur Azure charges, even while the VM is
deallocated. Read the tool before running it.

## Requirements

- The object ID of the platform's Infra Admin Group, whose members sign in to the VM.
- Permission to create a dedicated resource group in the subscription and deploy its resources
  and Deployment Stack.
- An RBAC administrator to run `authorize` after deployment.
- PowerShell 7, Git, Python 3.10 or newer, the Azure CLI with Bicep, and OpenSSH `ssh-keygen`.
  Azure Cloud Shell in PowerShell mode has them all.
- A VNet range that overlaps no network the VM must reach; the default is `10.253.0.0/24`.

## Install

Each version is a Git tag, `bastion-vm-linux/<version>`. In Azure Cloud Shell, in PowerShell mode,
clone this repository at a version and check that the checkout is exactly that tag:

```powershell
$Checkout = "$HOME/data-platform-public"
$Tag = "bastion-vm-linux/1.1.0"
git clone --quiet https://github.com/Data-Methods/data-platform-public.git $Checkout
git -C $Checkout -c advice.detachedHead=false checkout --quiet $Tag

$Commit = git -C $Checkout rev-parse HEAD
if ($Commit -ne (git -C $Checkout rev-parse "$Tag^{commit}") -or (git -C $Checkout status --porcelain)) {
    throw "$Checkout is not an unmodified checkout of $Tag."
}
"$Tag is commit $Commit"
```

`git -C $Checkout tag --list "bastion-vm-linux/*"` lists the versions. To upgrade, run
`git -C $Checkout fetch --quiet --tags`, check out the new tag and run the check again. A new
version uses the same `bastion-vm.json` and doesn't change an existing VM's guest.

## Use

Keep the configuration in its own folder, outside the checkout, and run the tool there:

```powershell
$BastionVm = "$Checkout/tools/bastion-vm-linux/bastion-vm-linux.ps1"
New-Item -ItemType Directory -Force "$HOME/bastion-vm" | Out-Null
Set-Location "$HOME/bastion-vm"
```

```powershell
pwsh -NoProfile -File $BastionVm configure           # writes bastion-vm.json; changes nothing in Azure
pwsh -NoProfile -File $BastionVm reconfigure         # changes the deployment settings
pwsh -NoProfile -File $BastionVm apply --dry-run     # lists what apply creates or updates
pwsh -NoProfile -File $BastionVm apply               # deploys or updates the VM
pwsh -NoProfile -File $BastionVm authorize --dry-run
pwsh -NoProfile -File $BastionVm authorize           # grants the Infra Admin Group the VM login role
pwsh -NoProfile -File $BastionVm status              # checks the stack and the login role
pwsh -NoProfile -File $BastionVm connection          # prints the commands that connect over SSH
pwsh -NoProfile -File $BastionVm tunnel              # a browser proxy to the platform's private sites
pwsh -NoProfile -File $BastionVm delete --dry-run
pwsh -NoProfile -File $BastionVm delete
```

Every action takes `--file <path>` for another configuration file; `apply`, `authorize` and
`delete` take `--yes` to skip the confirmation.

- `apply` prints the VM's VNet ID. Set it in the platform's infrastructure configuration and apply
  its workstation connectivity: this tool manages no peering or private DNS.
- `status` doesn't check first-boot setup, your SSH sign-in or private platform endpoints: check
  those before relying on the VM.
- `reconfigure` can't change the tenant, subscription, resource group or prefix. For a
  replacement VM, create a separate configuration with a new prefix and a non-overlapping range.
- On a shared machine, use a dedicated `AZURE_CONFIG_DIR` for the instance, sign in there, and
  run both commands `connection` prints in it.

**Tunnel.** `tunnel` runs on your own macOS, Windows or Linux machine. It needs PowerShell 7,
Python 3.10 or newer, the Azure CLI with its `bastion` and `ssh` extensions, OpenSSH, Chrome or
Edge, a checkout of the same version, verified as above, the instance's `bastion-vm.json`, and the
login role `authorize` grants. Once the proxy listens on 127.0.0.1 (port 1080, or `--port`), run
the Chrome or Edge command it prints in another terminal: that profile opens the platform's
private sites by their real names. To stop, close that browser, then press Ctrl-C. Any local user
can use the proxy while it runs, so run it only on a machine you alone use. Delete
`~/.data-platform-bastion-vm/<platform-instance-key>/browser-profile` to discard the profile.

**Delete.** First preserve the VM's repository changes and working files, check its replacement,
and disconnect its platform peering and DNS bridge. `delete` removes the stack's resources and,
for a resource group the tool created, the group and its login role assignment.

## Troubleshooting

- **The full Azure CLI error:** set `$env:BASTION_VM_DEBUG = "1"` and run again; tokens and
  secrets stay redacted.
- **`authorize` is refused:** ask an RBAC administrator to run it. The VM deploys without it.
- **SSH or `tunnel` signs in to the wrong subscription:** the Bastion extension uses the Azure CLI
  profile's active subscription. Run the `az account set` command `connection` prints first.
- **The port is in use:** run `tunnel --port <port>`.
- **The tunnel drops:** run `tunnel` again.
- **A sign-in in the tunnel's browser is blocked by location:** Conditional Access sees the VM's
  outbound address.
- **`delete` stops:** the VNet has peerings, or unrelated resources, locks or role assignments
  remain in the group. Review them, then run it again.
- **`snow` is missing on a VM from an earlier version:** install it on the VM:

```bash
curl -fL --proto '=https' --tlsv1.2 -o /tmp/snowflake-cli.deb \
  https://sfc-repo.snowflakecomputing.com/snowflake-cli/linux_x86_64/3.29.0/snowflake-cli-3.29.0.x86_64.deb
echo "658f43adb127e4a7c6066d995128ce8020d84ea3ed7fe6716055b00b7bf4c8d6  /tmp/snowflake-cli.deb" | sha256sum --check
sudo dpkg -i /tmp/snowflake-cli.deb
rm /tmp/snowflake-cli.deb
snow --version
```
