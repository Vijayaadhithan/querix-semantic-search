"""Manage analytics-only users without placing passwords in process arguments."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .auth import COMPANY_USER, INTERNAL_ADMIN, AnalyticsAuthStore
from .config import AnalyticsSettings, load_analytics_registry


CREDENTIAL_USERNAME_ENV = "ANALYTICS_CREDENTIAL_USERNAME"
CREDENTIAL_PASSWORD_ENV = "ANALYTICS_CREDENTIAL_PASSWORD"
CREDENTIAL_ROLE_ENV = "ANALYTICS_CREDENTIAL_ROLE"
CREDENTIAL_COMPANY_ENV = "ANALYTICS_CREDENTIAL_COMPANY_ID"


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    username: str
    password: str
    role: str
    company_id: str | None


def _credential_record_from_env() -> CredentialRecord:
    values = {
        CREDENTIAL_USERNAME_ENV: os.getenv(CREDENTIAL_USERNAME_ENV, "").strip(),
        CREDENTIAL_PASSWORD_ENV: os.getenv(CREDENTIAL_PASSWORD_ENV, ""),
        CREDENTIAL_ROLE_ENV: os.getenv(CREDENTIAL_ROLE_ENV, "").strip(),
        CREDENTIAL_COMPANY_ENV: os.getenv(CREDENTIAL_COMPANY_ENV, "").strip(),
    }
    missing = [
        name
        for name in (
            CREDENTIAL_USERNAME_ENV,
            CREDENTIAL_PASSWORD_ENV,
            CREDENTIAL_ROLE_ENV,
        )
        if not values[name]
    ]
    if missing:
        raise ValueError(
            "Analytics credential environment is incomplete; missing: "
            + ", ".join(missing)
        )
    role = values[CREDENTIAL_ROLE_ENV]
    if role not in {COMPANY_USER, INTERNAL_ADMIN}:
        raise ValueError("Analytics credential role is invalid")
    company_id = values[CREDENTIAL_COMPANY_ENV] or None
    if role == COMPANY_USER and company_id is None:
        raise ValueError(
            "Company analytics credentials require a company id"
        )
    if role == INTERNAL_ADMIN and company_id is not None:
        raise ValueError(
            "Internal analytics credentials cannot include a company id"
        )
    return CredentialRecord(
        username=values[CREDENTIAL_USERNAME_ENV],
        password=values[CREDENTIAL_PASSWORD_ENV],
        role=role,
        company_id=company_id.casefold() if company_id else None,
    )


def _generate_credentials_file(
    path: Path,
    *,
    username: str,
    role: str,
    company_id: str | None,
    replace: bool = False,
) -> None:
    normalized_username = username.strip()
    if not normalized_username:
        raise ValueError("Analytics credential username is required")
    if role not in {COMPANY_USER, INTERNAL_ADMIN}:
        raise ValueError("Analytics credential role is invalid")
    normalized_company = company_id.strip().casefold() if company_id else None
    if role == COMPANY_USER and normalized_company is None:
        raise ValueError("Company credentials require --company")
    if role == INTERNAL_ADMIN and normalized_company is not None:
        raise ValueError("Internal credentials cannot use --company")
    path.parent.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(36)
    lines = (
        f"{CREDENTIAL_USERNAME_ENV}={json.dumps(normalized_username)}\n"
        f"{CREDENTIAL_PASSWORD_ENV}={json.dumps(password)}\n"
        f"{CREDENTIAL_ROLE_ENV}={json.dumps(role)}\n"
        f"{CREDENTIAL_COMPANY_ENV}="
        f"{json.dumps(normalized_company or '')}\n"
    )
    if path.exists() and not replace:
        raise FileExistsError(path)
    write_path = (
        path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        if replace
        else path
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(write_path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(lines)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(write_path, path)
    except Exception:
        write_path.unlink(missing_ok=True)
        raise
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        path.unlink(missing_ok=True)
        raise ValueError("Credential file could not be secured to mode 0600")


def _sync_credential_record(
    store: AnalyticsAuthStore,
    record: CredentialRecord,
) -> str:
    existing = next(
        (
            user
            for user in store.list_users()
            if str(user["username"]).casefold()
            == record.username.casefold()
        ),
        None,
    )
    if existing is None:
        store.create_user(
            username=record.username,
            password=record.password,
            role=record.role,
            company_id=record.company_id,
        )
        return "created"
    if (
        existing["role"] != record.role
        or existing["company_id"] != record.company_id
    ):
        raise ValueError(
            "Credential binding does not match the existing analytics user"
        )
    store.set_password(record.username, record.password)
    return "updated"


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

    generate = commands.add_parser("generate-credentials")
    generate.add_argument("--file", required=True, type=Path)
    generate.add_argument("--username", required=True)
    generate.add_argument(
        "--role",
        required=True,
        choices=(INTERNAL_ADMIN, COMPANY_USER),
    )
    generate.add_argument("--company")
    generate.add_argument("--replace", action="store_true")

    commands.add_parser("sync-credentials")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "generate-credentials":
        try:
            _generate_credentials_file(
                args.file,
                username=args.username,
                role=args.role,
                company_id=args.company,
                replace=args.replace,
            )
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps({"credentials_file_created": True}))
        return 0
    settings = AnalyticsSettings.from_env()
    store = AnalyticsAuthStore(
        settings.snapshot_db_path,
        session_ttl_seconds=settings.session_ttl_seconds,
        company_session_idle_seconds=(
            settings.company_session_idle_seconds
        ),
        company_session_absolute_seconds=(
            settings.company_session_absolute_seconds
        ),
        internal_session_idle_seconds=(
            settings.internal_session_idle_seconds
        ),
        internal_session_absolute_seconds=(
            settings.internal_session_absolute_seconds
        ),
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
    if args.command == "sync-credentials":
        try:
            record = _credential_record_from_env()
            if record.role == COMPANY_USER:
                registry = load_analytics_registry(
                    settings.tenant_config_dir
                )
                if registry.resolve_company(record.company_id or "") is None:
                    raise ValueError(
                        "Credential company is unknown or analytics is disabled"
                    )
            status = _sync_credential_record(store, record)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps({"credentials_synced": True, "status": status}))
        return 0
    raise AssertionError("Unreachable analytics user command")


if __name__ == "__main__":
    raise SystemExit(main())
