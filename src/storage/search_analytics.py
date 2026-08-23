from __future__ import annotations

import contextlib
import json
import logging
import math
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from storage.mysql import (
    MySQLRuntimeConfig,
    mysql_connection,
    quote_mysql_identifier,
    require_pymysql,
)

LOGGER = logging.getLogger("uvicorn.error")
_STOP = object()
SEARCH_ANALYTICS_TIMING_FIELDS = frozenset(
    {
        "total_server_ms",
        "planning_ms",
        "query_model_ms",
        "query_model_load_ms",
        "engine_total_ms",
        "result_cache_ms",
        "embedding_ms",
        "embedding_load_ms",
        "vector_search_ms",
        "bm25_search_ms",
        "retrieval_ms",
        "parallel_retrieval_ms",
        "fusion_ms",
        "type_lookup_ms",
        "reranker_load_ms",
        "reranking_ms",
        "related_tail_ms",
        "database_filter_ms",
        "eligibility_ms",
        "hydration_ms",
        "response_mapping_ms",
        "session_storage_ms",
        "usage_recording_ms",
        "recent_search_ms",
    }
)
SEARCH_ANALYTICS_CONTEXT_FIELDS = frozenset(
    {
        "main_category",
        "subcategory_id",
        "subcategory",
        "state",
        "city_id",
        "city",
        "locality_id",
        "locality",
        "rental_duration",
        "min_rental_fee",
        "max_rental_fee",
        "target_ad_type",
        "route_reason",
        "retrieved_candidates",
        "eligible_candidates",
        "hydrated_results",
        "returned_results",
    }
)
_NESTED_FILTER_FIELDS = frozenset(
    {
        "main_category",
        "main_category_id",
        "subcategory",
        "subcategory_id",
        "state",
        "city",
        "city_id",
        "locality",
        "locality_id",
        "rental_duration",
        "min_rental_fee",
        "max_rental_fee",
        "target_ad_type",
        "fee",
        "sort_by",
    }
)


def sanitize_search_analytics_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    sanitized = {}
    for name in SEARCH_ANALYTICS_CONTEXT_FIELDS:
        item = value.get(name)
        if isinstance(item, str):
            item = item.strip()[:191]
            if item:
                sanitized[name] = item
        elif (
            isinstance(item, bool)
            or isinstance(item, (int, float))
            and math.isfinite(float(item))
        ):
            sanitized[name] = item
    for container_name in ("explicit_filters", "inferred_filters"):
        container = value.get(container_name)
        if not isinstance(container, dict):
            continue
        nested = {}
        for name in _NESTED_FILTER_FIELDS:
            item = container.get(name)
            if isinstance(item, str):
                item = item.strip()[:191]
                if item:
                    nested[name] = item
            elif isinstance(item, bool) or (
                isinstance(item, (int, float)) and math.isfinite(float(item))
            ):
                nested[name] = item
            elif isinstance(item, (list, tuple)):
                safe_items = [
                    entry
                    for entry in item[:20]
                    if isinstance(entry, (str, int, float, bool))
                ]
                if safe_items:
                    nested[name] = safe_items
        if nested:
            sanitized[container_name] = nested
    ignored = value.get("ignored_filter_names")
    if isinstance(ignored, (list, tuple)):
        names = [str(name).strip()[:64] for name in ignored[:32] if str(name).strip()]
        if names:
            sanitized["ignored_filter_names"] = names
    return sanitized


def utc_now() -> datetime:
    return datetime.now(UTC)


def mysql_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class SearchApiUsageEvent:
    provider: str
    model: str
    operation: str
    status: str = "success"
    api_calls: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0
    total_tokens: int = 0
    duration_ms: float = 0.0
    failure_reason: str = ""


