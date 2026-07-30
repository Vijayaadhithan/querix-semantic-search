from __future__ import annotations

import base64
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _json_default(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value)


def json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )


def encode_query_cursor(created_at: str, request_id: str) -> str:
    payload = json_dumps(
        {"v": 1, "created_at": created_at, "request_id": request_id}
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_query_cursor(cursor: str) -> tuple[str, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(cursor + padding).decode()
        )
        if payload.get("v") != 1:
            raise ValueError("Unsupported cursor version")
        created_at = str(payload["created_at"])
        request_id = str(payload["request_id"])
        if not created_at or not request_id:
            raise ValueError("Incomplete cursor")
        return created_at, request_id
    except Exception as exc:
        raise ValueError("Invalid analytics query cursor") from exc


class AnalyticsSnapshotStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS analytics_snapshots (
                    company_id TEXT PRIMARY KEY,
                    active_version TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    source_watermark TEXT,
                    source_rows_json TEXT NOT NULL,
                    company_dashboard_json TEXT NOT NULL,
                    internal_dashboard_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS analytics_query_records (
                    company_id TEXT NOT NULL,
                    snapshot_version TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    query_text TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    categories_text TEXT NOT NULL,
                    language TEXT NOT NULL,
                    company_json TEXT NOT NULL,
                    internal_json TEXT NOT NULL,
                    PRIMARY KEY (
                        company_id,
                        snapshot_version,
                        request_id
                    )
                );

                CREATE INDEX IF NOT EXISTS idx_analytics_queries_page
                ON analytics_query_records (
                    company_id,
                    snapshot_version,
                    created_at DESC,
                    request_id DESC
                );

                CREATE INDEX IF NOT EXISTS idx_analytics_queries_outcome
                ON analytics_query_records (
                    company_id,
                    snapshot_version,
                    outcome,
                    created_at DESC
                );

                CREATE TABLE IF NOT EXISTS analytics_refresh_runs (
                    run_id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error_type TEXT,
                    source_rows_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_analytics_runs_company
                ON analytics_refresh_runs (
                    company_id,
                    started_at DESC
                );
                """
            )

    def begin_refresh(self, company_id: str) -> str:
        run_id = uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO analytics_refresh_runs (
                    run_id,
                    company_id,
                    status,
                    started_at
                )
                VALUES (?, ?, 'running', ?)
                """,
                (run_id, company_id, utc_now_iso()),
            )
        return run_id

    def fail_refresh(
        self,
        run_id: str,
        *,
        error_type: str,
        source_rows: dict[str, int] | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE analytics_refresh_runs
                SET status = 'failed',
                    completed_at = ?,
                    error_type = ?,
                    source_rows_json = ?
                WHERE run_id = ?
                """,
                (
                    utc_now_iso(),
                    error_type[:191],
                    json_dumps(source_rows or {}),
                    run_id,
                ),
            )

    def publish(
        self,
        *,
        run_id: str,
        company_id: str,
        generated_at: str,
        source_watermark: str | None,
        source_rows: dict[str, int],
        company_dashboard: dict[str, Any],
        internal_dashboard: dict[str, Any],
        query_records: list[tuple[dict[str, Any], dict[str, Any]]],
    ) -> str:
        version = uuid.uuid4().hex
        query_values = []
        for company_record, internal_record in query_records:
            categories = [
                str(value)
                for value in company_record.get("categories", [])
            ]
            query_values.append(
                (
                    company_id,
                    version,
                    str(company_record.get("request_id") or ""),
                    str(company_record.get("created_at") or ""),
                    str(company_record.get("query") or ""),
                    str(company_record.get("outcome") or "unknown"),
                    f"|{'|'.join(categories)}|",
                    str(company_record.get("language") or "Unknown"),
                    json_dumps(company_record),
                    json_dumps(internal_record),
                )
            )
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                previous = connection.execute(
                    """
                    SELECT active_version
                    FROM analytics_snapshots
                    WHERE company_id = ?
                    """,
                    (company_id,),
                ).fetchone()
                connection.executemany(
                    """
                    INSERT INTO analytics_query_records (
                        company_id,
                        snapshot_version,
                        request_id,
                        created_at,
                        query_text,
                        outcome,
                        categories_text,
                        language,
                        company_json,
                        internal_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    query_values,
                )
                connection.execute(
                    """
                    INSERT INTO analytics_snapshots (
                        company_id,
                        active_version,
                        generated_at,
                        source_watermark,
                        source_rows_json,
                        company_dashboard_json,
                        internal_dashboard_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(company_id) DO UPDATE SET
                        active_version = excluded.active_version,
                        generated_at = excluded.generated_at,
                        source_watermark = excluded.source_watermark,
                        source_rows_json = excluded.source_rows_json,
                        company_dashboard_json =
                            excluded.company_dashboard_json,
                        internal_dashboard_json =
                            excluded.internal_dashboard_json
                    """,
                    (
                        company_id,
                        version,
                        generated_at,
                        source_watermark,
                        json_dumps(source_rows),
                        json_dumps(company_dashboard),
                        json_dumps(internal_dashboard),
                    ),
                )
                connection.execute(
                    """
                    UPDATE analytics_refresh_runs
                    SET status = 'complete',
                        completed_at = ?,
                        source_rows_json = ?
                    WHERE run_id = ?
                    """,
                    (utc_now_iso(), json_dumps(source_rows), run_id),
                )
                if previous is not None:
                    connection.execute(
                        """
                        DELETE FROM analytics_query_records
                        WHERE company_id = ?
                          AND snapshot_version = ?
                        """,
                        (company_id, previous["active_version"]),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return version

    def dashboard(
        self,
        company_id: str,
        *,
        internal: bool,
    ) -> dict[str, Any] | None:
        field = (
            "internal_dashboard_json"
            if internal
            else "company_dashboard_json"
        )
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT generated_at, source_watermark, source_rows_json, {field}
                FROM analytics_snapshots
                WHERE company_id = ?
                """,
                (company_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[field])
        payload["snapshot"] = {
            "generated_at": row["generated_at"],
            "source_watermark": row["source_watermark"],
            "source_rows": json.loads(row["source_rows_json"]),
            "refresh_schedule": "daily at 03:00 Asia/Kolkata",
        }
        return payload

    def query_records(
        self,
        company_id: str,
        *,
        internal: bool,
        limit: int,
        cursor: str | None = None,
        query: str | None = None,
        outcome: str | None = None,
        category: str | None = None,
        language: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
    ) -> dict[str, Any]:
        field = "internal_json" if internal else "company_json"
        clauses = [
            "records.company_id = ?",
            "records.snapshot_version = snapshots.active_version",
        ]
        values: list[Any] = [company_id]
        if cursor:
            cursor_created, cursor_request = decode_query_cursor(cursor)
            clauses.append(
                """
                (
                    records.created_at < ?
                    OR (
                        records.created_at = ?
                        AND records.request_id < ?
                    )
                )
                """
            )
            values.extend(
                [cursor_created, cursor_created, cursor_request]
            )
        if query:
            clauses.append(
                "LOWER(records.query_text) LIKE ? ESCAPE '\\'"
            )
            escaped = (
                query.casefold()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            values.append(f"%{escaped}%")
        if outcome:
            clauses.append("records.outcome = ?")
            values.append(outcome)
        if category:
            clauses.append("records.categories_text LIKE ?")
            values.append(f"%|{category}|%")
        if language:
            clauses.append("records.language = ?")
            values.append(language)
        if created_from:
            clauses.append("records.created_at >= ?")
            values.append(created_from)
        if created_to:
            clauses.append("records.created_at <= ?")
            values.append(created_to)
        values.append(limit + 1)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    records.request_id,
                    records.created_at,
                    records.{field} AS payload_json
                FROM analytics_query_records AS records
                INNER JOIN analytics_snapshots AS snapshots
                    ON snapshots.company_id = records.company_id
                WHERE {' AND '.join(clauses)}
                ORDER BY records.created_at DESC, records.request_id DESC
                LIMIT ?
                """,
                tuple(values),
            ).fetchall()
        has_more = len(rows) > limit
        visible = rows[:limit]
        next_cursor = (
            encode_query_cursor(
                visible[-1]["created_at"],
                visible[-1]["request_id"],
            )
            if has_more and visible
            else None
        )
        return {
            "company_id": company_id,
            "items": [
                json.loads(row["payload_json"]) for row in visible
            ],
            "returned": len(visible),
            "has_more": has_more,
            "next_cursor": next_cursor,
        }

    def company_status(self, company_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            snapshot = connection.execute(
                """
                SELECT generated_at, source_watermark, source_rows_json
                FROM analytics_snapshots
                WHERE company_id = ?
                """,
                (company_id,),
            ).fetchone()
            run = connection.execute(
                """
                SELECT
                    run_id,
                    status,
                    started_at,
                    completed_at,
                    error_type,
                    source_rows_json
                FROM analytics_refresh_runs
                WHERE company_id = ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (company_id,),
            ).fetchone()
        return {
            "company_id": company_id,
            "has_snapshot": snapshot is not None,
            "snapshot": (
                {
                    "generated_at": snapshot["generated_at"],
                    "source_watermark": snapshot["source_watermark"],
                    "source_rows": json.loads(
                        snapshot["source_rows_json"]
                    ),
                }
                if snapshot is not None
                else None
            ),
            "latest_run": (
                {
                    key: (
                        json.loads(run[key])
                        if key == "source_rows_json" and run[key]
                        else run[key]
                    )
                    for key in run.keys()
                }
                if run is not None
                else None
            ),
            "refresh_schedule": "daily at 03:00 Asia/Kolkata",
        }
