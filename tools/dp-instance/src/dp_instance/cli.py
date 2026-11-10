"""The `dp-instance` command line.

    dp-instance [status]                    where the instance stands, planning nothing
    dp-instance setup                       map the instance's repositories
    dp-instance preflight [--to VERSION]    each component's own checks of this workstation
    dp-instance prepare [--to VERSION]      turn your edits, or a Platform Version, into pull requests
    dp-instance plan [--step NAME]          plan main, the open change and your branch; starts no run
    dp-instance apply                       bring the platform to main, once its pull requests merge
    dp-instance resume                      carry on the instance's stopped run, a prepare or an apply
    dp-instance verify [--step NAME]        the completion check, or one step's verify
    dp-instance unlock RUN                  clear the run lock the named run holds
    dp-instance template-access setup|check
    dp-instance secret list|set|reload|rotate
    dp-instance self update [--to VERSION]  install the newest published dp-instance, or VERSION
    dp-instance --version                   this dp-instance's version

prepare, apply, resume, plan, verify and the secret commands hold the instance's run lock while
they run, and refuse while another command holds it; status holds it only while it reads
main's copies, and preflight and unlock hold none. Neither prepare, plan nor apply runs a
preflight: preflight runs them. An upgrade is `prepare --to`, the merge of its pull requests,
`apply`, each Adoption PR's merge and release, then `verify`.

Every command takes --json: one JSON object per line on stdout, ending with the result, and
no question asked. Exit codes: 0 done, 1 failed, 2 a usage error, 3 an external handoff,
4 values only a person supplies, 5 a decision, 6 a sign-in to renew, 7 runs a step waits out,
8 a step still pending.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import installed, lock, runs, secrets, self_update, setup, status, template_access, views
from .outcome import USAGE, Result, Stop, naming_preflight
from .screen import Screen
from .session import Options, Session, ready_for_template_access, terminal


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
        help=(
            "Approve each step's change that destroys nothing without stopping, with a preview or without "
            "one, as after prepare --no-preview, and the restarts of secret commands. A destroy or "
            "replacement, or a change that doesn't say whether it destroys, always asks: approve it by "
            "its ID with --approve. On code older than Platform 2026.8.0, the component's engine still "
            "approves destroys."
        ),
    )
    # A decision's ID, which only a stop names: resume and the secret commands take it, prepare for
    # the upgrade's restore and remove questions, and prepare and apply for their question about
    # replacing a stopped run.
    deciding = argparse.ArgumentParser(add_help=False)
    deciding.add_argument(
        "--approve",
        action="append",
        default=[],
        metavar="ID",
        help="Approve the decision with this ID, as a review showed it. Repeatable.",
    )
    # Prepare takes no --auto-approve: its code steps approve what they can on their own, and its
    # renders take only what a person approved.
    approving = argparse.ArgumentParser(add_help=False, parents=[automatic, deciding])
    parser = argparse.ArgumentParser(
        prog="dp-instance",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"dp-instance {installed.version() or '(a local copy, not a published version)'}",
        help="Show this dp-instance's version.",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser("status", parents=[common], help="Show where the instance stands, planning nothing.")
    setup_parser = commands.add_parser("setup", parents=[common], help="Map the instance's repositories.")
    setup_parser.add_argument(
        "--approve", action="append", default=[], metavar="ID", help="Write the map setup showed."
    )
    checking = commands.add_parser(
        "preflight",
        parents=[common],
        help=(
            "Run each component's own checks of this workstation, from main's code or with --to a "
            "Platform Version's; holds no run lock."
        ),
        description=(
            "Run each component's own preflight, all at once: its checks of this workstation, its tools, "
            "its sign-in and its access. From main's code, or with --to the templates of that Platform "
            "Version, against copies of origin/main in .dp-instance/preflight/. It changes nothing, starts "
            "no run and holds no run lock, so it runs beside any command, signed out and before the state "
            "account exists; each preflight says what it needs. Neither prepare, plan nor apply runs one."
        ),
    )
    preparing = commands.add_parser(
        "prepare",
        parents=[common, deciding],
        help="Turn your edits, or with --to a Platform Version, into pull requests to review and merge.",
    )
    for sub, what in (
        (checking, "The published Platform Version whose templates' checks run, such as 2026.8.0."),
        (
            preparing,
            "The published Platform Version to upgrade to, the next required stop, such as 2026.8.0.",
        ),
    ):
        target = sub.add_mutually_exclusive_group()
        target.add_argument("--to", metavar="VERSION", help=what)
        target.add_argument(
            "--candidate",
            metavar="FILE",
            # Data Methods' own pre-release instance only: an unpublished candidate manifest.
            help=argparse.SUPPRESS,
        )
    preparing.add_argument(
        "--no-preview",
        action="store_true",
        help=(
            "Open the pull requests without planning the platform's steps, so apply asks about each change "
            "unless apply --auto-approve covers it; a destroy or replacement always asks. The preview "
            "apply reads is the last prepare's: prepare again without it to preview the change."
        ),
    )
    preview = commands.add_parser(
        "plan",
        parents=[common],
        help=(
            "Plan, read only, every enabled step of every component in the release, or one with --step, "
            "on main, the open change and your branch; starts no run."
        ),
        description=(
            "Plan, read only, every enabled platform step of every component in the release, the phases "
            "after a merge included, or one step with --step. It plans what apply would do once the change "
            "merges: origin/main, the open change's branch while its pull requests are open, then your "
            "branch's commits, in copies of its own, renders first; edits that aren't committed are left "
            "out and named. It runs no upgrade or migrate, pushes nothing, opens no pull request and starts "
            "no run, and its record stays on this workstation. A step that skips across runs, and that "
            "nothing in the change reaches, reports no change without planning, so drift there shows only "
            "in apply's drift check."
        ),
    )
    commands.add_parser(
        "apply",
        parents=[common, approving],
        help="Bring the platform to main, as merged on GitHub, once the open change's pull requests merge.",
    )
    commands.add_parser(
        "resume",
        parents=[common, approving],
        help="Carry on the instance's stopped run, from any workstation.",
    )
    check = commands.add_parser("verify", parents=[common], help="Run every step's read-only verify.")
    for sub, what in ((preview, "Plan"), (check, "Verify")):
        sub.add_argument(
            "--step",
            metavar="NAME",
            help=f"{what} one step alone, by its full name: repository/component/phase/step.",
        )
    unlocking = commands.add_parser(
        "unlock",
        parents=[common],
        help="Clear the run lock the named run holds, when its command hangs while its process is alive.",
    )
    unlocking.add_argument("run", metavar="RUN", help="The run that holds the lock, as status names it.")
    itself = commands.add_parser("self", parents=[common], help="Install another dp-instance.")
    itself_commands = itself.add_subparsers(dest="itself", metavar="ACTION", required=True)
    updating = itself_commands.add_parser(
        "update", parents=[common], help="Install the newest published dp-instance, or the one --to names."
    )
    updating.add_argument(
        "--to",
        metavar="VERSION",
        help="An exact published dp-instance version, such as 4.0.0, to pin or roll back to.",
    )

    access = commands.add_parser("template-access", parents=[common], help="Set up or check template access.")
    access_commands = access.add_subparsers(dest="access", metavar="ACTION", required=True)
    for name, text, default in (
        (
            "setup",
            "Set up template access: each key, its vault copy and SSH alias.",
            "every template in the repository map whose key the Platform Infrastructure Key Vault holds",
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
        ready_for_template_access(start)
        if arguments.access == "setup":
            return template_access.setup(
                screen,
                start,
                arguments.template,
                instance=arguments.instance,
                development=arguments.development,
            )
        return template_access.check(screen, start, arguments.template, instance=arguments.instance)
    if arguments.command == "self":
        return self_update.self_update(screen, start, arguments.to)
    options = Options(
        json=arguments.json,
        auto_approve=getattr(arguments, "auto_approve", False),
        approvals=tuple(getattr(arguments, "approve", ())),
    )
    session = Session(options, start, screen=screen)
    command = arguments.command or "status"
    if command == "preflight":
        return views.preflight(session, arguments.to, arguments.candidate)
    try:
        return _on_instance(session, command, arguments)
    except Stop as stop:
        raise Stop(
            stop.status,
            stop.message,
            next=naming_preflight(stop.status, stop.detail.get("reason"), stop.next),
            **stop.detail,
        ) from None


def _on_instance(session: Session, command: str, arguments) -> Result:
    """A command on a found instance, which `preflight` can check: each of its failed stops but a
    temporary one names it. A stop before, as when the instance can't be found or is
    refused, names none, as `preflight` meets it too; nor do `setup`, `template-access` and
    `self update`, which run without an instance."""
    if command == "status":
        return status.status(session)
    if command == "unlock":
        return lock.unlock(session, arguments.run)
    with session.locked(" ".join(part for part in (command, getattr(arguments, "secret", None)) if part)):
        if command == "prepare":
            return runs.prepare(session, arguments.to, arguments.candidate, preview=not arguments.no_preview)
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
    argv = sys.argv[1:] if argv is None else argv
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
                getattr(arguments, "itself", None),
            )
            if part
        )
        or "status"
    )
    # One spinner from here, at a terminal: each section takes it over, and a question or a stop
    # ends it.
    screen.working()
    try:
        result = _dispatch(arguments, screen)
    except Stop as stop:
        screen.read()
        result = Result(command, stop.status, stop.message, stop.next, stop.detail)
        screen.blank()
        screen.note(
            stop.message, {"failed": "bold red", "decision": "bold yellow"}.get(stop.status, "bold orange1")
        )
        if stop.next:
            screen.continuing(stop.next)
    except KeyboardInterrupt:
        screen.read()
        result = Result(command, "failed", "Interrupted.")
    # Every command's total, the time it took, and its parts' times.
    result.detail.update(screen.total())
    screen.emit(result.describe())
    return result.code


if __name__ == "__main__":
    sys.exit(main())
