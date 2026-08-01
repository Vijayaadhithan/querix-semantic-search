from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from storage.mysql import MySQLRuntimeConfig, mysql_connection, require_pymysql
from storage.search_analytics import (
    SEARCH_ANALYTICS_TIMING_FIELDS,
    SearchAnalyticsEvent,
    SearchApiUsageEvent,
    write_search_analytics_events,
)

LOGGER = logging.getLogger("uvicorn.error")
_STOP = object()
_SCHEMA_VERSION = 3


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def serialize_search_analytics_event(event: SearchAnalyticsEvent) -> str:
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "request_id": event.request_id,
        "company_id": event.company_id,
        "query_text": event.query_text,
        "execution_path": event.execution_path,
        "result_count": event.result_count,
        "total_results": event.total_results,
        "status": event.status,
        "duration_ms": event.duration_ms,
        "plan_cache_hit": event.plan_cache_hit,
        "result_cache_hit": event.result_cache_hit,
        "timings_ms": {
            name: value
            for name, value in event.timings_ms.items()
            if name in SEARCH_ANALYTICS_TIMING_FIELDS
        },
        "created_at": _utc_iso(event.created_at),
        "api_usage": [
            {
                "provider": item.provider,
                "model": item.model,
                "operation": item.operation,
                "status": item.status,
                "api_calls": item.api_calls,
                "input_tokens": item.input_tokens,
                "output_tokens": item.output_tokens,
                "thought_tokens": item.thought_tokens,
                "total_tokens": item.total_tokens,
                "duration_ms": item.duration_ms,
                "failure_reason": item.failure_reason,
            }
            for item in event.api_usage
        ],
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def deserialize_search_analytics_event(
    payload_json: str,
) -> SearchAnalyticsEvent:
    payload = json.loads(payload_json)
    if int(payload.get("schema_version", 0)) not in {1, 2, _SCHEMA_VERSION}:
        raise ValueError("Unsupported search analytics spool schema")
    return SearchAnalyticsEvent(
        request_id=str(payload["request_id"]),
        company_id=str(payload["company_id"]),
        query_text=str(payload["query_text"]),
        execution_path=str(payload["execution_path"]),
        result_count=int(payload.get("result_count", 0)),
        total_results=int(payload.get("total_results", 0)),
        status=str(payload.get("status") or "success"),
        duration_ms=float(payload.get("duration_ms", 0.0)),
        plan_cache_hit=payload.get("plan_cache_hit"),
        result_cache_hit=payload.get("result_cache_hit"),
        timings_ms={
            str(name): float(value)
            for name, value in (payload.get("timings_ms") or {}).items()
        },
        created_at=datetime.fromisoformat(str(payload["created_at"])),
        api_usage=tuple(
            SearchApiUsageEvent(
                provider=str(item.get("provider") or ""),
                model=str(item.get("model") or ""),
                operation=str(item.get("operation") or ""),
                status=str(item.get("status") or "success"),
                api_calls=int(item.get("api_calls", 0)),
                input_tokens=int(item.get("input_tokens", 0)),
                output_tokens=int(item.get("output_tokens", 0)),
                thought_tokens=int(item.get("thought_tokens", 0)),
                total_tokens=int(item.get("total_tokens", 0)),
                duration_ms=float(item.get("duration_ms", 0.0)),
                failure_reason=str(item.get("failure_reason") or ""),
            )
            for item in payload.get("api_usage") or []
        ),
    )


