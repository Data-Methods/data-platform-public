"""The hold, as `status` reads it: Dagster's run queue held in Integration and Production.

Data Engineering's `hold` step sets each Environment's run queue limit to 0 in the core
Dagster Helm release's instance configuration, the `dagster-instance` ConfigMap, and keeps
the limit it replaces in that ConfigMap's `data-platform.io/held-run-queue-limit`
annotation; its `release` step restores the limit and drops the annotation. Nothing else
records the hold, so `status` reads the annotation from the cluster, read only, with
`kubectl` through the operator's own Azure sign-in. The cluster is reachable only from inside
the instance's network, so elsewhere the hold is unknown, which never fails `status`.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import processes
from .components import Cli, Log
from .instance import INFRASTRUCTURE_COMMAND, Instance
from .outcome import Stop, failed

INSTANCE_CONFIG = "dagster-instance"
HELD = "data-platform.io/held-run-queue-limit"
# The Environments Data Engineering holds, by the key of their Dagster namespace in
# Infrastructure's `kubernetes_addons` output. PR Sandboxes are never held.
ENVIRONMENTS = {"integration": "Integration", "prod": "Production"}
OUTSIDE = "outside the instance's network"


@dataclass(frozen=True)
class Hold:
    """The Environments whose Dagster runs are held, or why that couldn't be read."""

    held: tuple[str, ...] = ()
    unknown: str | None = None

    def describe(self) -> str | None:
        """The line `status` shows, or None when nothing is held."""
        if self.unknown is not None:
            return f"unknown ({self.unknown})"
        if self.held:
            return f"Dagster runs held in {' and '.join(self.held)}"
        return None


def _published(instance: Instance) -> dict:
    """Infrastructure's published outputs, read by the Client Infrastructure Repository's
    own CLI, which needs the operator's Azure sign-in and the instance's network."""
    infrastructure = instance.infrastructure
    if infrastructure is None:
        raise failed("no Client Infrastructure Repository is mapped")
    call = Cli(INFRASTRUCTURE_COMMAND, infrastructure.path, Log(None)).run("outputs", "show")
    if call.status != "done":
        if any("az login" in line for line in call.stderr):
            raise failed("sign in with az login first")
        raise failed(OUTSIDE)
    try:
        exports = json.loads(call.stdout)["exports"]
        return {
            "cluster": exports["kubernetes"]["value"]["cluster"]["id"],
            "namespaces": exports["kubernetes_addons"]["value"]["namespaces"],
        }
    except (ValueError, KeyError, TypeError):
        raise failed("Infrastructure hasn't published its Kubernetes outputs") from None


def _cluster(cluster_id: str) -> tuple[str, str, str]:
    parts = cluster_id.strip("/").split("/")
    if len(parts) != 8 or parts[6].lower() != "managedclusters":
        raise failed("Infrastructure's Kubernetes cluster isn't an AKS cluster ID")
    return parts[1], parts[3], parts[7]


def _held(kubeconfig: Path, namespace: str) -> bool:
    found = processes.run(
        "kubectl",
        "--kubeconfig",
        str(kubeconfig),
        "--request-timeout=15s",
        "get",
        "configmaps",
        "--namespace",
        namespace,
        "--field-selector",
        f"metadata.name={INSTANCE_CONFIG}",
        "--output",
        "json",
        check=False,
    )
    if not found.ok:
        raise failed(OUTSIDE)
    items = json.loads(found.stdout).get("items") or []
    return bool(items) and HELD in (items[0].get("metadata", {}).get("annotations") or {})


def read(instance: Instance) -> Hold:
    """What the hold acts on, read from the cluster; unknown, with the reason, when it can't
    be read."""
    try:
        published = _published(instance)
        subscription, group, name = _cluster(published["cluster"])
        with tempfile.TemporaryDirectory(prefix="dp-instance-kube-") as folder:
            kubeconfig = Path(folder) / "config"
            fetched = processes.run(
                "az",
                "aks",
                "get-credentials",
                "--subscription",
                subscription,
                "--resource-group",
                group,
                "--name",
                name,
                "--file",
                str(kubeconfig),
                "--only-show-errors",
                check=False,
            )
            if not fetched.ok:
                raise failed("sign in with az login first" if "az login" in fetched.stderr else OUTSIDE)
            processes.run(
                "kubelogin", "convert-kubeconfig", "--login", "azurecli", "--kubeconfig", str(kubeconfig)
            )
            held = tuple(
                label
                for key, label in ENVIRONMENTS.items()
                if (namespace := (published["namespaces"].get(f"dagster_{key}") or {}).get("name"))
                and _held(kubeconfig, namespace)
            )
    except Stop as stop:
        return Hold(unknown=stop.message)
    except (ValueError, OSError):
        return Hold(unknown=OUTSIDE)
    return Hold(held)
