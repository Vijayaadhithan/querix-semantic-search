"""Resolve tenant database, TLS, pooling, and vector-storage settings."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from core.settings import PROJECT_ROOT
from core.tenant_models import (
    TenantStorageConfig,
)
from storage.mysql import MySQLRuntimeConfig
from storage.postgres import PostgresRuntimeConfig


def _path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _env_name(section: dict[str, Any], key: str, default: str) -> str:
    value = str(section.get(key, default)).strip()
    if not value:
        raise ValueError(f"Environment-variable name {key!r} must not be empty")
    return value


def _env_value(
    section: dict[str, Any],
    key: str,
    default_env: str,
    *,
    default: str = "",
) -> str:
    return os.getenv(_env_name(section, key, default_env), default)


def _identifier(section: dict[str, Any], key: str, default: str) -> str:
    value = str(section.get(key, default)).strip()
    if not value or "\x00" in value:
        raise ValueError(f"Database identifier {key!r} must not be empty")
    return value


def _optional_identifier(section: dict[str, Any], key: str) -> str:
    value = str(section.get(key, "")).strip()
    if "\x00" in value:
        raise ValueError(f"Database identifier {key!r} is invalid")
    return value


def load_database_config(
    database: dict[str, Any], company_id: str
) -> tuple[MySQLRuntimeConfig | PostgresRuntimeConfig, str]:
    backend = str(database.get("backend", "mysql")).strip().casefold()
    if backend not in {"mysql", "postgres"}:
        raise ValueError(
            f"Tenant {company_id!r} has unsupported database backend {backend!r}."
        )
    default_prefix = "POSTGRES" if backend == "postgres" else "MYSQL"
    default_port = "5432" if backend == "postgres" else "3306"
    timeouts = dict(database.get("timeouts", {}))
    pool = dict(database.get("pool", {}))
    tls = dict(database.get("tls", {}))
    try:
        port = int(
            _env_value(
                database,
                "port_env",
                f"{default_prefix}_PORT",
                default=default_port,
            )
        )
    except ValueError as exc:
        raise ValueError(f"Tenant {company_id!r} has an invalid database port") from exc
    common_database_values = {
        "host": _env_value(
            database,
            "host_env",
            f"{default_prefix}_HOST",
            default="localhost",
        ),
        "port": port,
        "database": _env_value(
            database,
            "database_env",
            f"{default_prefix}_DATABASE",
        ),
        "user": _env_value(database, "user_env", f"{default_prefix}_USER"),
        "password": _env_value(
            database,
            "password_env",
            f"{default_prefix}_PASSWORD",
        ),
        "search_table": _identifier(database, "search_ready_table", "ads_search_ready"),
        "content_column": _identifier(database, "content_column", "embedding_content"),
        "bm25_column": _identifier(database, "bm25_column", "bm25_content"),
        "search_id_column": _identifier(database, "search_id_column", "id"),
        "result_table": _identifier(database, "result_table", "ads"),
        "result_id_column": _identifier(database, "result_id_column", "id"),
        "result_type_column": _identifier(database, "result_type_column", "type"),
        "connect_timeout_seconds": int(timeouts.get("connect_seconds", 10)),
        "read_timeout_seconds": int(timeouts.get("read_seconds", 300)),
        "write_timeout_seconds": int(timeouts.get("write_seconds", 300)),
        "statement_timeout_ms": int(timeouts.get("statement_timeout_ms", 0)),
        "pool_min_size": int(pool.get("min_size", 0)),
        "pool_max_size": int(pool.get("max_size", 4)),
        "pool_timeout_seconds": float(pool.get("timeout_seconds", 5)),
        "pool_validation_interval_seconds": float(
            pool.get("validation_interval_seconds", 30)
        ),
        "tls_mode": _env_value(
            tls,
            "mode_env",
            f"{default_prefix}_TLS_MODE",
            default=str(
                tls.get(
                    "mode",
                    "prefer" if backend == "postgres" else "disable",
                )
            ),
        )
        .strip()
        .casefold(),
        "tls_ca_file": _env_value(
            tls,
            "ca_file_env",
            f"{default_prefix}_TLS_CA_FILE",
        ),
        "tls_cert_file": _env_value(
            tls,
            "cert_file_env",
            f"{default_prefix}_TLS_CERT_FILE",
        ),
        "tls_key_file": _env_value(
            tls,
            "key_file_env",
            f"{default_prefix}_TLS_KEY_FILE",
        ),
        "index_namespace": _env_value(
            database,
            "index_namespace_env",
            f"{default_prefix}_INDEX_NAMESPACE",
            default=str(database.get("index_namespace", "")),
        ).strip(),
    }
    mysql = (
        PostgresRuntimeConfig(
            **common_database_values,
            schema=_identifier(database, "schema", "public"),
        )
        if backend == "postgres"
        else MySQLRuntimeConfig(
            **common_database_values,
            active_column=_optional_identifier(database, "active_column"),
        )
    )
    if not mysql.database:
        raise ValueError(
            f"Tenant {company_id!r} database environment variable is empty"
        )
    if not mysql.user:
        raise ValueError(f"Tenant {company_id!r} database user is empty")

    return mysql, backend


def load_storage_config(
    storage: dict, company_id: str, mysql, backend: str
) -> TenantStorageConfig:
    vector_backend = str(storage.get("vector_backend", "pgvector")).strip().casefold()
    if vector_backend != "pgvector":
        raise ValueError(
            f"Tenant {company_id!r} must use vector_backend 'pgvector'; "
            f"received {vector_backend!r}."
        )
    pgvector_database = None
    pgvector_table = "search_vectors"
    pgvector_hnsw_m = 16
    pgvector_hnsw_ef_construction = 64
    pgvector_hnsw_ef_search = 100
    pgvector_query_mode = "legacy"
    pgvector_prewarm_on_startup = False
    pgvector_prewarm_mode = "read"
    vector_dimensions = int(storage.get("vector_dimensions", 768))
    if vector_dimensions <= 0 or vector_dimensions > 2000:
        raise ValueError(
            f"Tenant {company_id!r} vector_dimensions must be between 1 and 2000"
        )
    pgvector = dict(storage.get("pgvector", {}))
    use_company_database = bool(
        pgvector.get("use_company_database", backend == "postgres")
    )
    if use_company_database:
        if not isinstance(mysql, PostgresRuntimeConfig):
            raise ValueError(
                f"Tenant {company_id!r} pgvector use_company_database "
                "requires a PostgreSQL company database."
            )
        pgvector_database = mysql
    else:
        vector_prefix = "PGVECTOR"
        pgvector_database = PostgresRuntimeConfig(
            host=_env_value(
                pgvector,
                "host_env",
                f"{vector_prefix}_HOST",
                default="localhost",
            ),
            port=int(
                _env_value(
                    pgvector,
                    "port_env",
                    f"{vector_prefix}_PORT",
                    default="5432",
                )
            ),
            database=_env_value(
                pgvector,
                "database_env",
                f"{vector_prefix}_DATABASE",
            ),
            user=_env_value(
                pgvector,
                "user_env",
                f"{vector_prefix}_USER",
            ),
            password=_env_value(
                pgvector,
                "password_env",
                f"{vector_prefix}_PASSWORD",
            ),
            schema=_identifier(pgvector, "schema", "public"),
        )
    pgvector_table = _identifier(
        pgvector,
        "table",
        f"{company_id}_search_vectors",
    )
    pgvector_hnsw = dict(pgvector.get("hnsw", {}))
    pgvector_hnsw_m = int(pgvector_hnsw.get("m", 16))
    pgvector_hnsw_ef_construction = int(pgvector_hnsw.get("ef_construction", 64))
    pgvector_hnsw_ef_search = int(pgvector_hnsw.get("ef_search", 100))
    pgvector_query_mode = str(pgvector.get("query_mode", "legacy")).strip().casefold()
    pgvector_prewarm_on_startup = bool(pgvector.get("prewarm_on_startup", False))
    pgvector_prewarm_mode = str(pgvector.get("prewarm_mode", "read")).strip().casefold()
    if pgvector_hnsw_m <= 0:
        raise ValueError(f"Tenant {company_id!r} pgvector hnsw.m must be positive")
    if pgvector_hnsw_ef_construction <= 0:
        raise ValueError(
            f"Tenant {company_id!r} pgvector hnsw.ef_construction must be positive"
        )
    if pgvector_hnsw_ef_search <= 0:
        raise ValueError(
            f"Tenant {company_id!r} pgvector hnsw.ef_search must be positive"
        )
    if pgvector_query_mode not in {"legacy", "shadow", "optimized"}:
        raise ValueError(
            f"Tenant {company_id!r} pgvector query_mode must be "
            "legacy, shadow, or optimized"
        )
    if pgvector_prewarm_mode not in {"buffer", "read", "prefetch"}:
        raise ValueError(
            f"Tenant {company_id!r} pgvector prewarm_mode must be "
            "buffer, read, or prefetch"
        )
    if not pgvector_database.database or not pgvector_database.user:
        raise ValueError(
            f"Tenant {company_id!r} pgvector database and user must be configured."
        )
    storage_config = TenantStorageConfig(
        bm25_path=_path(
            storage.get(
                "bm25_path",
                f"storage/companies/{company_id}/bm25.sqlite3",
            )
        ),
        vector_backend=vector_backend,
        pgvector_database=pgvector_database,
        pgvector_table=pgvector_table,
        vector_dimensions=vector_dimensions,
        pgvector_hnsw_m=pgvector_hnsw_m,
        pgvector_hnsw_ef_construction=pgvector_hnsw_ef_construction,
        pgvector_hnsw_ef_search=pgvector_hnsw_ef_search,
        pgvector_query_mode=pgvector_query_mode,
        pgvector_city_membership=bool(pgvector.get("city_membership", False)),
        pgvector_prewarm_on_startup=pgvector_prewarm_on_startup,
        pgvector_prewarm_mode=pgvector_prewarm_mode,
    )

    return storage_config
