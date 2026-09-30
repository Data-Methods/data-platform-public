from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass, fields
from ipaddress import IPv4Network
from pathlib import Path
from uuid import UUID

from .azure import AzureCli, CommandError

DEFAULT_FILE = Path("bastion-vm.json")


@dataclass(frozen=True)
class WorkstationConfig:
    schema_version: int
    platform_instance_key: str
    tenant_id: str
    subscription_id: str
    resource_group_name: str
    infra_admin_group_object_id: str
    name_prefix: str
    location: str
    vnet_address_prefix: str
    bastion_subnet_prefix: str
    workstation_subnet_prefix: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        for field in fields(self):
            if field.name != "schema_version":
                value = getattr(self, field.name)
                if not isinstance(value, str) or not value.strip() or value != value.strip():
                    raise ValueError(
                        f"{field.name} must be nonempty text without surrounding spaces"
                    )
        for name in ("tenant_id", "subscription_id", "infra_admin_group_object_id"):
            UUID(getattr(self, name))
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,16}[a-z0-9]", self.name_prefix):
            raise ValueError("name_prefix must be 3-18 lowercase letters, digits or hyphens")
        if not re.fullmatch(r"[a-z0-9-]{1,64}", self.platform_instance_key):
            raise ValueError(
                "platform_instance_key must contain lowercase letters, digits or hyphens"
            )
        if not re.fullmatch(r"[a-z0-9]+", self.location):
            raise ValueError("location must be an Azure region name, such as southcentralus")
        if "/" in self.resource_group_name or len(self.resource_group_name) > 90:
            raise ValueError("resource_group_name must be an Azure resource group name")
        vnet = IPv4Network(self.vnet_address_prefix)
        bastion = IPv4Network(self.bastion_subnet_prefix)
        workstation = IPv4Network(self.workstation_subnet_prefix)
        if not bastion.subnet_of(vnet) or not workstation.subnet_of(vnet):
            raise ValueError("both subnets must be within vnet_address_prefix")
        if bastion.overlaps(workstation):
            raise ValueError("Bastion and workstation subnets must not overlap")
        if bastion.prefixlen > 26 or workstation.prefixlen > 29:
            raise ValueError(
                "Bastion subnet must be /26 or larger; workstation subnet /29 or larger"
            )

    @property
    def resource_group_id(self) -> str:
        return f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group_name}"

    @property
    def vnet_id(self) -> str:
        return f"{self.resource_group_id}/providers/Microsoft.Network/virtualNetworks/{self.name_prefix}-vnet"

    @property
    def marker(self) -> str:
        return f"data-platform.io/instance={self.platform_instance_key};component=bastion-vm"

    @classmethod
    def load(cls, path: Path) -> WorkstationConfig:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("configuration must be a JSON object")
            return cls(**data)
        except (OSError, ValueError, TypeError) as exc:
            raise CommandError(f"Invalid workstation configuration {path}: {exc}") from exc

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def validate_context(config: WorkstationConfig, azure: AzureCli) -> None:
    account = azure.account()
    if str(account.get("tenantId", "")).casefold() != config.tenant_id.casefold():
        raise CommandError(f"Sign in to tenant {config.tenant_id} before using this configuration")
    if str(account.get("id", "")).casefold() != config.subscription_id.casefold():
        raise CommandError("Azure CLI returned a different subscription than the configuration")


def configure(path: Path, *, reconfigure: bool = False) -> None:
    if path.exists() and not reconfigure:
        config = WorkstationConfig.load(path)
        print(f"Configuration: {path}")
        print(f"Workstation VNet: {config.vnet_id}")
        print("Run reconfigure to change the recorded settings. No Azure changes were made.")
        return
    if not sys.stdin.isatty():
        raise CommandError(
            "configure requires an interactive terminal; use an explicit JSON configuration for automation"
        )
    current = asdict(WorkstationConfig.load(path)) if path.exists() else {}
    account = AzureCli().account()
    defaults = {
        "schema_version": 1,
        "platform_instance_key": "",
        "tenant_id": account["tenantId"],
        "subscription_id": account["id"],
        "resource_group_name": "RG-DataPlatform-Bastion",
        "infra_admin_group_object_id": "",
        "name_prefix": "dp-bastion",
        "location": "southcentralus",
        "vnet_address_prefix": "10.253.0.0/24",
        "bastion_subnet_prefix": "10.253.0.0/26",
        "workstation_subnet_prefix": "10.253.0.64/27",
    }
    desired = {"schema_version": 1}
    for name, default in defaults.items():
        if name == "schema_version":
            continue
        value = current.get(name, default)
        answer = input(f"{name.replace('_', ' ').capitalize()} [{value}]: ").strip()
        desired[name] = answer or value
    config = WorkstationConfig(**desired)
    if current:
        identity = ("tenant_id", "subscription_id", "resource_group_name", "name_prefix")
        if any(current[name] != desired[name] for name in identity):
            raise CommandError(
                "Use a separate --file for a replacement workstation. "
                "Keep the existing configuration until its stack has been retired."
            )
    validate_context(config, AzureCli(subscription=config.subscription_id))
    config.save(path)
    print(f"Saved {path}. No Azure changes were made. Review apply --dry-run next.")
