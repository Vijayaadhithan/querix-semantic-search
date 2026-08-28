#!/usr/bin/env python3
"""Provision and verify the least-privilege database workload accounts."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import replace
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.tenant_config import discover_tenant_profiles
from storage.mysql import (
    MySQLRuntimeConfig,
    mysql_connection,
    quote_mysql_identifier,
)
from storage.postgres import postgres_connection, require_psycopg

ROLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
MYSQL_HOST_RE = re.compile(r"^[A-Za-z0-9._%:-]{1,255}$")
DEFAULT_ANALYTICS_TABLES = {
    "search_history": "semantic_search_history",
    "api_usage": "semantic_search_api_usage",
    "categories": "categories",
    "sub_categories": "sub_categories",
    "states": "states",
    "location": "location",
    "attributes": "attributes",
    "attribute_values": "attribute_values",
    "ads_attributes": "ads_attributes",
    "ads": "ads",
    "users": "users",
}


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required database role setting is missing: {name}")
    return value


def _role(name: str) -> str:
    value = _required_env(name)
    if not ROLE_RE.fullmatch(value):
        raise RuntimeError(f"Unsafe database role name in {name}")
    return value


def _mysql_account(user: str, host: str) -> str:
    if not ROLE_RE.fullmatch(user) or not MYSQL_HOST_RE.fullmatch(host):
        raise RuntimeError("Unsafe MySQL account name or host")
    return f"'{user}'@'{host}'"


def _mysql_table(database: str, table: str) -> str:
    return f"{quote_mysql_identifier(database)}.{quote_mysql_identifier(table)}"


def _mysql_grant(
    cursor,
    account: str,
    privileges: tuple[str, ...],
    database: str,
    table: str,
) -> None:
    allowed = {"SELECT", "INSERT", "UPDATE"}
    if not privileges or not set(privileges).issubset(allowed):
        raise RuntimeError("Unexpected MySQL workload privilege")
    cursor.execute(
        f"GRANT {', '.join(privileges)} ON {_mysql_table(database, table)} TO {account}"
    )


def provision_mysql(profiles) -> None:
    mysql_profiles = [
        profile
        for profile in profiles
        if isinstance(profile.database, MySQLRuntimeConfig)
    ]
    if not mysql_profiles:
        return
    identities = {
        (
            profile.database.host,
            profile.database.port,
            profile.database.database,
        )
        for profile in mysql_profiles
    }
    hosts = {(host, port) for host, port, _database in identities}
    if len(hosts) != 1:
        raise RuntimeError(
            "One provisioning run cannot administer tenants on multiple MySQL hosts"
        )

    users = {
        "search": _role("MYSQL_SEARCH_USER"),
        "ingest": _role("MYSQL_INGEST_USER"),
        "telemetry": _role("MYSQL_TELEMETRY_USER"),
        "analytics": _role("MYSQL_ANALYTICS_USER"),
    }
    if len(set(users.values())) != len(users):
        raise RuntimeError("MySQL workload users must be distinct")
    passwords = {
        workload: _required_env(f"MYSQL_{workload.upper()}_PASSWORD")
        for workload in users
    }
    account_host = os.getenv("MYSQL_ROLE_HOST", "%").strip() or "%"
    accounts = {
        workload: _mysql_account(user, account_host) for workload, user in users.items()
    }
    admin_config = replace(
        mysql_profiles[0].database,
        user=_required_env("MYSQL_ADMIN_USER"),
        password=_required_env("MYSQL_ADMIN_PASSWORD"),
    )
    with mysql_connection(config=admin_config) as connection:
        with connection.cursor() as cursor:
            for workload, account in accounts.items():
                cursor.execute(
                    f"CREATE USER IF NOT EXISTS {account} IDENTIFIED BY %s",
                    (passwords[workload],),
                )
                cursor.execute(
                    f"ALTER USER {account} IDENTIFIED BY %s",
                    (passwords[workload],),
                )
                cursor.execute(f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM {account}")

            for profile in mysql_profiles:
                database = profile.database.database
                search_tables = {
                    profile.database.search_table,
                    profile.database.result_table,
                }
                for table in sorted(search_tables):
                    _mysql_grant(
                        cursor,
                        accounts["search"],
                        ("SELECT",),
                        database,
                        table,
                    )
                for table in sorted(
                    {profile.database.search_table, profile.database.result_table}
                ):
                    _mysql_grant(
                        cursor,
                        accounts["ingest"],
                        ("SELECT",),
                        database,
                        table,
                    )
                for table in (
                    profile.analytics.search_history_table,
                    profile.analytics.api_usage_table,
                ):
                    _mysql_grant(
                        cursor,
                        accounts["telemetry"],
                        ("SELECT", "INSERT", "UPDATE"),
                        database,
                        table,
                    )
                raw = yaml.safe_load(profile.config_path.read_text(encoding="utf-8"))
                analytics = dict((raw or {}).get("analytics", {}))
                analytics_tables = {
                    **DEFAULT_ANALYTICS_TABLES,
                    "search_history": profile.analytics.search_history_table,
                    "api_usage": profile.analytics.api_usage_table,
                    **{
                        str(name): str(table)
                        for name, table in dict(analytics.get("tables", {})).items()
                    },
                }
                for table in sorted(set(analytics_tables.values())):
                    _mysql_grant(
                        cursor,
                        accounts["analytics"],
                        ("SELECT",),
                        database,
                        table,
                    )

            for workload, account in accounts.items():
                cursor.execute(f"SHOW GRANTS FOR {account}")
                grants = [str(row[0]).upper() for row in cursor.fetchall()]
                has_global_grant = any(
                    " ON *.* " in grant and "USAGE ON *.*" not in grant
                    for grant in grants
                )
                if has_global_grant:
                    raise RuntimeError(
                        f"MySQL {workload} account retained a global privilege"
                    )


def provision_postgres(profiles) -> None:
    postgres_profiles = [
        profile for profile in profiles if profile.storage.pgvector_database is not None
    ]
    if not postgres_profiles:
        return
    search_role = _role("PGVECTOR_SEARCH_USER")
    ingest_role = _role("PGVECTOR_INGEST_USER")
    admin_role = _role("POSTGRES_USER")
    if len({search_role, ingest_role, admin_role}) != 3:
        raise RuntimeError("PostgreSQL admin, search, and ingestion roles must differ")
    role_passwords = {
        search_role: _required_env("PGVECTOR_SEARCH_PASSWORD"),
        ingest_role: _required_env("PGVECTOR_INGEST_PASSWORD"),
    }
    admin_password = _required_env("POSTGRES_PASSWORD")
    databases = {}
    for profile in postgres_profiles:
        config = profile.storage.pgvector_database
        identity = (config.host, config.port, config.database)
        databases.setdefault(identity, []).append(profile)

    for (_host, _port, _database), grouped_profiles in databases.items():
        base_config = grouped_profiles[0].storage.pgvector_database
        admin_config = replace(
            base_config,
            user=admin_role,
            password=admin_password,
        )
        require_psycopg()
        from psycopg import sql

        with postgres_connection(admin_config, autocommit=True) as connection:
            with connection.cursor() as cursor:
                for role_name, password in role_passwords.items():
                    cursor.execute(
                        "SELECT 1 FROM pg_roles WHERE rolname = %s",
                        (role_name,),
                    )
                    if cursor.fetchone() is None:
                        cursor.execute(
                            sql.SQL("CREATE ROLE {} LOGIN").format(
                                sql.Identifier(role_name)
                            )
                        )
                    cursor.execute(
                        sql.SQL(
                            "ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB "
                            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS "
                            "PASSWORD {}"
                        ).format(sql.Identifier(role_name), sql.Literal(password))
                    )
                    cursor.execute(
                        """
                        SELECT parent.rolname
                        FROM pg_auth_members AS membership
                        JOIN pg_roles AS parent ON parent.oid = membership.roleid
                        JOIN pg_roles AS member ON member.oid = membership.member
                        WHERE member.rolname = %s
                        """,
                        (role_name,),
                    )
                    for membership in cursor.fetchall():
                        cursor.execute(
                            sql.SQL("REVOKE {} FROM {}").format(
                                sql.Identifier(str(membership[0])),
                                sql.Identifier(role_name),
                            )
                        )
                    cursor.execute(
                        sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(
                            sql.Identifier(base_config.database),
                            sql.Identifier(role_name),
                        )
                    )
                    cursor.execute(
                        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                            sql.Identifier(base_config.database),
                            sql.Identifier(role_name),
                        )
                    )
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")

                schemas = {
                    profile.storage.pgvector_database.schema
                    for profile in grouped_profiles
                }
                for schema in sorted(schemas):
                    schema_identifier = sql.Identifier(schema)
                    cursor.execute(
                        sql.SQL("REVOKE CREATE ON SCHEMA {} FROM PUBLIC").format(
                            schema_identifier
                        )
                    )
                    cursor.execute(
                        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                            schema_identifier,
                            sql.Identifier(search_role),
                        )
                    )
                    cursor.execute(
                        sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(
                            schema_identifier,
                            sql.Identifier(ingest_role),
                        )
                    )
                    cursor.execute(
                        sql.SQL(
                            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} FROM {}"
                        ).format(schema_identifier, sql.Identifier(search_role))
                    )
                    cursor.execute(
                        sql.SQL(
                            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
                            "GRANT SELECT ON TABLES TO {}"
                        ).format(
                            sql.Identifier(ingest_role),
                            schema_identifier,
                            sql.Identifier(search_role),
                        )
                    )

                for profile in grouped_profiles:
                    config = profile.storage.pgvector_database
                    candidates = {
                        profile.storage.pgvector_table,
                        f"{profile.storage.pgvector_table}_a",
                        f"{profile.storage.pgvector_table}_b",
                    }
                    cursor.execute(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = %s AND table_name = ANY(%s)
                        """,
                        (config.schema, sorted(candidates)),
                    )
                    tables = [str(row[0]) for row in cursor.fetchall()]
                    if not tables:
                        raise RuntimeError(
                            f"No pgvector tables found for tenant {profile.company_id}"
                        )
                    for table in tables:
                        qualified = sql.SQL("{}.{}").format(
                            sql.Identifier(config.schema),
                            sql.Identifier(table),
                        )
                        cursor.execute(
                            sql.SQL("ALTER TABLE {} OWNER TO {}").format(
                                qualified,
                                sql.Identifier(ingest_role),
                            )
                        )
                        cursor.execute(
                            sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(
                                qualified,
                                sql.Identifier(search_role),
                            )
                        )

                cursor.execute(
                    """
                    SELECT rolname, rolsuper, rolcreatedb, rolcreaterole,
                           rolreplication, rolbypassrls
                    FROM pg_roles
                    WHERE rolname = ANY(%s)
                    """,
                    ([search_role, ingest_role],),
                )
                rows = cursor.fetchall()
                if len(rows) != 2 or any(any(row[1:]) for row in rows):
                    raise RuntimeError("PostgreSQL workload role attributes are unsafe")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--tenant-config-dir",
        type=Path,
        default=ROOT / "configs/tenants",
    )
    parser.add_argument("--skip-mysql", action="store_true")
    parser.add_argument("--skip-postgres", action="store_true")
    args = parser.parse_args()
    profiles = list(discover_tenant_profiles(args.tenant_config_dir).values())
    if not args.skip_mysql:
        provision_mysql(profiles)
        print("MySQL workload roles are provisioned and verified.")
    if not args.skip_postgres:
        provision_postgres(profiles)
        print("PostgreSQL workload roles are provisioned and verified.")


if __name__ == "__main__":
    main()
