#!/usr/bin/env python3
"""Identify transient database failures while rejecting permanent errors."""

from __future__ import annotations

import argparse
import runpy
import sys
import traceback
from dataclasses import replace
from pathlib import Path

TRANSIENT_EXIT_CODE = 75
MYSQL_TRANSIENT_CODES = {2002, 2003, 2006, 2013, 2055}
POSTGRES_TRANSIENT_CODES = {"57P01", "57P02", "57P03", "53300"}


def is_transient_database_error(error: BaseException) -> bool:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        module = type(current).__module__
        if module.startswith("pymysql"):
            code = current.args[0] if current.args else None
            if isinstance(code, int) and code in MYSQL_TRANSIENT_CODES:
                return True
        if module.startswith("psycopg"):
            state = getattr(current, "sqlstate", None)
            if state and (state.startswith("08") or state in POSTGRES_TRANSIENT_CODES):
                return True
            if state is None and type(current).__name__ == "OperationalError":
                return True
        for name in ("orig", "__cause__", "__context__"):
            nested = getattr(current, name, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


def probe_databases(company_id: str) -> None:
    # The wrapper is mounted into the existing analytics image; only this
    # command imports its database dependencies and existing tenant credentials.
    from sqlalchemy import text

    from analytics_service.config import AnalyticsSettings, load_analytics_registry
    from analytics_service.source import _connection

    settings = AnalyticsSettings.from_env()
    company = load_analytics_registry(settings.tenant_config_dir).resolve_company(
        company_id
    )
    if company is None:
        raise ValueError("Unknown or analytics-disabled company")
    targets = (company.database, company.telemetry_database)
    checked: set[tuple[str, str, int, str, str]] = set()
    for target in targets:
        identity = (
            target.backend,
            target.host,
            target.port,
            target.database,
            target.user,
        )
        if identity in checked:
            continue
        checked.add(identity)
        bounded = replace(target, connect_timeout_seconds=5, read_timeout_seconds=5)
        with _connection(bounded) as connection:
            connection.execute(text("SELECT 1")).scalar_one()
    print(f"Analytics source connectivity restored company={company_id}.", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("run-module", "run-script"):
        runner = commands.add_parser(command)
        runner.add_argument("target")
        runner.add_argument("arguments", nargs=argparse.REMAINDER)
    probe = commands.add_parser("probe")
    probe.add_argument("--company", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "probe":
            probe_databases(args.company)
        else:
            sys.argv = [args.target, *args.arguments]
            if args.command == "run-module":
                runpy.run_module(args.target, run_name="__main__", alter_sys=True)
            else:
                sys.path.insert(0, str(Path(args.target).resolve().parent))
                runpy.run_path(args.target, run_name="__main__")
    except Exception as exc:
        if is_transient_database_error(exc):
            print(
                "Transient analytics database failure "
                f"error_type={type(exc).__name__}.",
                file=sys.stderr,
                flush=True,
            )
            return TRANSIENT_EXIT_CODE
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
