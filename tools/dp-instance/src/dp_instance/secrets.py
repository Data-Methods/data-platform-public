"""`dp-instance secret list|reload|rotate`: secrets day to day, with no plan and no Terraform.

Each component keeps its own secrets through its secrets steps; dp-instance drives each
component's `secret` commands and asks for approvals. `list` shows every secret, what it's
for, who supplies it, what reads it and its state. A supplied secret changes by writing a new
version in Key Vault, by any means; `reload` then checks its fields by name and restarts the
readers that are behind. `rotate` makes a platform-generated secret's new value, sets it where
it's used, writes it to the vault and restarts its readers. Values are never printed.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass

from rich.text import Text

from . import record, targets
from .components import Call, Cli, Log
from .outcome import WAITS, Next, Result, Stop, failed
from .session import Session


@dataclass(frozen=True)
class Holder:
    """A component that may keep secrets, and its CLI."""

    repository: str
    component: str
    cli: Cli


def holders(session: Session, operation: str) -> tuple[list[Holder], record.Record]:
    manifest = targets.running(session.instance, session.releases)
    manifest.check_install(session.own_commit())
    run = record.Record.new(
        record.runs_folder(session.instance.state),
        "secret",
        operation,
        f"secret {operation}",
        manifest_origin=manifest.origin,
    )
    log = Log(run.log_path)
    mapped = session.instance.components
    found = [
        Holder(repository.key, component.key, Cli(component.command, mapped[repository.key].path, log))
        for repository in manifest.order
        if repository.key in mapped
        for component in repository.components
    ]
    return found, run


def _listing(session: Session, found: list[Holder]) -> list[tuple[Holder, dict]]:
    secrets = []
    for holder in found:
        call = holder.cli.run("secret", "list", "--json")
        if call.status == "usage":
            continue  # the component keeps no secrets, so it has no `secret` commands
        if call.status in WAITS:
            raise _waiting(call)
        if call.status != "done":
            raise failed(f"{holder.component} couldn't list its secrets: {call.message}")
        secrets += [(holder, event) for event in call.events if event.get("event") == "secret"]
    return secrets


def _owner(session: Session, found: list[Holder], name: str) -> Holder:
    owners = [holder for holder, event in _listing(session, found) if event.get("name") == name]
    if not owners:
        raise failed(
            f"No component keeps a secret named {name}.",
            Next("dp-instance secret list", "lists every secret"),
        )
    return owners[0]


def list_secrets(session: Session) -> Result:
    screen = session.screen
    found, run = holders(session, "list")
    screen.header("secret list", ("instance", session.instance.key))
    with screen.console.status("Reading every component's secrets") if screen.interactive else nullcontext():
        secrets = _listing(session, found)
    shown = []
    for holder, event in secrets:
        screen.blank()
        screen.labelled("Secret Name:", Text(str(event.get("name")), style="bold"))
        if event.get("description"):
            screen.labelled("Secret Desc:", str(event["description"]))
        screen.labelled("Component Name:", holder.component)
        screen.labelled("Secret Vault:", str(event.get("vault")))
        screen.labelled("Supplied By:", "you" if event.get("supplied_by") == "you" else "the platform")
        screen.labelled("Read By:", ", ".join(event.get("readers") or []) or "nothing restarts for it")
        state = str(event.get("state"))
        screen.labelled("Secret State:", Text(state, style="green" if state == "current" else "yellow"))
        if not event.get("rotatable"):
            screen.labelled("Rotation:", "not rotatable")
        shown.append(
            {"component": holder.component, **{key: value for key, value in event.items() if key != "event"}}
        )
    if not shown:
        screen.blank()
        screen.note("No component keeps secrets.")
    run.complete()
    return Result("secret list", "done", None, detail={"secrets": shown})


def _decisions(session: Session, call: Call, approvals: list[str], command: str) -> None:
    """A component's decision: asked at a terminal, otherwise a stop naming the command."""
    decision = (call.result or {}).get("decision") or {}
    identifier = decision.get("id")
    if not identifier or identifier in approvals:
        raise failed(f"The component stopped for a decision it can't take: {call.message}")
    screen = session.screen
    screen.blank()
    screen.labelled("Decision:", f"{decision.get('summary')} ({identifier})", "bold yellow")
    if session.interactive:
        if screen.choose([("y", "go ahead"), ("n", "stop")], "Approve?") == "y":
            approvals.append(identifier)
            return
        raise Stop("decision", "Not approved; nothing more was done.")
    resume = f"{command} " + " ".join(f"--approve {item}" for item in dict.fromkeys([*approvals, identifier]))
    raise Stop(
        "decision",
        f"{decision.get('summary')}: needs approval.",
        next=(Next(resume, "goes ahead with exactly that"),),
        decision={**decision, "resume": resume},
    )


def _drive(session: Session, holder: Holder, arguments: list[str], command: str) -> Call:
    approvals = list(session.options.approvals)
    while True:
        extra = [part for identifier in dict.fromkeys(approvals) for part in ("--approve", identifier)]
        if session.options.auto_approve:
            extra.append("--auto-approve")
        call = holder.cli.run(*arguments, "--json", *extra)
        if call.status == "decision":
            _decisions(session, call, approvals, command)
            continue
        return call


def _report(session: Session, holder: Holder, call: Call, command: str) -> Result:
    screen = session.screen
    for line in call.stderr:
        if line.strip():
            screen.note(line)
    if call.status == "done":
        return Result(command, "done", f"{holder.component}: done.")
    if call.status in WAITS:
        raise _waiting(call)
    raise failed(f"{holder.component}: {call.message}")


def _waiting(call: Call) -> Stop:
    """A component's stop for a sign-in, values or runs, as the command's own; a sign-in names
    the command that renews it."""
    if call.status != "sign-in":
        return Stop(call.status, call.message)
    command = ((call.result or {}).get("sign_in") or {}).get("command") or "az login"
    return Stop(
        "sign-in",
        call.message,
        sign_in={"command": command},
        next=(Next(command, "renews the sign-in; then run the command again"),),
    )


def reload(session: Session, name: str | None) -> Result:
    found, run = holders(session, "reload")
    session.screen.header("secret reload", ("instance", session.instance.key))
    command = "dp-instance secret reload" + (f" {name}" if name else "")
    chosen = (
        [_owner(session, found, name)]
        if name
        else [holder for holder in found if holder.cli.run("secret", "list", "--json").status != "usage"]
    )
    for holder in chosen:
        session.screen.blank()
        session.screen.labelled("Component Name:", Text(holder.component, style="bold"))
        call = _drive(session, holder, ["secret", "reload", *([name] if name else [])], command)
        _report(session, holder, call, "secret reload")
    run.complete()
    return Result("secret reload", "done", "Every reader that was behind restarted.")


def rotate(session: Session, name: str) -> Result:
    found, run = holders(session, "rotate")
    session.screen.header("secret rotate", ("instance", session.instance.key), ("secret", name))
    holder = _owner(session, found, name)
    session.screen.blank()
    session.screen.labelled("Component Name:", Text(holder.component, style="bold"))
    call = _drive(session, holder, ["secret", "rotate", name], f"dp-instance secret rotate {name}")
    result = _report(session, holder, call, "secret rotate")
    run.complete()
    return result
