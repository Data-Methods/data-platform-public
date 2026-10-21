"""The `dp-instance` command line.

    dp-instance [status]                    where the instance stands, planning nothing
    dp-instance setup                       map the instance's repositories
    dp-instance upgrade --to VERSION        one run, from the code into the platform
    dp-instance plan                        preview the next change; starts no run
    dp-instance apply                       a new run on the checked-out code
    dp-instance resume                      carry on from where the active run stopped
    dp-instance verify                      the completion check
    dp-instance template-access setup|check
    dp-instance secret list|reload|rotate

Every command takes --json: one JSON object per line on stdout, ending with the result, and
no question asked. Exit codes: 0 done, 1 failed, 2 a usage error, 3 an external handoff,
4 values only a person supplies, 5 a decision, 6 a sign-in to renew, 7 runs a step waits out.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import runs, secrets, setup, status, template_access, views
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
    approving = argparse.ArgumentParser(add_help=False)
    approving.add_argument(
        "--approve",
        action="append",
        default=[],
        metavar="ID",
        help="Approve the decision with this ID, as a review showed it. Repeatable.",
    )
    approving.add_argument(
        "--auto-approve",
        action="store_true",
        help="Approve each step's plan, and the restarts of secret commands, without stopping.",
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
        "upgrade", parents=[common, approving], help="Upgrade to a Platform Version."
    )
    target = upgrade.add_mutually_exclusive_group(required=True)
    target.add_argument("--to", metavar="VERSION", help="The published Platform Version, such as 2026.3.")
    target.add_argument(
        "--manifest",
        metavar="FILE",
        # Data Methods' own pre-release instance only: an unpublished candidate manifest.
        help=argparse.SUPPRESS,
    )
    commands.add_parser("plan", parents=[common], help="Preview the next change; starts no run.")
    apply = commands.add_parser(
        "apply", parents=[common, approving], help="Start a new run on the checked-out code."
    )
    apply.add_argument(
        "--change",
        metavar="TEXT",
        help='Name the change, such as "Raise the node count": it titles the pull requests.',
    )
    commands.add_parser(
        "resume", parents=[common, approving], help="Carry on from where the active run stopped."
    )
    commands.add_parser("verify", parents=[common], help="Run every step's read-only verify.")

    access = commands.add_parser("template-access", parents=[common], help="Set up or check template access.")
    access_commands = access.add_subparsers(dest="access", metavar="ACTION", required=True)
    for name, text in (
        ("setup", "Set up one template's key, vault copy and SSH alias."),
        ("check", "Check template access."),
    ):
        sub = access_commands.add_parser(name, parents=[common], help=text)
        sub.add_argument(
            "template", nargs=None if name == "setup" else "?", help="The Template Repository Key."
        )
        sub.add_argument(
            "--instance", metavar="KEY", help="The Platform Instance Key; read from the map by default."
        )
        sub.add_argument(
            "--key-vault",
            metavar="NAME",
            help="The Platform Infrastructure Key Vault, once it exists: infrastructure_key_vault.name in "
            "the Client Infrastructure Repository's config/infrastructure/platform_foundation.json.",
        )
        sub.add_argument(
            "--development",
            action="store_true",
            # Data Methods' own pre-release instance only: the key for the development repository.
            help=argparse.SUPPRESS,
        )
        if name == "setup":
            sub.add_argument(
                "--migrate-legacy-infrastructure-key",
                action="store_true",
                help="Adopt the older Infrastructure key, without rotating it.",
            )

    secret = commands.add_parser("secret", parents=[common], help="List, reload or rotate secrets.")
    secret_commands = secret.add_subparsers(dest="secret", metavar="ACTION", required=True)
    secret_commands.add_parser(
        "list", parents=[common], help="Every secret, who supplies it, what reads it, its state."
    )
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
                key_vault=arguments.key_vault,
                development=arguments.development,
                migrate_legacy=arguments.migrate_legacy_infrastructure_key,
            )
        return template_access.check(
            screen,
            start,
            arguments.template,
            instance=arguments.instance,
            key_vault=arguments.key_vault,
            development=arguments.development,
        )
    options = Options(
        json=arguments.json,
        auto_approve=getattr(arguments, "auto_approve", False),
        approvals=tuple(getattr(arguments, "approve", ())),
        change=getattr(arguments, "change", None),
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
        return views.plan(session)
    if command == "verify":
        return views.verify(session)
    if arguments.secret == "list":
        return secrets.list_secrets(session)
    if arguments.secret == "reload":
        return secrets.reload(session, arguments.name)
    return secrets.rotate(session, arguments.name)


def main(argv: list[str] | None = None) -> int:
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
    screen.emit(result.describe())
    return result.code


if __name__ == "__main__":
    sys.exit(main())
