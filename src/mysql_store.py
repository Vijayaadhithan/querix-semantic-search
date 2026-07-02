from dataclasses import dataclass, field
import ssl
from typing import Any

from settings import (
    MYSQL_BM25_COLUMN,
    MYSQL_CONTENT_COLUMN,
    MYSQL_DATABASE,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_RESULT_ID_COLUMN,
    MYSQL_RESULT_TABLE,
    MYSQL_SEARCH_ID_COLUMN,
    MYSQL_TABLE,
    MYSQL_USER,
)

MYSQL_ID_CANDIDATES = ("id", "ad_id", "ads_id", "adId", "adsId")


@dataclass(frozen=True)
class MySQLRuntimeConfig:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    search_table: str
    content_column: str
    bm25_column: str
    search_id_column: str
    result_table: str
    result_id_column: str
    result_type_column: str = "type"
    connect_timeout_seconds: int = 10
    read_timeout_seconds: int = 300
    write_timeout_seconds: int = 300
    statement_timeout_ms: int = 0
    pool_min_size: int = 0
    pool_max_size: int = 4
    pool_timeout_seconds: float = 5.0
    tls_mode: str = "disable"
    tls_ca_file: str = ""
    tls_cert_file: str = ""
    tls_key_file: str = ""

    def __post_init__(self) -> None:
        if min(
            self.connect_timeout_seconds,
            self.read_timeout_seconds,
            self.write_timeout_seconds,
        ) <= 0:
            raise ValueError("MySQL connection timeouts must be greater than zero")
        if self.statement_timeout_ms < 0:
            raise ValueError("MySQL statement_timeout_ms must not be negative")
        if (
            self.pool_min_size < 0
            or self.pool_max_size <= 0
            or self.pool_min_size > self.pool_max_size
        ):
            raise ValueError("Invalid MySQL connection pool size")
        if self.pool_timeout_seconds <= 0:
            raise ValueError("MySQL pool_timeout_seconds must be greater than zero")
        if self.tls_mode not in {
            "disable",
            "prefer",
            "require",
            "verify-ca",
            "verify-full",
        }:
            raise ValueError(f"Unsupported MySQL TLS mode {self.tls_mode!r}")


DEFAULT_MYSQL_CONFIG = MySQLRuntimeConfig(
    host=MYSQL_HOST,
    port=MYSQL_PORT,
    database=MYSQL_DATABASE,
    user=MYSQL_USER,
    password=MYSQL_PASSWORD,
    search_table=MYSQL_TABLE,
    content_column=MYSQL_CONTENT_COLUMN,
    bm25_column=MYSQL_BM25_COLUMN,
    search_id_column=MYSQL_SEARCH_ID_COLUMN,
    result_table=MYSQL_RESULT_TABLE,
    result_id_column=MYSQL_RESULT_ID_COLUMN,
)


def resolved_mysql_config(
    config: MySQLRuntimeConfig | None = None,
) -> MySQLRuntimeConfig:
    return config or DEFAULT_MYSQL_CONFIG


def require_pymysql():
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError(
            "MySQL support requires PyMySQL. Install requirements.txt first."
        ) from exc
    return pymysql


def _mysql_ssl_context(config: MySQLRuntimeConfig) -> ssl.SSLContext | None:
    if config.tls_mode in {"disable", "prefer"}:
        return None
    if config.tls_mode == "require":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    else:
        context = ssl.create_default_context(
            cafile=config.tls_ca_file or None,
        )
        context.check_hostname = config.tls_mode == "verify-full"
    if config.tls_cert_file:
        context.load_cert_chain(
            certfile=config.tls_cert_file,
            keyfile=config.tls_key_file or None,
        )
    return context


