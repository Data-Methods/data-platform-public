from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .azure import AzureCli, CommandError
from .changes import Change
from .config import WorkstationConfig, validate_context

VM_ADMIN_LOGIN_ROLE_ID = "1c0163c0-47e6-4577-8991-ea5c82e286e4"
TEMPLATE = Path(__file__).resolve().parents[1] / "resources" / "main.bicep"
WORKSTATION_SIZE = "Standard_D2s_v7"


def _authorization_context(config: WorkstationConfig) -> tuple[str, str]:
    return config.resource_group_id, config.infra_admin_group_object_id


def _authorization_assignment(
    config: WorkstationConfig,
    azure: AzureCli,
) -> dict[str, object] | None:
    scope, principal_id = _authorization_context(config)
    assignments = azure.run(
        "role",
        "assignment",
        "list",
        "--assignee-object-id",
        principal_id,
        "--scope",
        scope,
        "--role",
        VM_ADMIN_LOGIN_ROLE_ID,
        "--output",
        "json",
        parse_json=True,
    )
    if not isinstance(assignments, list):
        raise CommandError("Azure returned an invalid Bastion VM authorization response")
    exact = [
        item
        for item in assignments
        if isinstance(item, dict)
        and str(item.get("scope", "")).casefold() == scope.casefold()
        and str(item.get("roleDefinitionId", "")).rsplit("/", 1)[-1].casefold()
        == VM_ADMIN_LOGIN_ROLE_ID
    ]
    if len(exact) > 1:
        raise CommandError("Azure returned duplicate Bastion VM login assignments")
    return exact[0] if exact else None


def authorization_changes(config: WorkstationConfig, azure: AzureCli) -> list[Change]:
    if resource_group(config, azure) is None:
        raise CommandError("Run apply to create the Bastion resource group before authorize.")
    if _authorization_assignment(config, azure) is not None:
        return []
    resource_group_scope, _principal_id = _authorization_context(config)
    return [
        Change(
            "assign Azure role",
            "Virtual Machine Administrator Login to the Infra Admin Group",
            "allow activated and permanent Infra Admins to use Entra SSH on the Bastion VM",
            (f"Scope: {resource_group_scope}",),
        )
    ]


def authorize(config: WorkstationConfig, azure: AzureCli) -> None:
    if resource_group(config, azure) is None:
        raise CommandError("Run apply to create the Bastion resource group before authorize.")
    if _authorization_assignment(config, azure) is not None:
        return
    scope, principal_id = _authorization_context(config)
    assignment = azure.run(
        "role",
        "assignment",
        "create",
        "--assignee-object-id",
        principal_id,
        "--assignee-principal-type",
        "Group",
        "--role",
        VM_ADMIN_LOGIN_ROLE_ID,
        "--scope",
        scope,
        "--output",
        "json",
        parse_json=True,
    )
    if not isinstance(assignment, dict) or not assignment.get("id"):
        raise CommandError("Azure did not return the Bastion VM login assignment ID")


def blockers(config: WorkstationConfig, azure: AzureCli) -> list[str]:
    if (
        resource_group(config, azure) is not None
        and _authorization_assignment(config, azure) is not None
    ):
        return []
    return [
        "Run 'bastion-vm-linux.ps1 authorize' after apply to grant the Infra Admin Group VM login."
    ]


def _stack(
    config: WorkstationConfig,
    desired: WorkstationConfig,
    azure: AzureCli,
) -> dict[str, object] | None:
    if resource_group(desired, azure) is None:
        return None
    stack_name = f"{desired.name_prefix}-vm"
    stacks = azure.run(
        "stack",
        "group",
        "list",
        "--resource-group",
        desired.resource_group_name,
        "--query",
        f"[?name=='{stack_name}']",
        "-o",
        "json",
        parse_json=True,
    )
    if not isinstance(stacks, list):
        raise CommandError("Azure returned an invalid deployment stack response")
    if len(stacks) > 1:
        raise CommandError(f"multiple Bastion VM stacks match {stack_name}")
    if not stacks:
        return None
    stack = stacks[0]
    ownership = config.marker
    if ownership not in str(stack.get("description", "")):
        raise CommandError(
            f"Bastion VM stack {stack_name} exists without this Platform Instance marker"
        )
    return stack


