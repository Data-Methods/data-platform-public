from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from bastion_vm_linux import config as workstation_config
from bastion_vm_linux import deployment as workstation
from bastion_vm_linux.config import WorkstationConfig


def config_fixture() -> WorkstationConfig:
    return WorkstationConfig(
        schema_version=1,
        platform_instance_key="client-platform",
        tenant_id="00000000-0000-0000-0000-000000000002",
        subscription_id="00000000-0000-0000-0000-000000000003",
        resource_group_name="RG-DataPlatform",
        infra_admin_group_object_id="00000000-0000-0000-0000-000000000001",
        name_prefix="dp-bastion",
        location="westus2",
        vnet_address_prefix="10.253.0.0/24",
        bastion_subnet_prefix="10.253.0.0/26",
        workstation_subnet_prefix="10.253.0.64/27",
    )


class Azure:
    def __init__(
        self,
        *,
        authorized: bool = True,
        deployed: bool = True,
        bastion_available: bool = True,
        provisioning_state: str = "succeeded",
        configuration: WorkstationConfig | None = None,
        group_exists: bool = True,
        owned_group: bool = False,
    ) -> None:
        self.config = configuration or config_fixture()
        self.group_exists = group_exists
        self.owned_group = owned_group
        self.extra_resources = []
        self.locks = []
        self.peerings = []
        self.extra_assignments = []
        self.calls: list[tuple[str, ...]] = []
        self.authorized = authorized
        self.deployed = deployed
        self.bastion_available = bastion_available
        self.provisioning_state = provisioning_state

    def account(self) -> dict[str, str]:
        return {
            "id": "00000000-0000-0000-0000-000000000003",
            "tenantId": "00000000-0000-0000-0000-000000000002",
        }

    def run(self, *args: str, parse_json: bool = False) -> Any:
        self.calls.append(args)
        if args[:2] == ("group", "show"):
            assert parse_json
            return {
                "id": self.config.resource_group_id,
                "location": "westus2",
                "tags": {
                    "data-platform-instance-key": "client-platform",
                    "data-platform-component-key": "bastion-vm"
                    if self.owned_group
                    else "platform-resource-group",
                },
            }
        if args[:2] == ("group", "exists"):
            return "true" if self.group_exists else "false"
        if args[:3] == ("stack", "group", "list"):
            assert parse_json
            return (
                [
                    {
                        "name": "dp-bastion-vm",
                        "resources": [{"id": self.config.vnet_id}],
                        "parameters": {
                            key: {"value": value}
                            for key, value in workstation.parameters(self.config).items()
                        },
                        "provisioningState": self.provisioning_state,
                        "description": (
                            "Data Platform Bastion VM resources; "
                            "data-platform.io/instance=client-platform;component=bastion-vm"
                        ),
                    }
                ]
                if self.deployed
                else []
            )
        if args[:3] == ("role", "assignment", "list"):
            assert parse_json
            if not self.authorized:
                return []
            return [
                *self.extra_assignments,
                {
                    "id": "vm-login-assignment",
                    "principalId": self.config.infra_admin_group_object_id,
                    "scope": self.config.resource_group_id,
                    "roleDefinitionId": (
                        "/subscriptions/00000000-0000-0000-0000-000000000003/providers/"
                        "Microsoft.Authorization/roleDefinitions/"
                        f"{workstation.VM_ADMIN_LOGIN_ROLE_ID}"
                    ),
                },
            ]
        if args[:2] == ("group", "create"):
            self.group_exists = True
            self.owned_group = True
            return ""
        if args[:2] == ("group", "delete"):
            self.group_exists = False
            return ""
        if args[:2] == ("resource", "list"):
            return self.extra_resources + ([{"id": self.config.vnet_id}] if self.deployed else [])
        if args[:2] == ("lock", "list"):
            return self.locks
        if args[:4] == ("network", "vnet", "peering", "list"):
            return self.peerings
        if args[:3] == ("role", "assignment", "create"):
            self.authorized = True
            return {"id": "vm-login-assignment"}
        if args[:2] == ("vm", "show"):
            return (
                "/subscriptions/test/resourceGroups/test/providers/"
                "Microsoft.Compute/virtualMachines/dp-bastion-vm"
                if self.deployed
                else ""
            )
        if args[:3] == ("network", "bastion", "show"):
            return (
                "/subscriptions/test/resourceGroups/test/providers/"
                "Microsoft.Network/bastionHosts/dp-bastion"
                if self.deployed and self.bastion_available
                else ""
            )
        if args[:3] == ("stack", "group", "create"):
            self.deployed = True
            return ""
        if args[:3] == ("stack", "group", "delete"):
            self.deployed = False
            return ""
        raise AssertionError(args)

    def run_with_heartbeat(
        self,
        *args: str,
        parse_json: bool = False,
        heartbeat_label: str,
        heartbeat_seconds: int = 30,
    ) -> Any:
        assert heartbeat_label == "Bastion VM deployment"
        assert heartbeat_seconds == 30
        return self.run(*args, parse_json=parse_json)


