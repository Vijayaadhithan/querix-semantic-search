from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

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
    user_id: str | None = None
    route_reason: str = ""
    page_number: int = 1
    filters: dict[str, Any] = field(default_factory=dict)
    status: str = "success"
    result_cache_hit: bool = False
    plan_cache_hit: bool = False
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
                    company_id VARCHAR(63) NOT NULL,
                    user_id VARCHAR(128) NULL,
                    query_text TEXT NOT NULL,
                    query_hash BINARY(32) NOT NULL,
                    execution_path VARCHAR(32) NOT NULL,
                    route_reason VARCHAR(191) NOT NULL DEFAULT '',
                    page_number INT UNSIGNED NOT NULL DEFAULT 1,
                    filters_json LONGTEXT NOT NULL,
                    result_count INT UNSIGNED NOT NULL DEFAULT 0,
                    total_results INT UNSIGNED NOT NULL DEFAULT 0,
                    status VARCHAR(32) NOT NULL DEFAULT 'success',
                    result_cache_hit TINYINT(1) NOT NULL DEFAULT 0,
                    plan_cache_hit TINYINT(1) NOT NULL DEFAULT 0,
                    api_call_count INT UNSIGNED NOT NULL DEFAULT 0,
                    input_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    output_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    thought_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    total_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    duration_ms DECIMAL(14, 3) NOT NULL DEFAULT 0,
                    created_at DATETIME(6) NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_search_request_id (request_id),
                    KEY idx_search_company_created (company_id, created_at),
                    KEY idx_search_user_created (user_id, created_at),
                    KEY idx_search_query_hash_created (query_hash, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {usage} (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    search_history_id BIGINT UNSIGNED NOT NULL,
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
                    KEY idx_usage_search (search_history_id),
                    KEY idx_usage_provider_created (
                        provider,
                        operation,
                        created_at
                    ),
                    FOREIGN KEY (search_history_id)
                        REFERENCES {history} (id)
                        ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )


def search_analytics_schema_status(
    config: MySQLRuntimeConfig,
    *,
    search_history_table: str,
    api_usage_table: str,
) -> dict[str, bool]:
    pymysql = require_pymysql()
    expected = {search_history_table, api_usage_table}
    with mysql_connection(
        cursorclass=pymysql.cursors.DictCursor,
        config=config,
    ) as connection:
        with connection.cursor() as cursor:
            placeholders = ", ".join("%s" for _ in expected)
            cursor.execute(
                f"""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name IN ({placeholders})
                """,
                tuple(sorted(expected)),
            )
            present = {
                str(
                    row.get("table_name")
                    or row.get("TABLE_NAME")
                    or next(iter(row.values()))
                )
                for row in cursor.fetchall()
            }
    return {table: table in present for table in sorted(expected)}


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
        history = quote_mysql_identifier(self.search_history_table)
        usage = quote_mysql_identifier(self.api_usage_table)
        created_at = mysql_utc(event.created_at)
        normalized_query = " ".join(event.query_text.split())
        query_hash = hashlib.sha256(
            normalized_query.casefold().encode("utf-8")
        ).digest()
        filters_json = json.dumps(
            event.filters,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            ensure_ascii=False,
        )
        connection.begin()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO {history} (
                        request_id,
                        company_id,
                        user_id,
                        query_text,
                        query_hash,
                        execution_path,
                        route_reason,
                        page_number,
                        filters_json,
                        result_count,
                        total_results,
                        status,
                        result_cache_hit,
                        plan_cache_hit,
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
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        event.request_id[:32],
                        event.company_id[:63],
                        event.user_id[:128] if event.user_id else None,
                        normalized_query[:1000],
                        query_hash,
                        event.execution_path[:32],
                        event.route_reason[:191],
                        max(int(event.page_number), 1),
                        filters_json,
                        max(int(event.result_count), 0),
                        max(int(event.total_results), 0),
                        event.status[:32],
                        int(bool(event.result_cache_hit)),
                        int(bool(event.plan_cache_hit)),
                        event.api_call_count,
                        event.input_tokens,
                        event.output_tokens,
                        event.thought_tokens,
                        event.total_tokens,
                        max(float(event.duration_ms), 0.0),
                        created_at,
                    ),
                )
                search_history_id = int(cursor.lastrowid)
                if event.api_usage:
                    cursor.executemany(
                        f"""
                        INSERT INTO {usage} (
                            search_history_id,
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
                            %s, %s, %s, %s, %s, %s, %s
                        )
                        """,
                        [
                            (
                                search_history_id,
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