def parameters(config: WorkstationConfig) -> dict[str, str]:
    return {
        "location": config.location,
        "namePrefix": config.name_prefix,
        "adminUsername": "azureuser",
        "vmSize": WORKSTATION_SIZE,
        "vnetAddressPrefix": config.vnet_address_prefix,
        "bastionSubnetPrefix": config.bastion_subnet_prefix,
        "workstationSubnetPrefix": config.workstation_subnet_prefix,
    }


def changes(config: WorkstationConfig, azure: AzureCli) -> list[Change]:
    desired = config
    stack = _stack(config, desired, azure)
    if stack is not None:
        actual = stack.get("parameters") or {}
        changed = [
            key
            for key, expected in parameters(config).items()
            if (actual.get(key) or {}).get("value") != expected
        ]
        if not changed and str(stack.get("provisioningState", "")).casefold() == "succeeded":
            return []
        return [
            Change(
                "update",
                str(stack["name"]),
                "apply changed deployment inputs or resume an incomplete deployment",
                tuple(f"Changed input: {key}" for key in changed),
            )
        ]
    group = resource_group(config, azure)
    if group is not None and not _owns_group(group):
        raise CommandError(
            "A new Bastion stack requires a dedicated Bastion resource group. "
            "Use a separate configuration for the replacement; keep the existing group unchanged."
        )
    return [
        *(
            [
                Change(
                    "create",
                    f"resource group {config.resource_group_name}",
                    "own the Bastion lifecycle independently of platform infrastructure",
                )
            ]
            if group is None
            else []
        ),
        Change(
            "create",
            f"Bastion VM stack {desired.name_prefix}-vm",
            "provide a private Linux workstation reached through Azure Bastion",
        ),
    ]


