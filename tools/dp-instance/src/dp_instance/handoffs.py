"""Handoff pages: one per external system, grouping every component's work there.

A handoff is work in a system the platform doesn't administer, done by whoever holds the
rights there, the operator included. Each component supplies the text of its own actions;
dp-instance groups them by system, adds who asked from the operator's own Azure sign-in, and
writes `handoffs/<system>.md` in the run's record. A page never holds a credential or a
contact name.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from . import processes
from .outcome import Stop

SYSTEMS = {
    "azure": "Azure",
    "azure-devops": "Azure DevOps",
    "dns": "DNS",
    "entra": "Microsoft Entra",
    "exchange": "Exchange",
    "github": "GitHub",
    "jira": "Jira",
    "network": "The client's network",
    "snowflake": "Snowflake",
    "teams": "Microsoft Teams",
}


def system_name(system: str) -> str:
    return SYSTEMS.get(system, system)


def asker() -> str:
    """Who asked: the operator's own Azure sign-in, when there is one."""
    try:
        found = processes.run(
            "az",
            "account",
            "show",
            "--query",
            "user.name",
            "--output",
            "tsv",
            "--only-show-errors",
            check=False,
        )
    except Stop:
        return "the operator running dp-instance"
    name = found.stdout.strip() if found.ok else ""
    return name or "the operator running dp-instance"


def _section(item: dict) -> list[str]:
    if item.get("required"):
        need = "**Required.** The platform needs this, so the run waits for it."
    else:
        need = "**Optional.** Nothing waits for this."
    lines = [
        f"## {item.get('title', item.get('id', 'Outside work'))}",
        "",
        need,
        "",
        f"**What and why.** {item.get('why', '')}",
        "",
    ]
    if item.get("by_when"):
        lines += [f"**By when.** {item['by_when']}", ""]
    values = item.get("values") or {}
    if values:
        lines += ["**The exact values.**", "", "| Value | |", "| --- | --- |"]
        lines += [f"| {label} | `{value}` |" for label, value in values.items()]
        lines.append("")
    lines += [f"**Rights needed.** {item.get('rights', '')}", ""]
    if item.get("steps"):
        lines += ["**Steps.**", ""]
        lines += [f"{number}. {step}" for number, step in enumerate(item["steps"], 1)]
        lines.append("")
    if item.get("commands"):
        lines += ["**Commands.**", "", "```", *item["commands"], "```", ""]
    lines += [
        f"**What restarts.** {item.get('restarts') or 'Nothing restarts.'}",
        "",
        f"**How to check it worked.** {item.get('check', '')}",
        "",
        f"From `{item.get('component', '')}`'s step `{item.get('step', '')}` (`{item.get('id', '')}`).",
        "",
    ]
    return lines


def write_pages(folder: Path, sections: list[dict], instance: str, asked_by: str) -> dict[str, Path]:
    """One page per system; returns each system's page. Required work comes first."""
    by_system: dict[str, dict[str, dict]] = {}
    for item in sections:
        by_system.setdefault(item.get("system", "other"), {})[item.get("id", str(len(by_system)))] = item
    written = {}
    for system, items in sorted(by_system.items()):
        ordered = sorted(items.values(), key=lambda item: (not item.get("required"), item.get("id", "")))
        lines = [
            f"# {system_name(system)}: outside work for {instance}",
            "",
            f"Asked by {asked_by} on {date.today().isoformat()}, for the Platform Instance "
            f"`{instance}`. Each section is one piece of work in {system_name(system)}; "
            "dp-instance observes when it's done, whoever does it.",
            "",
        ]
        for item in ordered:
            lines += _section(item)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{system}.md"
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        written[system] = path
    return written
