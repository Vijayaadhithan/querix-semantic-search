"""Manage analytics-only users without placing passwords in process arguments."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from collections.abc import Sequence

from .auth import COMPANY_USER, INTERNAL_ADMIN, AnalyticsAuthStore
from .config import AnalyticsSettings, load_analytics_registry


def _password_from_input(*, confirm: bool, password_stdin: bool) -> str:
    if password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
        if not password:
            raise SystemExit("No password was received on standard input.")
        return password
    password = getpass.getpass("Password: ")
    if confirm and password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords do not match.")
    return password


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage users for the separate analytics service."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    create.add_argument("--username", required=True)
    create.add_argument(
        "--role",
        required=True,
        choices=(INTERNAL_ADMIN, COMPANY_USER),
    )
    create.add_argument("--company")
    create.add_argument("--password-stdin", action="store_true")

    password = commands.add_parser("set-password")
    password.add_argument("--username", required=True)
    password.add_argument("--password-stdin", action="store_true")

    active = commands.add_parser("set-active")
    active.add_argument("--username", required=True)
    active.add_argument(
        "--active",
        required=True,
        choices=("true", "false"),
    )

    commands.add_parser("list")
    commands.add_parser("prune-sessions")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = AnalyticsSettings.from_env()
    store = AnalyticsAuthStore(
        settings.snapshot_db_path,
        session_ttl_seconds=settings.session_ttl_seconds,
        max_login_attempts=settings.login_max_attempts,
        lock_seconds=settings.login_lock_seconds,
        password_min_length=settings.password_min_length,
    )
    if args.command == "create":
        registry = load_analytics_registry(settings.tenant_config_dir)
        if args.role == COMPANY_USER:
            if not args.company:
                raise SystemExit("--company is required for company_user.")
            company = registry.resolve_company(args.company)
            if company is None:
                raise SystemExit(
                    "Company is unknown or analytics is disabled: "
                    f"{args.company}"
                )
            company_id = company.company_id
        else:
            if args.company:
                raise SystemExit(
                    "--company cannot be used for internal_admin."
                )
            company_id = None
        password = _password_from_input(
            confirm=not args.password_stdin,
            password_stdin=args.password_stdin,
        )
        try:
            result = store.create_user(
                username=args.username,
                password=password,
                role=args.role,
                company_id=company_id,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "set-password":
        password = _password_from_input(
            confirm=not args.password_stdin,
            password_stdin=args.password_stdin,
        )
        try:
            store.set_password(args.username, password)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps({"username": args.username, "updated": True}))
        return 0
    if args.command == "set-active":
        try:
            store.set_active(
                args.username,
                active=args.active == "true",
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(
            json.dumps(
                {
                    "username": args.username,
                    "active": args.active == "true",
                }
            )
        )
        return 0
    if args.command == "list":
        print(json.dumps({"users": store.list_users()}, indent=2))
        return 0
    if args.command == "prune-sessions":
        print(json.dumps({"deleted_sessions": store.prune_expired_sessions()}))
        return 0
    raise AssertionError("Unreachable analytics user command")


if __name__ == "__main__":
    raise SystemExit(main())