def _run_ssh_keygen(*args: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("ssh-keygen")
    if executable is None:
        raise CommandError("OpenSSH ssh-keygen is required for Bastion VM deployment")
    result = subprocess.run(
        [executable, *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown ssh-keygen error"
        raise CommandError(f"ssh-keygen failed: {detail}")
    return result


def _create_ssh_key(path: Path) -> str:
    public = path.with_name(f"{path.name}.pub")
    _run_ssh_keygen("-t", "ed25519", "-f", str(path), "-C", "dp-bastion-vm", "-N", "")
    return public.read_text(encoding="utf-8").strip()


@contextmanager
def _temporary_ssh_public_key() -> Iterator[str]:
    with tempfile.TemporaryDirectory(prefix="data-platform-bastion-vm-") as directory:
        yield _create_ssh_key(Path(directory) / "provisioning-key")


def _owns_group(group: dict[str, object]) -> bool:
    return (group.get("tags") or {}).get("data-platform-component-key") == "bastion-vm"


def resource_group(config: WorkstationConfig, azure: AzureCli) -> dict[str, object] | None:
    validate_context(config, azure)
    exists = azure.run("group", "exists", "--name", config.resource_group_name).strip().casefold()
    if exists == "false":
        return None
    if exists != "true":
        raise CommandError("Azure returned an invalid resource group existence response")
    group = azure.run(
        "group", "show", "--name", config.resource_group_name, "-o", "json", parse_json=True
    )
    if (
        not isinstance(group, dict)
        or str(group.get("id", "")).casefold() != config.resource_group_id.casefold()
    ):
        raise CommandError("Azure resource group does not match the configuration")
    tags = group.get("tags") or {}
    if (
        not isinstance(tags, dict)
        or tags.get("data-platform-instance-key") != config.platform_instance_key
    ):
        raise CommandError("Resource group does not carry this Platform Instance's marker")
    if tags.get("data-platform-component-key") not in {"bastion-vm", "platform-resource-group"}:
        raise CommandError(
            "Resource group is not marked as Bastion or primary platform infrastructure"
        )
    return group


def _print_connection(desired: WorkstationConfig, azure: AzureCli) -> None:
    vm_id = azure.run(
        "vm",
        "show",
        "--resource-group",
        desired.resource_group_name,
        "--name",
        f"{desired.name_prefix}-vm",
        "--query",
        "id",
        "-o",
        "tsv",
    )
    if not vm_id:
        raise CommandError(
            f"Bastion VM {desired.name_prefix}-vm was not found in {desired.resource_group_name}"
        )
    bastion_id = azure.run(
        "network",
        "bastion",
        "show",
        "--resource-group",
        desired.resource_group_name,
        "--name",
        desired.name_prefix,
        "--query",
        "id",
        "-o",
        "tsv",
    )
    if not bastion_id:
        raise CommandError(
            f"Azure Bastion {desired.name_prefix} was not found in {desired.resource_group_name}"
        )
    print("Connect from a local shell with your activated or permanent Infra Admin identity:")
    print(
        "Use a dedicated AZURE_CONFIG_DIR when other sessions share this machine. "
        "Bastion tunnel authentication uses that profile's active subscription."
    )
    print(f"  az account set --subscription {desired.subscription_id}")
    print(
        f"  az network bastion ssh --name {desired.name_prefix} "
        f"--resource-group {desired.resource_group_name} --target-resource-id {vm_id} "
        f"--subscription {desired.subscription_id} "
        "--auth-type AAD"
    )
    print()
    print(
        "Browser-based Bastion access is also available, but is not recommended for deployment work:"
    )
    print(f"  https://portal.azure.com/#resource{vm_id}/connect")


def connection(config: WorkstationConfig, azure: AzureCli) -> None:
    desired = config
    if _stack(config, desired, azure) is None:
        raise CommandError(
            "Bastion VM is not deployed; run 'status' and apply any required changes"
        )
    _print_connection(desired, azure)


def apply(config: WorkstationConfig, azure: AzureCli) -> None:
    desired = config
    changes(config, azure)
    if not TEMPLATE.is_file():
        raise CommandError(f"missing Bastion VM template: {TEMPLATE}")
    account = azure.account()
    if resource_group(config, azure) is None:
        azure.run(
            "group",
            "create",
            "--name",
            config.resource_group_name,
            "--location",
            config.location,
            "--tags",
            f"data-platform-instance-key={config.platform_instance_key}",
            "data-platform-component-key=bastion-vm",
            "-o",
            "none",
        )
        if resource_group(config, azure) is None:
            raise CommandError("Bastion resource group creation did not complete")
    location = desired.location
    stack_name = f"{desired.name_prefix}-vm"
    print("Bastion VM deployment stack:")
    print()
    print(f"  Tenant ID:              {account['tenantId']}")
    print(f"  Subscription ID:        {account['id']}")
    print(f"  Bastion resource group: {desired.resource_group_name}")
    print(f"  Location:               {location}")
    print(f"  Stack:                  {stack_name}")
    print(f"  VM SKU:                 {WORKSTATION_SIZE}")
    print()
    print("A temporary SSH key will be used only to satisfy VM provisioning requirements.")
    with _temporary_ssh_public_key() as public_key:
        azure.run_with_heartbeat(
            "stack",
            "group",
            "create",
            "--name",
            stack_name,
            "--resource-group",
            desired.resource_group_name,
            "--template-file",
            str(TEMPLATE),
            "--parameters",
            *(f"{key}={value}" for key, value in parameters(config).items()),
            f"adminSshPublicKey={public_key}",
            "--action-on-unmanage",
            "deleteResources",
            "--resources-without-delete-support",
            "detach",
            "--deny-settings-mode",
            "none",
            "--description",
            (f"Data Platform Bastion VM resources; {config.marker}"),
            "--yes",
            "--output",
            "none",
            heartbeat_label="Bastion VM deployment",
        )
    print("Bastion VM deployment complete.")
    for message in blockers(config, azure):
        print(message)
    _print_connection(desired, azure)
    print(f"Workstation VNet: {config.vnet_id}")
    print("Use this VNet ID in the infrastructure configuration to enable private platform access.")


def deletion_changes(config: WorkstationConfig, azure: AzureCli) -> list[Change]:
    group = resource_group(config, azure)
    if group is None:
        return []
    planned = []
    stack = _stack(config, config, azure)
    if _owns_group(group):
        _check_group_cleanup(config, azure, stack=stack)
    if stack is not None:
        resources = azure.run(
            "resource",
            "list",
            "--resource-group",
            config.resource_group_name,
            "-o",
            "json",
            parse_json=True,
        )
        if not isinstance(resources, list):
            raise CommandError("Azure returned an invalid resource list")
        if any(
            str(item.get("id", "")).casefold() == config.vnet_id.casefold() for item in resources
        ):
            peerings = azure.run(
                "network",
                "vnet",
                "peering",
                "list",
                "--resource-group",
                config.resource_group_name,
                "--vnet-name",
                f"{config.name_prefix}-vnet",
                "-o",
                "json",
                parse_json=True,
            )
            if not isinstance(peerings, list):
                raise CommandError("Azure returned an invalid peering list")
            if peerings:
                raise CommandError(
                    "Disconnect the Bastion VNet peerings before deleting this workstation."
                )
        planned.append(
            Change(
                "delete",
                f"{config.name_prefix}-vm and its managed resources",
                "retire this workstation after preserving its files",
            )
        )
    if _owns_group(group):
        planned.append(
            Change(
                "delete",
                f"resource group {config.resource_group_name}",
                "remove only the empty Bastion-owned group and its VM login authorization; stop if unrelated resources, roles or locks remain",
            )
        )
    return planned


def delete(config: WorkstationConfig, azure: AzureCli) -> None:
    deletion_changes(config, azure)
    group = resource_group(config, azure)
    if group is None:
        print("Bastion resource group is already absent.")
        return
    if _stack(config, config, azure) is not None:
        azure.run(
            "stack",
            "group",
            "delete",
            "--name",
            f"{config.name_prefix}-vm",
            "--resource-group",
            config.resource_group_name,
            "--action-on-unmanage",
            "deleteResources",
            "--resources-without-delete-support",
            "detach",
            "--yes",
        )
    if not _owns_group(group):
        print("Bastion VM stack resources deleted. The primary resource group was not modified.")
        print("The resource-group VM login authorization remains available for future Bastion VMs.")
        return
    _check_group_cleanup(config, azure)
    azure.run("group", "delete", "--name", config.resource_group_name, "--yes")
    print("Bastion-owned resource group and its VM login authorization deleted.")


def _check_group_cleanup(
    config: WorkstationConfig, azure: AzureCli, *, stack: dict[str, object] | None = None
) -> None:
    resources = azure.run(
        "resource",
        "list",
        "--resource-group",
        config.resource_group_name,
        "-o",
        "json",
        parse_json=True,
    )
    allowed_ids: set[str] = set()
    if stack is not None:
        managed = stack.get("resources")
        if not isinstance(managed, list):
            raise CommandError(
                "Cannot verify the stack resource inventory; no resources were deleted."
            )
        allowed_ids = {str(item["id"]).casefold() for item in managed}
        allowed_ids.add(
            f"{config.resource_group_id}/providers/Microsoft.Resources/deploymentStacks/{config.name_prefix}-vm".casefold()
        )
    locks = azure.run(
        "lock",
        "list",
        "--resource-group",
        config.resource_group_name,
        "-o",
        "json",
        parse_json=True,
    )
    assignments = azure.run(
        "role",
        "assignment",
        "list",
        "--scope",
        config.resource_group_id,
        "-o",
        "json",
        parse_json=True,
    )
    if not all(isinstance(items, list) for items in (resources, locks, assignments)):
        raise CommandError("Cannot verify the Bastion group is empty; the group was retained.")
    unrelated_roles = [
        item
        for item in assignments
        if str(item.get("scope", "")).casefold() == config.resource_group_id.casefold()
        and not (
            str(item.get("principalId", "")).casefold()
            == config.infra_admin_group_object_id.casefold()
            and str(item.get("roleDefinitionId", "")).rsplit("/", 1)[-1].casefold()
            == VM_ADMIN_LOGIN_ROLE_ID
        )
    ]
    unrelated_resources = [
        item
        for item in resources
        if str(item.get("id", "")).casefold() not in allowed_ids
        and str(item.get("managedBy", "")).casefold() not in allowed_ids
    ]
    if unrelated_resources or locks or unrelated_roles:
        raise CommandError(
            "Resources, locks or unrelated roles prevent cleanup. The group was retained; "
            "review them before retrying delete."
        )


def status(config: WorkstationConfig, azure: AzureCli) -> list[Change]:
    return changes(config, azure)
