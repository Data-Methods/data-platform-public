"""The `dp-instance` command line.

    dp-instance [status]                    where the instance stands, planning nothing
    dp-instance setup                       map the instance's repositories
    dp-instance upgrade [--to VERSION]      one run, from the code into the platform
    dp-instance plan [--step NAME]          preview the next change, or one step; starts no run
    dp-instance apply                       a new run on main's code: a configuration change
    dp-instance resume                      carry on from where the active run stopped
    dp-instance verify [--step NAME]        the completion check, or one step's verify
    dp-instance template-access setup|check
    dp-instance secret list|set|reload|rotate
    dp-instance update                      install the newest published dp-instance

Every command takes --json: one JSON object per line on stdout, ending with the result, and
no question asked. Exit codes: 0 done, 1 failed, 2 a usage error, 3 an external handoff,
4 values only a person supplies, 5 a decision, 6 a sign-in to renew, 7 runs a step waits out,
8 a step still pending.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import runs, secrets, setup, status, template_access, update, views
from .outcome import USAGE, Result, Stop
from .screen import Screen
from .session import Options, Session, terminal


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write one JSON object per line to stdout, ending with the result; never ask.",
    )
    automatic = argparse.ArgumentParser(add_help=False)
    automatic.add_argument(
        "--auto-approve",
        action="store_true",
        help="Approve each step's plan, and the restarts of secret commands, without stopping.",
    )
    # A decision's ID, which only a stop names: resume and the secret commands take it.
    approving = argparse.ArgumentParser(add_help=False, parents=[automatic])
    approving.add_argument(
        "--approve",
        action="append",
        default=[],
        metavar="ID",
        help="Approve the decision with this ID, as a review showed it. Repeatable.",
    )
    parser = argparse.ArgumentParser(
        prog="dp-instance",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser("status", parents=[common], help="Show where the instance stands, planning nothing.")
    setup_parser = commands.add_parser("setup", parents=[common], help="Map the instance's repositories.")
    setup_parser.add_argument(
        "--approve", action="append", default=[], metavar="ID", help="Write the map setup showed."
    )
    upgrade = commands.add_parser(
        "upgrade", parents=[common, automatic], help="Upgrade to the next release, or the one --to names."
    )
    target = upgrade.add_mutually_exclusive_group()
    target.add_argument(
        "--to",
        metavar="VERSION",
        help="The published Platform Version, such as 2026.3. By default, the next stop: the next "
        "Platform Version, else a newer fix of the one the instance runs.",
    )
    target.add_argument(
        "--manifest",
        metavar="FILE",
        # Data Methods' own pre-release instance only: an unpublished candidate manifest.
        help=argparse.SUPPRESS,
    )
    preview = commands.add_parser("plan", parents=[common], help="Preview the next change; starts no run.")
    commands.add_parser(
        "apply",
        parents=[common, automatic],
        help="Apply main's configuration: a new run on main's code, once your change is merged.",
    )
    commands.add_parser(
        "resume", parents=[common, approving], help="Carry on from where the active run stopped."
    )
    check = commands.add_parser("verify", parents=[common], help="Run every step's read-only verify.")
    for sub, what in ((preview, "Plan"), (check, "Verify")):
        sub.add_argument(
            "--step",
            metavar="NAME",
            help=f"{what} one step alone, by its full name: repository/component/phase/step.",
        )
    commands.add_parser("update", parents=[common], help="Install the newest published dp-instance.")

    access = commands.add_parser("template-access", parents=[common], help="Set up or check template access.")
    access_commands = access.add_subparsers(dest="access", metavar="ACTION", required=True)
    for name, text, default in (
        (
            "setup",
            "Set up template access: each key, its vault copy and SSH alias.",
            "every template in dp-instance.json whose key the Platform Infrastructure Key Vault holds",
        ),
        ("check", "Check template access.", "every template set up here or mapped"),
    ):
        sub = access_commands.add_parser(name, parents=[common], help=text)
        sub.add_argument("template", nargs="?", help=f"The Template Repository Key; by default, {default}.")
        sub.add_argument(
            "--instance", metavar="KEY", help="The Platform Instance Key; read from the map by default."
        )
        if name == "setup":
            sub.add_argument(
                "--development",
                action="store_true",
                # Data Methods' own pre-release instance only: a named template's key for its
                # development repository. Setup with no template restores every one the vault holds.
                help=argparse.SUPPRESS,
            )

    secret = commands.add_parser("secret", parents=[common], help="List, set, reload or rotate secrets.")
    secret_commands = secret.add_subparsers(dest="secret", metavar="ACTION", required=True)
    listing = secret_commands.add_parser(
        "list", parents=[common], help="Every secret, or one: who supplies it, what reads it, its state."
    )
    listing.add_argument("name", nargs="?", help="One secret; every secret by default.")
    setting = secret_commands.add_parser(
        "set",
        parents=[common, approving],
        help="At a terminal, replace the fields a person supplies of one secret.",
    )
    setting.add_argument("name", help="The secret to set.")
    reload = secret_commands.add_parser(
        "reload", parents=[common, approving], help="Restart the readers that are behind."
    )
    reload.add_argument("name", nargs="?", help="One secret; every secret by default.")
    rotate = secret_commands.add_parser(
        "rotate", parents=[common, approving], help="Rotate a platform-generated secret."
    )
    rotate.add_argument("name", help="The secret to rotate.")
    return parser


def _dispatch(arguments, screen: Screen) -> Result:
    start = Path.cwd()
    if arguments.command == "setup":
        return setup.setup(start, screen, tuple(arguments.approve))
    if arguments.command == "template-access":
        if arguments.access == "setup":
            return template_access.setup(
                screen,
                start,
                arguments.template,
                instance=arguments.instance,
                development=arguments.development,
            )
        return template_access.check(screen, start, arguments.template, instance=arguments.instance)
    if arguments.command == "update":
        return update.update(screen)
    options = Options(
        json=arguments.json,
        auto_approve=getattr(arguments, "auto_approve", False),
        approvals=tuple(getattr(arguments, "approve", ())),
    )
    session = Session(options, start, screen=screen)
    command = arguments.command or "status"
    if command == "status":
        return status.status(session)
    if command == "upgrade":
        return runs.upgrade(session, arguments.to, arguments.manifest)
    if command == "apply":
        return runs.apply(session)
    if command == "resume":
        return runs.resume(session)
    if command == "plan":
        return views.plan(session, arguments.step)
    if command == "verify":
        return views.verify(session, arguments.step)
    if arguments.secret == "list":
        return secrets.list_secrets(session, arguments.name)
    if arguments.secret == "set":
        return secrets.set_secret(session, arguments.name)
    if arguments.secret == "reload":
        return secrets.reload(session, arguments.name)
    return secrets.rotate(session, arguments.name)


def utf8_output() -> None:
    """Write UTF-8 to the output and error streams whatever the console's code page: on Windows a
    pipe or a redirect takes the code page, such as cp1252, which can't encode characters such
    as the arrow in an upgrade's goal, and the command would stop at the first."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    parser = _parser()
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as ended:
        return USAGE if ended.code else 0
    arguments.json = getattr(arguments, "json", False)
    screen = Screen(json_mode=arguments.json, interactive=terminal())
    command = (
        " ".join(
            part
            for part in (
                arguments.command,
                getattr(arguments, "access", None),
                getattr(arguments, "secret", None),
            )
            if part
        )
        or "status"
    )
    try:
        result = _dispatch(arguments, screen)
    except Stop as stop:
        result = Result(command, stop.status, stop.message, stop.next, stop.detail)
        screen.blank()
        screen.note(
            stop.message, {"failed": "bold red", "decision": "bold yellow"}.get(stop.status, "bold orange1")
        )
        if stop.next:
            screen.next_block(stop.next)
    except KeyboardInterrupt:
        result = Result(command, "failed", "Interrupted.")
    # Every command's total, the time it took, and its parts' times.
    result.detail.update(screen.total())
    screen.emit(result.describe())
    return result.code


if __name__ == "__main__":
    sys.exit(main())
