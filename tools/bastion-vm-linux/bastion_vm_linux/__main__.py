from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import deployment
from .azure import AzureCli, CommandError
from .changes import confirm, render_plan
from .config import DEFAULT_FILE, WorkstationConfig, configure, validate_context


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bastion-vm-linux.ps1")
    actions = result.add_subparsers(dest="action", required=True)
    for name in (
        "configure",
        "reconfigure",
        "authorize",
        "apply",
        "status",
        "connection",
        "delete",
    ):
        command = actions.add_parser(name)
        command.add_argument("--file", type=Path, default=DEFAULT_FILE)
        if name in {"authorize", "apply", "delete"}:
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--yes", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.action in {"configure", "reconfigure"}:
            configure(args.file, reconfigure=args.action == "reconfigure")
            return 0
        config = WorkstationConfig.load(args.file)
        azure = AzureCli(subscription=config.subscription_id)
        validate_context(config, azure)
        print(f"Platform Instance: {config.platform_instance_key}")
        print(f"Subscription: {config.subscription_id}")
        print(f"Stack: {config.name_prefix}-vm")
        if args.action == "connection":
            deployment.connection(config, azure)
            return 0
        if args.action == "authorize":
            planned = deployment.authorization_changes(config, azure)
            blockers = []
        elif args.action == "delete":
            planned = deployment.deletion_changes(config, azure)
            blockers = []
        else:
            planned = deployment.changes(config, azure)
            blockers = deployment.blockers(config, azure) if args.action == "status" else []
        if args.action == "status":
            render_plan("Bastion VM", "status", planned, dry_run=False, blockers=blockers)
            print(
                "Checked: stack status, recorded deployment parameters and resource-group VM login assignment."
            )
            print("Not checked: guest tools, SSH login or private platform connectivity.")
            return 1 if planned or blockers else 0
        render_plan("Bastion VM", args.action, planned, dry_run=args.dry_run, blockers=blockers)
        if blockers:
            return 1
        if args.dry_run or not planned:
            return 0
        if not confirm(f"Bastion VM {args.action}", yes=args.yes):
            print("Cancelled")
            return 0
        getattr(deployment, args.action)(config, azure)
        return 0
    except (CommandError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