def _connect_spool(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_database = not path.exists()
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    if new_database:
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS search_analytics_spool (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            company_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            spooled_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_search_spool_company_id
        ON search_analytics_spool (company_id, id)
        """
    )
    connection.commit()
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return connection


@contextmanager
def _spool_connection(path: Path):
    connection = _connect_spool(path)
    try:
        yield connection
    finally:
        connection.close()


def initialize_search_analytics_spool(path: Path) -> None:
    with _spool_connection(path):
        pass


def search_analytics_spool_status(
    path: Path,
    *,
    company_id: str,
) -> dict[str, int]:
    path = Path(path)
    if not path.exists():
        return {"pending": 0, "spool_bytes": 0}
    with _spool_connection(path) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS pending
            FROM search_analytics_spool
            WHERE company_id = ?
            """,
            (company_id,),
        ).fetchone()
    related_paths = (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    )
    return {
        "pending": int(row["pending"]),
        "spool_bytes": sum(
            item.stat().st_size for item in related_paths if item.exists()
        ),
    }


class SQLiteSearchAnalyticsSpoolStore:
    """Asynchronously persists analytics locally for scheduled delivery."""

    def __init__(
        self,
        path: Path,
        *,
        company_id: str,
        queue_capacity: int = 1000,
    ):
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be greater than zero")
        self.path = Path(path)
        self.company_id = company_id
        initialize_search_analytics_spool(self.path)
        self._queue: queue.Queue = queue.Queue(maxsize=queue_capacity)
        self._closed = False
        self._lock = threading.Lock()
        self._submitted = 0
        self._spooled = 0
        self._failed = 0
        self._dropped = 0
        self._worker = threading.Thread(
            target=self._run,
            name=f"search-analytics-spool-{company_id}",
            daemon=True,
        )
        self._worker.start()

    def submit(self, event: SearchAnalyticsEvent) -> bool:
        if event.company_id != self.company_id:
            LOGGER.error(
                "Search analytics spool tenant mismatch store=%s event=%s",
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
                "Search analytics spool queue full company=%s; "
                "dropping event",
                self.company_id,
            )
            return False
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            counters = {
                "mode": "daily_spool",
                "submitted": self._submitted,
                "spooled": self._spooled,
                "failed": self._failed,
                "dropped": self._dropped,
                "queued": self._queue.qsize(),
            }
        try:
            disk = search_analytics_spool_status(
                self.path,
                company_id=self.company_id,
            )
        except sqlite3.Error:
            disk = {"pending": -1, "spool_bytes": -1}
        return {**counters, **disk}

    def _run(self) -> None:
        connection = _connect_spool(self.path)
        try:
            while True:
                item = self._queue.get()
                try:
                    if item is _STOP:
                        return
                    payload_json = serialize_search_analytics_event(item)
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO search_analytics_spool (
                            request_id,
                            company_id,
                            payload_json,
                            created_at,
                            spooled_at
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            item.request_id,
                            item.company_id,
                            payload_json,
                            _utc_iso(item.created_at),
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                    connection.commit()
                except Exception as exc:
                    connection.rollback()
                    with self._lock:
                        self._failed += 1
                    LOGGER.error(
                        "Search analytics local spool write failed "
                        "company=%s error_type=%s",
                        self.company_id,
                        type(exc).__name__,
                    )
                else:
                    with self._lock:
                        self._spooled += 1
                finally:
                    self._queue.task_done()
        finally:
            connection.close()

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
                "Search analytics spool worker did not drain company=%s",
                self.company_id,
            )
            return
        self._worker.join(timeout=max(timeout_seconds, 0.1))


def _reclaim_spool_space(path: Path) -> None:
    try:
        with _spool_connection(path) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA incremental_vacuum")
    except sqlite3.Error as exc:
        LOGGER.warning(
            "Search analytics spool cleanup deferred error_type=%s",
            type(exc).__name__,
        )


def deliver_search_analytics_spool(
    path: Path,
    config: MySQLRuntimeConfig,
    *,
    company_id: str,
    search_history_table: str,
    api_usage_table: str,
    batch_size: int = 500,
) -> dict[str, int]:
    """Deliver a stable spool snapshot, then delete only committed rows."""
    if batch_size <= 0 or batch_size > 5000:
        raise ValueError("batch_size must be between 1 and 5000")
    path = Path(path)
    initialize_search_analytics_spool(path)
    with _spool_connection(path) as spool:
        cutoff_row = spool.execute(
            """
            SELECT COALESCE(MAX(id), 0) AS cutoff_id
            FROM search_analytics_spool
            WHERE company_id = ?
            """,
            (company_id,),
        ).fetchone()
    cutoff_id = int(cutoff_row["cutoff_id"])
    if cutoff_id == 0:
        _reclaim_spool_space(path)
        status = search_analytics_spool_status(
            path,
            company_id=company_id,
        )
        return {
            "selected": 0,
            "uploaded": 0,
            "deleted": 0,
            **status,
        }

    pymysql = require_pymysql()
    uploaded = 0
    deleted = 0
    with mysql_connection(
        cursorclass=pymysql.cursors.DictCursor,
        config=config,
    ) as destination:
        while True:
            with _spool_connection(path) as spool:
                rows = spool.execute(
                    """
                    SELECT id, payload_json
                    FROM search_analytics_spool
                    WHERE company_id = ?
                      AND id <= ?
                    ORDER BY id
                    LIMIT ?
                    """,
                    (company_id, cutoff_id, batch_size),
                ).fetchall()
            if not rows:
                break
            events = [
                deserialize_search_analytics_event(row["payload_json"])
                for row in rows
            ]
            if any(event.company_id != company_id for event in events):
                raise ValueError(
                    "Search analytics spool contains a tenant mismatch"
                )
            write_search_analytics_events(
                destination,
                events,
                search_history_table=search_history_table,
                api_usage_table=api_usage_table,
            )
            uploaded += len(events)
            row_ids = [int(row["id"]) for row in rows]
            placeholders = ", ".join("?" for _ in row_ids)
            with _spool_connection(path) as spool:
                cursor = spool.execute(
                    f"""
                    DELETE FROM search_analytics_spool
                    WHERE id IN ({placeholders})
                    """,
                    row_ids,
                )
                spool.commit()
                deleted += max(int(cursor.rowcount), 0)

    _reclaim_spool_space(path)
    status = search_analytics_spool_status(path, company_id=company_id)
    return {
        "selected": uploaded,
        "uploaded": uploaded,
        "deleted": deleted,
        **status,
    }