def mysql_connection(
    cursorclass=None,
    config: MySQLRuntimeConfig | None = None,
):
    config = resolved_mysql_config(config)
    pymysql = require_pymysql()
    connection_options = {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "password": config.password,
        "database": config.database,
        "charset": "utf8mb4",
        "autocommit": True,
        "connect_timeout": config.connect_timeout_seconds,
        "read_timeout": config.read_timeout_seconds,
        "write_timeout": config.write_timeout_seconds,
    }
    if config.statement_timeout_ms:
        connection_options["init_command"] = (
            "SET SESSION MAX_EXECUTION_TIME="
            f"{config.statement_timeout_ms}"
        )
    ssl_context = _mysql_ssl_context(config)
    if config.tls_mode == "disable":
        connection_options["ssl_disabled"] = True
    elif ssl_context is not None:
        connection_options["ssl"] = ssl_context
    if cursorclass is not None:
        connection_options["cursorclass"] = cursorclass
    return pymysql.connect(
        **connection_options,
    )


def quote_mysql_identifier(identifier: str) -> str:
    if not identifier or "\x00" in identifier:
        raise ValueError("MySQL identifiers must be non-empty strings")
    return f"`{identifier.replace('`', '``')}`"


def mysql_source_name(config: MySQLRuntimeConfig | None = None) -> str:
    config = resolved_mysql_config(config)
    return f"mysql:{config.database}.{config.search_table}"


def fetch_mysql_columns(
    table: str | None = None,
    config: MySQLRuntimeConfig | None = None,
) -> list[str]:
    config = resolved_mysql_config(config)
    table = table or config.search_table
    pymysql = require_pymysql()
    with mysql_connection(
        cursorclass=pymysql.cursors.DictCursor,
        config=config,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"SHOW COLUMNS FROM {quote_mysql_identifier(table)}")
            return [row["Field"] for row in cursor.fetchall()]


def detect_mysql_primary_key(
    columns: list[str],
    override: str | None = None,
    table: str | None = None,
    config: MySQLRuntimeConfig | None = None,
) -> str | None:
    config = resolved_mysql_config(config)
    table = table or config.search_table
    if override:
        if override not in columns:
            raise RuntimeError(f"MySQL primary key column '{override}' was not found.")
        return override

    pymysql = require_pymysql()
    with mysql_connection(
        cursorclass=pymysql.cursors.DictCursor,
        config=config,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SHOW KEYS FROM {quote_mysql_identifier(table)} "
                "WHERE Key_name = 'PRIMARY'"
            )
            keys = cursor.fetchall()
            if keys:
                return keys[0]["Column_name"]

    return next((name for name in MYSQL_ID_CANDIDATES if name in columns), None)


def count_mysql_rows(
    content_column: str | None = None,
    table: str | None = None,
    config: MySQLRuntimeConfig | None = None,
) -> int:
    config = resolved_mysql_config(config)
    content_column = content_column or config.content_column
    table = table or config.search_table
    pymysql = require_pymysql()
    quoted_content = quote_mysql_identifier(content_column)
    where_clause = f"{quoted_content} IS NOT NULL AND TRIM({quoted_content}) <> ''"
    with mysql_connection(
        cursorclass=pymysql.cursors.DictCursor,
        config=config,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT COUNT(*) AS row_count FROM {quote_mysql_identifier(table)} "
                f"WHERE {where_clause}"
            )
            return int(cursor.fetchone()["row_count"])