@dataclass(frozen=True)
class SearchAnalyticsEvent:
    company_id: str
    query_text: str
    execution_path: str
    duration_ms: float
    result_count: int
    total_results: int
    status: str = "success"
    api_usage: tuple[SearchApiUsageEvent, ...] = ()
    plan_cache_hit: bool | None = None
    result_cache_hit: bool | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=utc_now)

    @property
    def api_call_count(self) -> int:
        return sum(max(int(item.api_calls), 0) for item in self.api_usage)

    @property
    def input_tokens(self) -> int:
        return sum(max(int(item.input_tokens), 0) for item in self.api_usage)

    @property
    def output_tokens(self) -> int:
        return sum(max(int(item.output_tokens), 0) for item in self.api_usage)

    @property
    def thought_tokens(self) -> int:
        return sum(max(int(item.thought_tokens), 0) for item in self.api_usage)

    @property
    def total_tokens(self) -> int:
        return sum(max(int(item.total_tokens), 0) for item in self.api_usage)


class SearchAnalyticsStore(Protocol):
    def submit(self, event: SearchAnalyticsEvent) -> bool: ...

    def status(self) -> dict[str, Any]: ...

    def close(self, timeout_seconds: float = 5.0) -> None: ...


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


def write_search_analytics_events(
    connection,
    events: list[SearchAnalyticsEvent] | tuple[SearchAnalyticsEvent, ...],
    *,
    search_history_table: str,
    api_usage_table: str,
) -> int:
    """Atomically insert an idempotent batch into the tenant's MySQL DB."""
    if not events:
        return 0
    history = quote_mysql_identifier(search_history_table)
    usage = quote_mysql_identifier(api_usage_table)
    connection.begin()
    try:
        with connection.cursor() as cursor:
            for event in events:
                created_at = mysql_utc(event.created_at)
                normalized_query = " ".join(event.query_text.split())
                cursor.execute(
                    f"""
                    INSERT INTO {history} (
                        request_id,
                        query_text,
                        created_at
                    )
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        id = LAST_INSERT_ID(id)
                    """,
                    (
                        event.request_id[:32],
                        normalized_query[:1000],
                        created_at,
                    ),
                )
                attempts_json = json.dumps(
                    [
                        {
                            "attempt_number": attempt_number,
                            "provider": item.provider[:64],
                            "model": item.model[:191],
                            "operation": item.operation[:64],
                            "status": item.status[:32],
                            "api_calls": max(int(item.api_calls), 0),
                            "input_tokens": max(int(item.input_tokens), 0),
                            "output_tokens": max(int(item.output_tokens), 0),
                            "thought_tokens": max(
                                int(item.thought_tokens),
                                0,
                            ),
                            "total_tokens": max(int(item.total_tokens), 0),
                            "duration_ms": max(
                                float(item.duration_ms),
                                0.0,
                            ),
                            "failure_reason": item.failure_reason[:255],
                        }
                        for attempt_number, item in enumerate(
                            event.api_usage,
                            start=1,
                        )
                    ],
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                timings: dict[str, float] = {}
                for name, value in event.timings_ms.items():
                    if name not in SEARCH_ANALYTICS_TIMING_FIELDS:
                        continue
                    try:
                        measured = float(value)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(measured) and measured >= 0:
                        timings[name] = round(measured, 3)
                timings_json = json.dumps(
                    timings,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                context_json = json.dumps(
                    sanitize_search_analytics_context(event.context),
                    separators=(",", ":"),
                    ensure_ascii=False,
                    default=str,
                )
                cursor.execute(
                    f"""
                    INSERT INTO {usage} (
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
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s
                    )
                    ON DUPLICATE KEY UPDATE
                        company_id = VALUES(company_id),
                        execution_path = VALUES(execution_path),
                        result_count = VALUES(result_count),
                        total_results = VALUES(total_results),
                        status = VALUES(status),
                        api_call_count = VALUES(api_call_count),
                        input_tokens = VALUES(input_tokens),
                        output_tokens = VALUES(output_tokens),
                        thought_tokens = VALUES(thought_tokens),
                        total_tokens = VALUES(total_tokens),
                        duration_ms = VALUES(duration_ms),
                        attempts_json = VALUES(attempts_json),
                        plan_cache_hit = VALUES(plan_cache_hit),
                        result_cache_hit = VALUES(result_cache_hit),
                        timings_json = VALUES(timings_json),
                        context_json = VALUES(context_json),
                        created_at = VALUES(created_at)
                    """,
                    (
                        event.request_id[:32],
                        event.company_id[:63],
                        event.execution_path[:32],
                        max(int(event.result_count), 0),
                        max(int(event.total_results), 0),
                        event.status[:32],
                        event.api_call_count,
                        event.input_tokens,
                        event.output_tokens,
                        event.thought_tokens,
                        event.total_tokens,
                        max(float(event.duration_ms), 0.0),
                        attempts_json,
                        event.plan_cache_hit,
                        event.result_cache_hit,
                        timings_json,
                        context_json,
                        created_at,
                    ),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return len(events)


class MySQLSearchAnalyticsStore:
    """Best-effort ordered MySQL writer kept off the request latency path."""

    def __init__(
        self,
        config: MySQLRuntimeConfig,
        *,
        company_id: str,
        search_history_table: str = "semantic_search_history",
        api_usage_table: str = "semantic_search_api_usage",
        queue_capacity: int = 1000,
    ):
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be greater than zero")
        self.config = config
        self.company_id = company_id
        self.search_history_table = search_history_table
        self.api_usage_table = api_usage_table
        self._queue: queue.Queue = queue.Queue(maxsize=queue_capacity)
        self._closed = False
        self._lock = threading.Lock()
        self._submitted = 0
        self._written = 0
        self._failed = 0
        self._dropped = 0
        self._worker = threading.Thread(
            target=self._run,
            name=f"search-analytics-{company_id}",
            daemon=True,
        )
        self._worker.start()

    def submit(self, event: SearchAnalyticsEvent) -> bool:
        if event.company_id != self.company_id:
            LOGGER.error(
                "Search analytics tenant mismatch store=%s event=%s",
                self.company_id,
                event.company_id,
            )
            return False
        with self._lock:
            if self._closed:
                return False
            self._submitted += 1
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._lock:
                self._dropped += 1
            LOGGER.warning(
                "Search analytics queue full company=%s; dropping event",
                self.company_id,
            )
            return False
        return True

    def status(self) -> dict[str, int]:
        with self._lock:
            return {
                "submitted": self._submitted,
                "written": self._written,
                "failed": self._failed,
                "dropped": self._dropped,
                "queued": self._queue.qsize(),
            }

    def _connect(self):
        pymysql = require_pymysql()
        return mysql_connection(
            cursorclass=pymysql.cursors.DictCursor,
            config=self.config,
        )

    def _run(self) -> None:
        connection = None
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                for attempt in range(2):
                    try:
                        if connection is None or not connection.open:
                            connection = self._connect()
                        else:
                            connection.ping(reconnect=True)
                        self._write(connection, item)
                    except Exception as exc:
                        if connection is not None:
                            with contextlib.suppress(Exception):
                                connection.close()
                            connection = None
                        if attempt == 0:
                            continue
                        with self._lock:
                            self._failed += 1
                        LOGGER.error(
                            "Search analytics write failed company=%s error_type=%s",
                            self.company_id,
                            type(exc).__name__,
                        )
                    else:
                        with self._lock:
                            self._written += 1
                        break
            finally:
                self._queue.task_done()
                if item is _STOP and connection is not None:
                    with contextlib.suppress(Exception):
                        connection.close()

    def _write(self, connection, event: SearchAnalyticsEvent) -> None:
        write_search_analytics_events(
            connection,
            [event],
            search_history_table=self.search_history_table,
            api_usage_table=self.api_usage_table,
        )

    def flush(self, timeout_seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self, timeout_seconds: float = 5.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.flush(timeout_seconds)
        try:
            self._queue.put(_STOP, timeout=max(timeout_seconds, 0.1))
        except queue.Full:
            LOGGER.warning(
                "Search analytics worker did not drain company=%s",
                self.company_id,
            )
            return
        self._worker.join(timeout=max(timeout_seconds, 0.1))