def test_configure_defaults_to_explicit_workstation_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    azure = Azure()
    monkeypatch.setattr(workstation_config, "AzureCli", lambda **kwargs: azure)
    monkeypatch.setattr(workstation_config.sys.stdin, "isatty", lambda: True)
    answers = iter(
        [
            "client-platform",
            "",
            "",
            "",
            "00000000-0000-0000-0000-000000000001",
            "",
            "",
            "",
            "",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    path = tmp_path / "bastion-vm.json"

    workstation_config.configure(path)

    assert WorkstationConfig.load(path).name_prefix == "dp-bastion"
    assert WorkstationConfig.load(path).resource_group_name == "RG-DataPlatform-Bastion"
    assert not azure.calls


def test_template_is_the_bicep_source_beside_the_package() -> None:
    assert workstation.TEMPLATE.is_file()
    assert workstation.TEMPLATE.name == "main.bicep"


@pytest.mark.parametrize("state", ["succeeded", "Succeeded"])
def test_succeeded_stack_is_noop(state: str) -> None:
    azure = Azure(provisioning_state=state)
    assert workstation.changes(config_fixture(), azure) == []  # type: ignore[arg-type]


@pytest.mark.parametrize("state", ["failed", "creating", ""])
def test_incomplete_stack_requires_apply(state: str) -> None:
    azure = Azure(provisioning_state=state)
    assert len(workstation.changes(config_fixture(), azure)) == 1  # type: ignore[arg-type]


def test_apply_uses_python_azure_client_without_host_shell(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    @contextmanager
    def temporary_key() -> Iterator[str]:
        yield "ssh-ed25519 public"

    monkeypatch.setattr(workstation, "_temporary_ssh_public_key", temporary_key)
    azure = Azure()

    workstation.apply(config_fixture(), azure)  # type: ignore[arg-type]

    create = next(call for call in azure.calls if call[:3] == ("stack", "group", "create"))
    assert str(workstation.TEMPLATE) in create
    assert "location=westus2" in create
    assert f"vmSize={workstation.WORKSTATION_SIZE}" in create
    assert all(not value.startswith("operatorObjectId=") for value in create)
    assert all(not value.startswith("operatorPrincipalType=") for value in create)
    assert "adminSshPublicKey=ssh-ed25519 public" in create
    assert not any(call[:3] == ("ad", "signed-in-user", "show") for call in azure.calls)
    description = create[create.index("--description") + 1]
    assert "data-platform.io/instance=client-platform;component=bastion-vm" in description
    output = capsys.readouterr().out
    assert "Bastion VM deployment complete." in output
    assert f"VM SKU:                 {workstation.WORKSTATION_SIZE}" in output
    assert "--auth-type AAD" in output
    assert "AZURE_CONFIG_DIR" in output
    select_subscription = f"az account set --subscription {config_fixture().subscription_id}"
    assert output.index(select_subscription) < output.index("az network bastion ssh")
    ssh_command = next(line for line in output.splitlines() if "az network bastion ssh" in line)
    assert f"--subscription {config_fixture().subscription_id}" in ssh_command
    assert all(call[:2] != ("account", "set") for call in azure.calls)
    assert "https://portal.azure.com/#resource" in output
    assert "Recovery connection" not in output
    assert "--ssh-key" not in output
    assert config_fixture().vnet_id in output


def test_connection_always_prints_cli_command_and_browser_alternative(
    capsys: pytest.CaptureFixture[str],
) -> None:
    azure = Azure()

    workstation.connection(config_fixture(), azure)  # type: ignore[arg-type]

    output = capsys.readouterr().out
    assert "Connect from a local shell" in output
    assert "az network bastion ssh" in output
    assert "--auth-type AAD" in output
    assert "https://portal.azure.com/#resource" in output
    assert "not recommended for deployment work" in output
    assert all(
        call[:3] not in {("stack", "group", "create"), ("stack", "group", "delete")}
        for call in azure.calls
    )


def test_connection_fails_cleanly_when_workstation_is_not_deployed() -> None:
    azure = Azure(deployed=False)

    with pytest.raises(workstation.CommandError, match="Bastion VM is not deployed"):
        workstation.connection(config_fixture(), azure)  # type: ignore[arg-type]

    assert all(
        call[:3] not in {("stack", "group", "create"), ("stack", "group", "delete")}
        for call in azure.calls
    )


def test_connection_fails_cleanly_when_bastion_is_missing() -> None:
    azure = Azure(bastion_available=False)

    with pytest.raises(workstation.CommandError, match="Azure Bastion dp-bastion was not found"):
        workstation.connection(config_fixture(), azure)  # type: ignore[arg-type]

    assert all(
        call[:3] not in {("stack", "group", "create"), ("stack", "group", "delete")}
        for call in azure.calls
    )


def test_temporary_provisioning_key_is_deleted_after_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary_directory = workstation.tempfile.TemporaryDirectory
    monkeypatch.setattr(
        workstation.tempfile,
        "TemporaryDirectory",
        lambda **kwargs: temporary_directory(dir=tmp_path, **kwargs),
    )

    with workstation._temporary_ssh_public_key() as public_key:
        assert public_key.startswith("ssh-ed25519 ")
        assert any(tmp_path.iterdir())

    assert list(tmp_path.iterdir()) == []


def test_authorize_assigns_vm_login_to_infra_admin_group_at_resource_group_scope() -> None:
    azure = Azure(authorized=False)

    assert len(workstation.authorization_changes(config_fixture(), azure)) == 1  # type: ignore[arg-type]
    workstation.authorize(config_fixture(), azure)  # type: ignore[arg-type]

    create = next(call for call in azure.calls if call[:3] == ("role", "assignment", "create"))
    assert "--assignee-object-id" in create
    assert "00000000-0000-0000-0000-000000000001" in create
    assert "--assignee-principal-type" in create
    assert "Group" in create
    assert workstation.VM_ADMIN_LOGIN_ROLE_ID in create
    assert (
        "/subscriptions/00000000-0000-0000-0000-000000000003/resourceGroups/RG-DataPlatform"
        in create
    )


def test_authorize_is_idempotent() -> None:
    azure = Azure(authorized=True)

    assert workstation.authorization_changes(config_fixture(), azure) == []  # type: ignore[arg-type]
    workstation.authorize(config_fixture(), azure)  # type: ignore[arg-type]

    assert not any(call[:3] == ("role", "assignment", "create") for call in azure.calls)


def test_missing_login_is_reported_without_blocking_deployment() -> None:
    azure = Azure(authorized=False)

    assert workstation.blockers(config_fixture(), azure) == [  # type: ignore[arg-type]
        "Run 'bastion-vm-linux.ps1 authorize' after apply to grant the Infra Admin Group VM login."
    ]


def test_delete_in_primary_group_removes_only_the_stack(
    capsys: pytest.CaptureFixture[str],
) -> None:
    azure = Azure()

    workstation.delete(config_fixture(), azure)  # type: ignore[arg-type]

    assert [call for call in azure.calls if call[:3] == ("stack", "group", "delete")] == [
        (
            "stack",
            "group",
            "delete",
            "--name",
            "dp-bastion-vm",
            "--resource-group",
            "RG-DataPlatform",
            "--action-on-unmanage",
            "deleteResources",
            "--resources-without-delete-support",
            "detach",
            "--yes",
        )
    ]
    output = capsys.readouterr().out
    assert "primary resource group was not modified" in output
    assert "resource-group VM login authorization remains" in output


def dedicated_config():
    return replace(config_fixture(), resource_group_name="RG-DataPlatform-Bastion")


def test_new_group_and_stack_are_explicit_in_read_only_plan():
    azure = Azure(
        configuration=dedicated_config(), group_exists=False, deployed=False, authorized=False
    )
    plan = workstation.changes(dedicated_config(), azure)
    assert len(plan) == 2
    assert plan[0].action == "create"
    assert all(call[:2] not in {("group", "create"), ("group", "delete")} for call in azure.calls)
    assert not azure.deployed


def test_authorize_never_creates_a_missing_group():
    azure = Azure(configuration=dedicated_config(), group_exists=False, deployed=False)
    with pytest.raises(workstation.CommandError, match="Run apply"):
        workstation.authorize(dedicated_config(), azure)
    assert not azure.group_exists


def test_new_stack_refuses_the_primary_group():
    azure = Azure(deployed=False)
    with pytest.raises(workstation.CommandError, match="dedicated"):
        workstation.changes(config_fixture(), azure)
    assert not azure.deployed


def test_delete_dedicated_group_and_repeat_is_noop():
    azure = Azure(configuration=dedicated_config(), owned_group=True)
    workstation.delete(dedicated_config(), azure)
    assert not azure.group_exists
    assert not azure.deployed
    assert workstation.deletion_changes(dedicated_config(), azure) == []
    workstation.delete(dedicated_config(), azure)
    assert len([call for call in azure.calls if call[:2] == ("group", "delete")]) == 1


@pytest.mark.parametrize("remaining", ["resource", "lock", "role"])
def test_partial_delete_retains_group_with_unrelated_objects_and_can_resume(remaining):
    azure = Azure(configuration=dedicated_config(), owned_group=True, deployed=False)
    if remaining == "resource":
        azure.extra_resources = [{"id": "unrelated"}]
    elif remaining == "lock":
        azure.locks = [{"id": "lock"}]
    else:
        azure.extra_assignments = [
            {
                "scope": dedicated_config().resource_group_id,
                "roleDefinitionId": "unrelated",
                "principalId": "other",
            }
        ]
    with pytest.raises(workstation.CommandError, match="group was retained"):
        workstation.delete(dedicated_config(), azure)
    assert not azure.deployed
    assert azure.group_exists
    azure.extra_resources = []
    azure.locks = []
    azure.extra_assignments = []
    workstation.delete(dedicated_config(), azure)
    assert not azure.group_exists


def test_delete_requires_bridge_disconnection_before_stack_mutation():
    azure = Azure(configuration=dedicated_config(), owned_group=True)
    azure.peerings = [{"name": "platform"}]
    with pytest.raises(workstation.CommandError, match="Disconnect"):
        workstation.delete(dedicated_config(), azure)
    assert azure.deployed
    assert azure.group_exists


@pytest.mark.parametrize("remaining", ["resource", "lock", "role"])
def test_delete_checks_foreign_objects_before_stack_mutation(remaining):
    azure = Azure(configuration=dedicated_config(), owned_group=True)
    if remaining == "resource":
        azure.extra_resources = [{"id": "unrelated"}]
    elif remaining == "lock":
        azure.locks = [{"id": "lock"}]
    else:
        azure.extra_assignments = [
            {
                "scope": dedicated_config().resource_group_id,
                "roleDefinitionId": "unrelated",
                "principalId": "other",
            }
        ]
    with pytest.raises(workstation.CommandError, match="group was retained"):
        workstation.delete(dedicated_config(), azure)
    assert azure.deployed
    assert not any(call[:3] == ("stack", "group", "delete") for call in azure.calls)


def test_delete_rechecks_for_resources_created_during_stack_removal():
    class ConcurrentResource(Azure):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            if args[:3] == ("stack", "group", "delete"):
                self.extra_resources = [{"id": "new-unrelated-resource"}]
            return result

    azure = ConcurrentResource(configuration=dedicated_config(), owned_group=True)
    with pytest.raises(workstation.CommandError, match="group was retained"):
        workstation.delete(dedicated_config(), azure)
    assert not azure.deployed
    assert azure.group_exists


def test_group_lookup_does_not_treat_access_failure_as_absence():
    class Forbidden(Azure):
        def run(self, *args, **kwargs):
            raise workstation.CommandError("AuthorizationFailed")

    with pytest.raises(workstation.CommandError, match="AuthorizationFailed"):
        workstation.changes(config_fixture(), Forbidden())


@pytest.mark.parametrize("marker", [None, "iot"])
def test_existing_stack_cannot_use_an_unmarked_or_unrelated_component_group(marker):
    class OtherGroup(Azure):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            if args[:2] == ("group", "show"):
                result["tags"]["data-platform-component-key"] = marker
            return result

    azure = OtherGroup()
    with pytest.raises(workstation.CommandError, match="not marked"):
        workstation.delete(config_fixture(), azure)
    assert azure.deployed


def test_initial_apply_does_not_require_login_assignment(monkeypatch):
    config = dedicated_config()
    azure = Azure(configuration=config, group_exists=False, deployed=False, authorized=False)

    @contextmanager
    def key():
        yield "ssh-ed25519 public"

    monkeypatch.setattr(workstation, "_temporary_ssh_public_key", key)
    workstation.apply(config, azure)
    assert azure.group_exists and azure.deployed
    assert workstation.changes(config, azure) == []
    assert not azure.authorized
    workstation.authorize(config, azure)
    assert not workstation.blockers(config, azure)
