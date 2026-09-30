from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from bastion_vm_linux import __main__ as cli
from bastion_vm_linux import config as config_module
from bastion_vm_linux.azure import AzureCli, CommandError
from bastion_vm_linux.changes import Change
from bastion_vm_linux.config import WorkstationConfig

TOOL = Path(__file__).resolve().parents[2] / "tools" / "bastion-vm-linux"


@pytest.fixture
def config() -> WorkstationConfig:
    return WorkstationConfig.load(TOOL / "bastion-vm.example.json")


@pytest.mark.parametrize(
    "updates",
    [
        {"extra": True},
        {"schema_version": True},
        {"schema_version": 2},
        {"name_prefix": "../../old"},
        {"subscription_id": "invalid"},
        {"bastion_subnet_prefix": "10.253.0.0/27"},
        {"workstation_subnet_prefix": "10.253.0.32/27"},
        {"workstation_subnet_prefix": "10.252.0.64/27"},
    ],
)
def test_rejects_invalid_config_before_azure(config, updates, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**asdict(config), **updates}))
    with pytest.raises(CommandError):
        WorkstationConfig.load(path)


def test_config_save_roundtrip_leaves_no_temporary_file(config, tmp_path):
    path = tmp_path / "bastion-vm.json"
    config.save(path)
    assert WorkstationConfig.load(path) == config
    assert list(tmp_path.glob(".*.tmp")) == []


def test_azure_cli_pins_subscription_without_changing_shared_default(monkeypatch, config):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr("bastion_vm_linux.azure.subprocess.run", run)
    monkeypatch.setattr("bastion_vm_linux.azure.azure_cli_command", lambda *args: ["az", *args])
    AzureCli(subscription=config.subscription_id).account()
    assert calls == [
        ["az", "account", "show", "--output", "json", "--subscription", config.subscription_id]
    ]


def test_context_refuses_wrong_tenant(config):
    class Azure:
        def account(self):
            return {"tenantId": "another-tenant", "id": config.subscription_id}

    with pytest.raises(CommandError, match="Sign in to tenant"):
        config_module.validate_context(config, Azure())


def test_configure_is_idempotent_and_offline_for_existing_file(config, tmp_path, monkeypatch):
    path = tmp_path / "bastion-vm.json"
    config.save(path)
    before = path.read_bytes()
    monkeypatch.setattr(config_module, "AzureCli", lambda: pytest.fail("unexpected Azure request"))
    config_module.configure(path)
    assert path.read_bytes() == before


def test_reconfigure_refuses_replacement_identity(config, tmp_path, monkeypatch):
    path = tmp_path / "bastion-vm.json"
    config.save(path)
    before = path.read_bytes()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    class Azure:
        def account(self):
            return {"tenantId": config.tenant_id, "id": config.subscription_id}

    monkeypatch.setattr(config_module, "AzureCli", Azure)
    monkeypatch.setattr(
        "builtins.input", lambda prompt: "replacement" if prompt.startswith("Name prefix") else ""
    )
    with pytest.raises(CommandError, match="separate --file"):
        config_module.configure(path, reconfigure=True)
    assert path.read_bytes() == before


@pytest.mark.parametrize("action", ["apply", "authorize", "delete"])
def test_dry_run_never_mutates(config, tmp_path, monkeypatch, action):
    path = tmp_path / "config.json"
    config.save(path)
    before = path.read_bytes()
    monkeypatch.setattr(cli, "AzureCli", lambda **kwargs: object())
    monkeypatch.setattr(cli, "validate_context", lambda *_: None)
    for name in ("changes", "authorization_changes", "deletion_changes"):
        monkeypatch.setattr(cli.deployment, name, lambda *_: [Change("create", "target", "reason")])
    monkeypatch.setattr(cli.deployment, "blockers", lambda *_: [])
    monkeypatch.setattr(cli.deployment, "_stack", lambda *_: {"name": "stack"})
    monkeypatch.setattr(cli.deployment, action, lambda *_: pytest.fail("unexpected mutation"))
    assert cli.main([action, "--file", str(path), "--dry-run"]) == 0
    assert path.read_bytes() == before


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 is not installed")
def test_entry_script_runs_from_a_copy_of_the_tool_folder(tmp_path):
    copy = tmp_path / "bastion-vm-linux"
    shutil.copytree(TOOL, copy, ignore=shutil.ignore_patterns("__pycache__"))
    environment = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(copy / "bastion-vm-linux.ps1"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "usage: bastion-vm-linux.ps1" in result.stdout
    for action in ("configure", "apply", "authorize", "status", "connection", "delete"):
        assert action in result.stdout
