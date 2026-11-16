"""Explicit MySQL telemetry schema provisioning and readiness checks."""

from __future__ import annotations

from storage.mysql import (
    MySQLRuntimeConfig,
    mysql_connection,
    quote_mysql_identifier,
    require_pymysql,
)


def create_search_analytics_schema(
    config: MySQLRuntimeConfig,
    *,
    company_id: str,
    search_history_table: str,
    api_usage_table: str,
) -> None:
    history = quote_mysql_identifier(search_history_table)
    usage = quote_mysql_identifier(api_usage_table)
    pymysql = require_pymysql()
    with (
        mysql_connection(
            cursorclass=pymysql.cursors.DictCursor,
            config=config,
        ) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            f"""
                CREATE TABLE IF NOT EXISTS {history} (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    request_id CHAR(32) CHARACTER SET ascii
                        COLLATE ascii_bin NOT NULL,
                    query_text TEXT NOT NULL,
                    created_at DATETIME(6) NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_search_request_id (request_id),
                    KEY idx_search_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
        )
        cursor.execute(
            f"""
                CREATE TABLE IF NOT EXISTS {usage} (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    request_id CHAR(32) CHARACTER SET ascii
                        COLLATE ascii_bin NOT NULL,
                    company_id VARCHAR(63) NOT NULL,
                    execution_path VARCHAR(32) NOT NULL,
                    result_count INT UNSIGNED NOT NULL DEFAULT 0,
                    total_results INT UNSIGNED NOT NULL DEFAULT 0,
                    status VARCHAR(32) NOT NULL DEFAULT 'success',
                    api_call_count INT UNSIGNED NOT NULL DEFAULT 0,
                    input_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    output_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    thought_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    total_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    duration_ms DECIMAL(14, 3) NOT NULL DEFAULT 0,
                    attempts_json LONGTEXT NOT NULL,
                    plan_cache_hit TINYINT(1) NULL,
                    result_cache_hit TINYINT(1) NULL,
                    timings_json LONGTEXT NULL,
                    context_json LONGTEXT NULL,
                    created_at DATETIME(6) NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_api_usage_request (request_id),
                    KEY idx_usage_company_created (company_id, created_at),
                    KEY idx_usage_execution_created (
                        execution_path,
                        created_at
                    )
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
        )
        _migrate_search_analytics_schema(
            cursor,
            company_id=company_id,
            search_history_table=search_history_table,
            api_usage_table=api_usage_table,
        )


def _table_columns(cursor, table_name: str) -> set[str]:
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = %s
        """,
        (table_name,),
    )
    return {
        str(
            row.get("column_name") or row.get("COLUMN_NAME") or next(iter(row.values()))
        )
        for row in cursor.fetchall()
    }


def _table_indexes(cursor, table_name: str) -> dict[str, set[str]]:
    cursor.execute(
        """
        SELECT index_name, column_name
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = %s
        """,
        (table_name,),
    )
    indexes: dict[str, set[str]] = {}
    for row in cursor.fetchall():
        index_name = str(row.get("index_name") or row.get("INDEX_NAME") or "")
        column_name = str(row.get("column_name") or row.get("COLUMN_NAME") or "")
        if index_name and column_name:
            indexes.setdefault(index_name, set()).add(column_name)
    return indexes


def _drop_indexes_for_columns(
    cursor,
    *,
    table_name: str,
    columns: set[str],
) -> None:
    table = quote_mysql_identifier(table_name)
    for index_name, indexed_columns in _table_indexes(
        cursor,
        table_name,
    ).items():
        if index_name != "PRIMARY" and indexed_columns.intersection(columns):
            cursor.execute(
                f"ALTER TABLE {table} DROP INDEX {quote_mysql_identifier(index_name)}"
            )


def _add_index_if_missing(
    cursor,
    *,
    table_name: str,
    index_name: str,
    definition: str,
) -> None:
    if index_name in _table_indexes(cursor, table_name):
        return
    cursor.execute(f"ALTER TABLE {quote_mysql_identifier(table_name)} ADD {definition}")


def _migrate_search_analytics_schema(
    cursor,
    *,
    company_id: str,
    search_history_table: str,
    api_usage_table: str,
) -> None:
    """Move all operational metrics out of tenant-visible search history."""
    history = quote_mysql_identifier(search_history_table)
    usage = quote_mysql_identifier(api_usage_table)
    usage_columns = _table_columns(cursor, api_usage_table)

    # Version 1 usage rows linked to the tenant table by numeric foreign key.
    # Add portable request/company keys before collapsing those rows.
    if "search_history_id" in usage_columns:
        if "request_id" not in usage_columns:
            cursor.execute(
                f"""
                ALTER TABLE {usage}
                ADD COLUMN request_id CHAR(32) CHARACTER SET ascii
                    COLLATE ascii_bin NULL AFTER id
                """
            )
        if "company_id" not in usage_columns:
            cursor.execute(
                f"""
                ALTER TABLE {usage}
                ADD COLUMN company_id VARCHAR(63) NULL AFTER request_id
                """
            )
        cursor.execute(
            f"""
            UPDATE {usage} AS api_usage
            INNER JOIN {history} AS search_history
                ON search_history.id = api_usage.search_history_id
            SET
                api_usage.request_id = search_history.request_id,
                api_usage.company_id = search_history.company_id
            WHERE api_usage.request_id IS NULL
               OR api_usage.company_id IS NULL
            """
        )
        cursor.execute(
            f"""
            SELECT COUNT(*) AS missing_links
            FROM {usage}
            WHERE request_id IS NULL OR company_id IS NULL
            """
        )
        row = cursor.fetchone()
        missing_links = int(
            row.get("missing_links")
            or row.get("MISSING_LINKS")
            or next(iter(row.values()))
            or 0
        )
        if missing_links:
            raise RuntimeError(
                "Cannot migrate API usage rows with missing search history"
            )

    usage_columns = _table_columns(cursor, api_usage_table)
    # Versions 1 and 2 stored one row per provider attempt. Version 3 stores
    # one internal row per search, preserving attempts as JSON and keeping
    # request totals non-duplicated and easy to aggregate.
    if "attempt_number" in usage_columns:
        cursor.execute(
            f"""
            SELECT COUNT(*) AS orphan_count
            FROM {usage} AS api_usage
            LEFT JOIN {history} AS search_history
                ON search_history.request_id = api_usage.request_id
            WHERE search_history.id IS NULL
            """
        )
        row = cursor.fetchone()
        orphan_count = int(
            row.get("orphan_count")
            or row.get("ORPHAN_COUNT")
            or next(iter(row.values()))
            or 0
        )
        if orphan_count:
            raise RuntimeError("Cannot migrate orphaned internal API usage rows")

        summary_table_name = f"{api_usage_table[:42]}_request_summary_v3"
        backup_table_name = f"{api_usage_table[:47]}_pre_v3"
        summary = quote_mysql_identifier(summary_table_name)
        backup = quote_mysql_identifier(backup_table_name)
        cursor.execute(f"DROP TABLE IF EXISTS {summary}")
        cursor.execute(f"DROP TABLE IF EXISTS {backup}")
        cursor.execute(
            f"""
            CREATE TABLE {summary} (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                request_id CHAR(32) CHARACTER SET ascii
                    COLLATE ascii_bin NOT NULL,
                company_id VARCHAR(63) NOT NULL,
                execution_path VARCHAR(32) NOT NULL,
                result_count INT UNSIGNED NOT NULL DEFAULT 0,
                total_results INT UNSIGNED NOT NULL DEFAULT 0,
                status VARCHAR(32) NOT NULL DEFAULT 'success',
                api_call_count INT UNSIGNED NOT NULL DEFAULT 0,
                input_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                output_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                thought_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                total_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                duration_ms DECIMAL(14, 3) NOT NULL DEFAULT 0,
                attempts_json LONGTEXT NOT NULL,
                plan_cache_hit TINYINT(1) NULL,
                result_cache_hit TINYINT(1) NULL,
                timings_json LONGTEXT NULL,
                context_json LONGTEXT NULL,
                created_at DATETIME(6) NOT NULL,
                PRIMARY KEY (id),
                UNIQUE KEY uq_api_usage_request (request_id),
                KEY idx_usage_company_created (company_id, created_at),
                KEY idx_usage_execution_created (
                    execution_path,
                    created_at
                )
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute(
            f"""
            INSERT INTO {summary} (
                request_id,
                company_id,
                execution_path,
                result_count,
                total_results,
                status,
                api_call_count,
                input_tokens,
                output_tokens,
                thought_tokens,
                total_tokens,
                duration_ms,
                attempts_json,
                plan_cache_hit,
                result_cache_hit,
                timings_json,
                context_json,
                created_at
            )
            SELECT
                search_history.request_id,
                COALESCE(MAX(api_usage.company_id), %s),
                search_history.execution_path,
                search_history.result_count,
                search_history.total_results,
                search_history.status,
                search_history.api_call_count,
                search_history.input_tokens,
                search_history.output_tokens,
                search_history.thought_tokens,
                search_history.total_tokens,
                search_history.duration_ms,
                CASE
                    WHEN COUNT(api_usage.id) = 0 THEN JSON_ARRAY()
                    ELSE CONCAT(
                        '[',
                        GROUP_CONCAT(
                            JSON_OBJECT(
                            'attempt_number',
                                api_usage.attempt_number,
                            'provider', api_usage.provider,
                            'model', api_usage.model,
                            'operation', api_usage.operation,
                            'status', api_usage.status,
                            'api_calls', api_usage.api_calls,
                            'input_tokens', api_usage.input_tokens,
                            'output_tokens', api_usage.output_tokens,
                            'thought_tokens', api_usage.thought_tokens,
                            'total_tokens', api_usage.total_tokens,
                            'duration_ms', api_usage.duration_ms,
                            'failure_reason',
                                api_usage.failure_reason
                            )
                            ORDER BY api_usage.attempt_number
                            SEPARATOR ','
                        ),
                        ']'
                    )
                END,
                NULL,
                NULL,
                NULL,
                NULL,
                search_history.created_at
            FROM {history} AS search_history
            LEFT JOIN {usage} AS api_usage
                ON api_usage.request_id = search_history.request_id
            GROUP BY
                search_history.id,
                search_history.request_id,
                search_history.execution_path,
                search_history.result_count,
                search_history.total_results,
                search_history.status,
                search_history.api_call_count,
                search_history.input_tokens,
                search_history.output_tokens,
                search_history.thought_tokens,
                search_history.total_tokens,
                search_history.duration_ms,
                search_history.created_at
            """,
            ((company_id or "unknown")[:63],),
        )
        cursor.execute(
            f"""
            SELECT
                (SELECT COUNT(*) FROM {history}) AS history_count,
                (SELECT COUNT(*) FROM {summary}) AS summary_count
            """
        )
        row = cursor.fetchone()
        history_count = int(row.get("history_count") or row.get("HISTORY_COUNT") or 0)
        summary_count = int(row.get("summary_count") or row.get("SUMMARY_COUNT") or 0)
        if summary_count != history_count:
            raise RuntimeError(
                "Internal API usage summary count does not match history"
            )
        cursor.execute(
            f"""
            RENAME TABLE
                {usage} TO {backup},
                {summary} TO {usage}
            """
        )
        cursor.execute(f"DROP TABLE {backup}")

    # Versions 4 and 5 add extensible operational timings, explicit cache
    # state, and an allowlisted request-filter context.
    # Columns are nullable so pre-migration rows correctly mean "unknown".
    usage_columns = _table_columns(cursor, api_usage_table)
    for column_name, definition in (
        ("plan_cache_hit", "TINYINT(1) NULL"),
        ("result_cache_hit", "TINYINT(1) NULL"),
        ("timings_json", "LONGTEXT NULL"),
        ("context_json", "LONGTEXT NULL"),
    ):
        if column_name not in usage_columns:
            cursor.execute(
                f"ALTER TABLE {usage} ADD COLUMN "
                f"{quote_mysql_identifier(column_name)} {definition}"
            )

    # The tenant receives only the query and its timestamp. request_id remains
    # as the idempotency/correlation key; every other field is internal.
    history_columns = _table_columns(cursor, search_history_table)
    obsolete_columns = history_columns.difference(
        {"id", "request_id", "query_text", "created_at"}
    )
    if obsolete_columns:
        _drop_indexes_for_columns(
            cursor,
            table_name=search_history_table,
            columns=obsolete_columns,
        )
        drop_columns = ", ".join(
            f"DROP COLUMN {quote_mysql_identifier(column)}"
            for column in sorted(obsolete_columns)
        )
        cursor.execute(f"ALTER TABLE {history} {drop_columns}")

    _add_index_if_missing(
        cursor,
        table_name=search_history_table,
        index_name="idx_search_created",
        definition="KEY idx_search_created (created_at)",
    )


def search_analytics_schema_status(
    config: MySQLRuntimeConfig,
    *,
    search_history_table: str,
    api_usage_table: str,
) -> dict[str, bool]:
    pymysql = require_pymysql()
    expected = {
        search_history_table: {
            "id",
            "request_id",
            "query_text",
            "created_at",
        },
        api_usage_table: {
            "id",
            "request_id",
            "company_id",
            "execution_path",
            "result_count",
            "total_results",
            "status",
            "api_call_count",
            "input_tokens",
            "output_tokens",
            "thought_tokens",
            "total_tokens",
            "duration_ms",
            "attempts_json",
            "plan_cache_hit",
            "result_cache_hit",
            "timings_json",
            "context_json",
            "created_at",
        },
    }
    with (
        mysql_connection(
            cursorclass=pymysql.cursors.DictCursor,
            config=config,
        ) as connection,
        connection.cursor() as cursor,
    ):
        placeholders = ", ".join("%s" for _ in expected)
        cursor.execute(
            f"""
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = DATABASE()
                  AND table_name IN ({placeholders})
                """,
            tuple(sorted(expected)),
        )
        present: dict[str, set[str]] = {}
        for row in cursor.fetchall():
            table_name = str(row.get("table_name") or row.get("TABLE_NAME") or "")
            column_name = str(row.get("column_name") or row.get("COLUMN_NAME") or "")
            if table_name and column_name:
                present.setdefault(table_name, set()).add(column_name)
    return {
        table: columns.issubset(present.get(table, set()))
        for table, columns in sorted(expected.items())
    }
