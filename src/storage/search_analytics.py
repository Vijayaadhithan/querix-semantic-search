from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from storage.mysql import (
    MySQLRuntimeConfig,
    mysql_connection,
    quote_mysql_identifier,
    require_pymysql,
)


LOGGER = logging.getLogger("uvicorn.error")
_STOP = object()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def mysql_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


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
    search_history_table: str,
    api_usage_table: str,
) -> None:
    history = quote_mysql_identifier(search_history_table)
    usage = quote_mysql_identifier(api_usage_table)
    pymysql = require_pymysql()
    with mysql_connection(
        cursorclass=pymysql.cursors.DictCursor,
        config=config,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {history} (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    request_id CHAR(32) CHARACTER SET ascii
                        COLLATE ascii_bin NOT NULL,
                    query_text TEXT NOT NULL,
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
                    attempt_number SMALLINT UNSIGNED NOT NULL,
                    provider VARCHAR(64) NOT NULL,
                    model VARCHAR(191) NOT NULL,
                    operation VARCHAR(64) NOT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'success',
                    api_calls SMALLINT UNSIGNED NOT NULL DEFAULT 0,
                    input_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    output_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    thought_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    total_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    duration_ms DECIMAL(14, 3) NOT NULL DEFAULT 0,
                    failure_reason VARCHAR(255) NOT NULL DEFAULT '',
                    created_at DATETIME(6) NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_api_usage_request_attempt (
                        request_id,
                        attempt_number
                    ),
                    KEY idx_usage_company_created (company_id, created_at),
                    KEY idx_usage_provider_created (
                        provider,
                        operation,
                        created_at
                    )
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            _migrate_search_analytics_schema(
                cursor,
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
            row.get("column_name")
            or row.get("COLUMN_NAME")
            or next(iter(row.values()))
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
        index_name = str(
            row.get("index_name")
            or row.get("INDEX_NAME")
            or ""
        )
        column_name = str(
            row.get("column_name")
            or row.get("COLUMN_NAME")
            or ""
        )
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
                f"ALTER TABLE {table} "
                f"DROP INDEX {quote_mysql_identifier(index_name)}"
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
    cursor.execute(
        f"ALTER TABLE {quote_mysql_identifier(table_name)} "
        f"ADD {definition}"
    )


def _migrate_search_analytics_schema(
    cursor,
    *,
    search_history_table: str,
    api_usage_table: str,
) -> None:
    """Reduce tenant history fields and detach internal usage rows safely."""
    history = quote_mysql_identifier(search_history_table)
    usage = quote_mysql_identifier(api_usage_table)
    usage_columns = _table_columns(cursor, api_usage_table)

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

    usage_columns = _table_columns(cursor, api_usage_table)
    if "search_history_id" in usage_columns:
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

        cursor.execute(
            """
            SELECT constraint_name
            FROM information_schema.key_column_usage
            WHERE table_schema = DATABASE()
              AND table_name = %s
              AND column_name = 'search_history_id'
              AND referenced_table_name IS NOT NULL
            """,
            (api_usage_table,),
        )
        for row in cursor.fetchall():
            constraint_name = str(
                row.get("constraint_name")
                or row.get("CONSTRAINT_NAME")
                or next(iter(row.values()))
            )
            cursor.execute(
                f"ALTER TABLE {usage} DROP FOREIGN KEY "
                f"{quote_mysql_identifier(constraint_name)}"
            )
        _drop_indexes_for_columns(
            cursor,
            table_name=api_usage_table,
            columns={"search_history_id"},
        )
        cursor.execute(
            f"""
            ALTER TABLE {usage}
            MODIFY COLUMN request_id CHAR(32) CHARACTER SET ascii
                COLLATE ascii_bin NOT NULL,
            MODIFY COLUMN company_id VARCHAR(63) NOT NULL,
            DROP COLUMN search_history_id
            """
        )

    _add_index_if_missing(
        cursor,
        table_name=api_usage_table,
        index_name="uq_api_usage_request_attempt",
        definition=(
            "UNIQUE KEY uq_api_usage_request_attempt "
            "(request_id, attempt_number)"
        ),
    )
    _add_index_if_missing(
        cursor,
        table_name=api_usage_table,
        index_name="idx_usage_company_created",
        definition=(
            "KEY idx_usage_company_created (company_id, created_at)"
        ),
    )

    tenant_only_columns = {
        "company_id",
        "user_id",
        "query_hash",
        "route_reason",
        "page_number",
        "filters_json",
        "result_cache_hit",
        "plan_cache_hit",
    }
    history_columns = _table_columns(cursor, search_history_table)
    obsolete_columns = tenant_only_columns.intersection(history_columns)
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
            "created_at",
        },
        api_usage_table: {
            "id",
            "request_id",
            "company_id",
            "attempt_number",
            "provider",
            "model",
            "operation",
            "status",
            "api_calls",
            "input_tokens",
            "output_tokens",
            "thought_tokens",
            "total_tokens",
            "duration_ms",
            "failure_reason",
            "created_at",
        },
    }
    with mysql_connection(
        cursorclass=pymysql.cursors.DictCursor,
        config=config,
    ) as connection:
        with connection.cursor() as cursor:
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
                table_name = str(
                    row.get("table_name") or row.get("TABLE_NAME") or ""
                )
                column_name = str(
                    row.get("column_name") or row.get("COLUMN_NAME") or ""
                )
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
                        created_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    ON DUPLICATE KEY UPDATE
                        id = LAST_INSERT_ID(id)
                    """,
                    (
                        event.request_id[:32],
                        normalized_query[:1000],
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
                        created_at,
                    ),
                )
                if event.api_usage:
                    cursor.executemany(
                        f"""
                        INSERT INTO {usage} (
                            request_id,
                            company_id,
                            attempt_number,
                            provider,
                            model,
                            operation,
                            status,
                            api_calls,
                            input_tokens,
                            output_tokens,
                            thought_tokens,
                            total_tokens,
                            duration_ms,
                            failure_reason,
                            created_at
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s,
                            %s
                        )
                        ON DUPLICATE KEY UPDATE
                            provider = VALUES(provider),
                            model = VALUES(model),
                            operation = VALUES(operation),
                            status = VALUES(status),
                            api_calls = VALUES(api_calls),
                            input_tokens = VALUES(input_tokens),
                            output_tokens = VALUES(output_tokens),
                            thought_tokens = VALUES(thought_tokens),
                            total_tokens = VALUES(total_tokens),
                            duration_ms = VALUES(duration_ms),
                            failure_reason = VALUES(failure_reason),
                            created_at = VALUES(created_at)
                        """,
                        [
                            (
                                event.request_id[:32],
                                event.company_id[:63],
                                attempt_number,
                                item.provider[:64],
                                item.model[:191],
                                item.operation[:64],
                                item.status[:32],
                                max(int(item.api_calls), 0),
                                max(int(item.input_tokens), 0),
                                max(int(item.output_tokens), 0),
                                max(int(item.thought_tokens), 0),
                                max(int(item.total_tokens), 0),
                                max(float(item.duration_ms), 0.0),
                                item.failure_reason[:255],
                                created_at,
                            )
                            for attempt_number, item in enumerate(
                                event.api_usage,
                                start=1,
                            )
                        ],
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
                            try:
                                connection.close()
                            except Exception:
                                pass
                            connection = None
                        if attempt == 0:
                            continue
                        with self._lock:
                            self._failed += 1
                        LOGGER.error(
                            "Search analytics write failed company=%s "
                            "error_type=%s",
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
                    try:
                        connection.close()
                    except Exception:
                        pass

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