def iter_mysql_rows(
    content_column: str | None,
    primary_key_column: str | None,
    limit: int | None = None,
    table: str | None = None,
    config: MySQLRuntimeConfig | None = None,
    fetch_batch_size: int = 1000,
):
    if fetch_batch_size <= 0:
        raise ValueError("fetch_batch_size must be greater than zero")
    config = resolved_mysql_config(config)
    content_column = content_column or config.content_column
    table = table or config.search_table
    pymysql = require_pymysql()
    quoted_content = quote_mysql_identifier(content_column)
    quoted_table = quote_mysql_identifier(table)
    quoted_primary_key = (
        quote_mysql_identifier(primary_key_column)
        if primary_key_column
        else None
    )
    emitted = 0
    last_primary_key = None
    has_last_primary_key = False
    offset = 0

    while limit is None or emitted < limit:
        page_size = (
            fetch_batch_size
            if limit is None
            else min(fetch_batch_size, limit - emitted)
        )
        conditions = [
            f"{quoted_content} IS NOT NULL",
            f"TRIM({quoted_content}) <> ''",
        ]
        params: list[Any] = []
        if quoted_primary_key and has_last_primary_key:
            conditions.append(f"{quoted_primary_key} > %s")
            params.append(last_primary_key)
        query = (
            f"SELECT * FROM {quoted_table} "
            f"WHERE {' AND '.join(conditions)}"
        )
        if quoted_primary_key:
            query += f" ORDER BY {quoted_primary_key}"
        query += " LIMIT %s"
        params.append(page_size)
        if not quoted_primary_key:
            query += " OFFSET %s"
            params.append(offset)

        # Fetch the complete page and close the database connection before
        # yielding any row. Ingestion can then spend minutes embedding the
        # page without leaving a streaming MySQL socket idle.
        with mysql_connection(
            cursorclass=pymysql.cursors.DictCursor,
            config=config,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                rows = list(cursor.fetchall())
        if not rows:
            break

        if quoted_primary_key:
            last_primary_key = rows[-1][primary_key_column]
            has_last_primary_key = True
        else:
            offset += len(rows)
        emitted += len(rows)
        yield from rows

        if len(rows) < page_size:
            break


def fetch_product_types_by_ids(
    product_ids,
    connection=None,
    config: MySQLRuntimeConfig | None = None,
) -> dict[str, str]:
    config = resolved_mysql_config(config)
    unique_ids = list(dict.fromkeys(product_ids))
    if not unique_ids:
        return {}

    owns_connection = connection is None
    if owns_connection:
        pymysql = require_pymysql()
        connection = mysql_connection(
            cursorclass=pymysql.cursors.DictCursor,
            config=config,
        )

    placeholders = ", ".join(["%s"] * len(unique_ids))
    query = (
        f"SELECT {quote_mysql_identifier(config.result_id_column)}, "
        f"{quote_mysql_identifier(config.result_type_column)} "
        f"FROM {quote_mysql_identifier(config.result_table)} "
        f"WHERE {quote_mysql_identifier(config.result_id_column)} "
        f"IN ({placeholders})"
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, unique_ids)
            rows = cursor.fetchall()
    finally:
        if owns_connection:
            connection.close()

    return {
        str(row[config.result_id_column]): str(row[config.result_type_column])
        for row in rows
        if row.get(config.result_id_column) is not None
        and row.get(config.result_type_column) is not None
    }


def fetch_products_by_ids(
    product_ids,
    connection=None,
    config: MySQLRuntimeConfig | None = None,
) -> list[dict]:
    config = resolved_mysql_config(config)
    unique_ids = list(dict.fromkeys(product_ids))
    if not unique_ids:
        return []

    owns_connection = connection is None
    if owns_connection:
        pymysql = require_pymysql()
        connection = mysql_connection(
            cursorclass=pymysql.cursors.DictCursor,
            config=config,
        )

    placeholders = ", ".join(["%s"] * len(unique_ids))
    query = (
        f"SELECT * FROM {quote_mysql_identifier(config.result_table)} "
        f"WHERE {quote_mysql_identifier(config.result_id_column)} "
        f"IN ({placeholders})"
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, unique_ids)
            rows = cursor.fetchall()
    finally:
        if owns_connection:
            connection.close()

    rows_by_id = {
        str(row[config.result_id_column]): row
        for row in rows
        if row.get(config.result_id_column) is not None
    }
    return [
        rows_by_id[str(product_id)]
        for product_id in unique_ids
        if str(product_id) in rows_by_id
    ]
