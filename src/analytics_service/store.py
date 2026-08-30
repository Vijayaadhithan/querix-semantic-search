from __future__ import annotations

import base64
import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .schedule import REFRESH_SCHEDULE

LOGGER = logging.getLogger(__name__)
STALE_REFRESH_AFTER = timedelta(hours=6)


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
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode())
        if payload.get("v") != 1:
            raise ValueError("Unsupported cursor version")
        created_at = str(payload["created_at"])
        request_id = str(payload["request_id"])
        if not created_at or not request_id:
            raise ValueError("Incomplete cursor")
        return created_at, request_id
    except Exception as exc:
        raise ValueError("Invalid analytics query cursor") from exc


def encode_query_sort_cursor(sort_by: str, sort_direction: str, offset: int) -> str:
    payload = json_dumps(
        {
            "v": 2,
            "sort_by": sort_by,
            "sort_direction": sort_direction,
            "offset": offset,
        }
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_query_sort_cursor(cursor: str) -> tuple[str, str, int]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode())
        if payload.get("v") != 2:
            raise ValueError("Unsupported cursor version")
        sort_by = str(payload["sort_by"])
        sort_direction = str(payload["sort_direction"])
        offset = int(payload["offset"])
        if not sort_by or sort_direction not in {"asc", "desc"} or offset < 0:
            raise ValueError("Incomplete cursor")
        return sort_by, sort_direction, offset
    except Exception as exc:
        raise ValueError("Invalid analytics query cursor") from exc


def _query_sort_expression(sort_by: str, *, internal: bool) -> str:
    common = {
        "created_at": "records.created_at",
        "outcome": "records.outcome",
        "results": (
            "json_extract(records.internal_json, '$.api.result_count')"
            if internal
            else "json_extract(records.company_json, '$.search.result_count')"
        ),
    }
    internal_only = {
        "execution_path": (
            "NULLIF(json_extract(records.internal_json, "
            "'$.performance.execution_path'), '')"
        ),
        "duration": (
            "json_extract(records.internal_json, "
            "'$.performance.total_server_duration_ms')"
        ),
        "tokens": "json_extract(records.internal_json, '$.token_usage.total_tokens')",
    }
    expressions = {**common, **(internal_only if internal else {})}
    try:
        return expressions[sort_by]
    except KeyError as exc:
        allowed = ", ".join(expressions)
        raise ValueError(f"Query sort field must be one of: {allowed}") from exc


class AnalyticsSnapshotStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._facet_cache: dict[tuple[str, str, bool], dict[str, Any]] = {}
        self._initialize()
        interrupted = self.reconcile_stale_refreshes()
        if interrupted:
            LOGGER.warning(
                "Reconciled stale analytics refresh runs status=interrupted count=%d",
                interrupted,
            )

    @staticmethod
    def _dashboard_activity_record(
        payload_json: str,
        *,
        internal: bool,
    ) -> dict[str, Any]:
        """Return only the fields used to calculate dashboard activity views.

        Query Explorer responses retain the complete, audience-specific payload in
        SQLite. A large nested JSON document becomes many Python objects, so this
        short-lived projection preserves the dashboard/filter contract without
        carrying query text, enrichment, flags, or unused telemetry through the
        aggregation step.
        """
        payload = json.loads(payload_json)
        filters = dict(payload.get("filters") or {})
        record: dict[str, Any] = {
            "created_at": payload.get("created_at"),
            "normalized_query": payload.get("normalized_query"),
            "request_kind": payload.get("request_kind"),
            "outcome": payload.get("outcome"),
            "categories": list(payload.get("categories") or ()),
            "language": payload.get("language"),
            "filters": {
                name: filters[name]
                for name in (
                    "main_category",
                    "subcategory",
                    "city",
                    "city_id",
                    "target_ad_type",
                )
                if name in filters
            },
        }
        if not internal:
            search = dict(payload.get("search") or {})
            record["search"] = {
                "result_count": search.get("result_count"),
                "total_results": search.get("total_results"),
            }
            return record

        api = dict(payload.get("api") or {})
        performance = dict(payload.get("performance") or {})
        cache = dict(performance.get("cache") or {})
        record["api"] = {"status": api.get("status")}
        record["performance"] = {
            "execution_path": performance.get("execution_path"),
            "total_server_duration_ms": performance.get("total_server_duration_ms"),
            "downstream_api_calls": performance.get("downstream_api_calls"),
            "cache": {
                name: cache[name]
                for name in ("plan_hit", "result_hit")
                if cache.get(name) is not None
            },
            "stages_ms": {
                str(name): value
                for name, value in dict(performance.get("stages_ms") or {}).items()
                if isinstance(value, (int, float))
            },
        }
        record["attempts"] = [
            {
                name: attempt.get(name)
                for name in (
                    "provider",
                    "operation",
                    "api_calls",
                    "input_tokens",
                    "output_tokens",
                    "thought_tokens",
                    "total_tokens",
                )
            }
            for attempt in payload.get("attempts") or ()
            if isinstance(attempt, dict)
        ]
        diagnostics = dict(payload.get("diagnostics") or {})
        record["diagnostics"] = {
            "code": diagnostics.get("code"),
            "evidence_complete": diagnostics.get("evidence_complete"),
        }
        return record

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

    def reconcile_stale_refreshes(
        self,
        *,
        now: datetime | None = None,
    ) -> int:
        """Close runs that could not record a terminal state before shutdown."""
        completed_at = now or datetime.now(UTC)
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=UTC)
        else:
            completed_at = completed_at.astimezone(UTC)
        cutoff = completed_at - STALE_REFRESH_AFTER
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE analytics_refresh_runs
                SET status = 'interrupted',
                    completed_at = ?,
                    error_type = 'process_interrupted'
                WHERE status = 'running'
                  AND started_at < ?
                """,
                (completed_at.isoformat(), cutoff.isoformat()),
            )
        return max(int(cursor.rowcount), 0)

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
            categories = [str(value) for value in company_record.get("categories", [])]
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
        with self._lock:
            self._facet_cache = {
                key: value
                for key, value in self._facet_cache.items()
                if key[0] != company_id
            }
        return version

    def dashboard(
        self,
        company_id: str,
        *,
        internal: bool,
    ) -> dict[str, Any] | None:
        field = "internal_dashboard_json" if internal else "company_dashboard_json"
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
            "refresh_schedule": REFRESH_SCHEDULE,
        }
        return payload

    def dashboard_activity_records(
        self,
        company_id: str,
        *,
        internal: bool,
    ) -> tuple[dict[str, Any], ...]:
        field = "internal_json" if internal else "company_json"
        with self._connection() as connection:
            snapshot = connection.execute(
                """
                SELECT active_version
                FROM analytics_snapshots
                WHERE company_id = ?
                """,
                (company_id,),
            ).fetchone()
            if snapshot is None:
                return ()
            version = str(snapshot["active_version"])
            rows = connection.execute(
                f"""
                SELECT records.{field} AS payload_json
                FROM analytics_query_records AS records
                WHERE records.company_id = ?
                  AND records.snapshot_version = ?
                ORDER BY records.created_at ASC, records.request_id ASC
                """,
                (company_id, version),
            )
            # Iterate the SQLite cursor instead of fetchall(). The internal
            # payloads are much larger than the compact dashboard projection;
            # retaining every JSON string while decoding every record causes a
            # large transient allocation and pushes the constrained analytics
            # container into swap.
            return tuple(
                self._dashboard_activity_record(
                    row["payload_json"],
                    internal=internal,
                )
                for row in rows
            )

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
        execution_path: str | None = None,
        language: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        include_filtered_results: bool = False,
        request_kind: str | None = None,
        city_id: int | None = None,
        subcategory_id: int | None = None,
        ad_type: str | None = None,
        diagnostic_code: str | None = None,
        has_filters: bool | None = None,
        sort_by: str = "created_at",
        sort_direction: str = "desc",
        include_facets: bool = True,
    ) -> dict[str, Any]:
        field = "internal_json" if internal else "company_json"
        normalized_sort = str(sort_by or "created_at").strip().casefold()
        normalized_direction = str(sort_direction or "desc").strip().casefold()
        if normalized_direction not in {"asc", "desc"}:
            raise ValueError("Query sort direction must be asc or desc")
        sort_expression = _query_sort_expression(normalized_sort, internal=internal)
        uses_default_cursor = (
            normalized_sort == "created_at" and normalized_direction == "desc"
        )
        offset = 0
        clauses = [
            "records.company_id = ?",
            "records.snapshot_version = snapshots.active_version",
        ]
        values: list[Any] = [company_id]
        if not include_filtered_results:
            # Keep ordinary catalogue/filter browsing out of the default Query
            # Explorer, but never hide failed requests. A failed browse is an
            # operational incident rather than demand noise and must remain
            # visible without a frontend-only opt-in parameter.
            clauses.append(
                "("
                f"COALESCE(json_extract(records.{field}, "
                "'$.request_kind'), 'text_search') = 'text_search' "
                "OR records.outcome = 'failure'"
                ")"
            )
        if cursor and uses_default_cursor:
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
            values.extend([cursor_created, cursor_created, cursor_request])
        elif cursor:
            cursor_sort, cursor_direction, offset = decode_query_sort_cursor(cursor)
            if (cursor_sort, cursor_direction) != (
                normalized_sort,
                normalized_direction,
            ):
                raise ValueError("Analytics query cursor does not match sorting")
        if query:
            clauses.append("LOWER(records.query_text) LIKE ? ESCAPE '\\'")
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
        if execution_path:
            clauses.append(
                "LOWER(json_extract(records.internal_json, "
                "'$.performance.execution_path')) = ?"
            )
            values.append(execution_path.casefold())
        if language:
            clauses.append("records.language = ?")
            values.append(language)
        for json_path, expected in (
            ("$.request_kind", request_kind),
            ("$.filters.city_id", city_id),
            ("$.filters.subcategory_id", subcategory_id),
            ("$.filters.target_ad_type", ad_type),
        ):
            if expected is not None and expected != "":
                clauses.append(f"json_extract(records.{field}, '{json_path}') = ?")
                values.append(expected)
        if diagnostic_code and internal:
            clauses.append(
                "json_extract(records.internal_json, '$.diagnostics.code') = ?"
            )
            values.append(diagnostic_code)
        if has_filters is not None:
            operator = "!=" if has_filters else "="
            clauses.append(
                f"COALESCE(json_extract(records.{field}, '$.filters'), '{{}}') "
                f"{operator} '{{}}'"
            )
        if created_from:
            clauses.append("records.created_at >= ?")
            values.append(created_from)
        if created_to:
            clauses.append("records.created_at <= ?")
            values.append(created_to)
        order_clause = (
            "records.created_at DESC, records.request_id DESC"
            if uses_default_cursor
            else (
                f"({sort_expression} IS NULL) ASC, "
                f"{sort_expression} {normalized_direction.upper()}, "
                "records.created_at DESC, records.request_id DESC"
            )
        )
        values.append(limit + 1)
        if not uses_default_cursor:
            values.append(offset)
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
                WHERE {" AND ".join(clauses)}
                ORDER BY {order_clause}
                LIMIT ?
                {"OFFSET ?" if not uses_default_cursor else ""}
                """,
                tuple(values),
            ).fetchall()
        has_more = len(rows) > limit
        visible = rows[:limit]
        next_cursor = None
        if has_more and visible:
            next_cursor = (
                encode_query_cursor(
                    visible[-1]["created_at"],
                    visible[-1]["request_id"],
                )
                if uses_default_cursor
                else encode_query_sort_cursor(
                    normalized_sort,
                    normalized_direction,
                    offset + len(visible),
                )
            )
        result = {
            "company_id": company_id,
            "items": [json.loads(row["payload_json"]) for row in visible],
            "returned": len(visible),
            "has_more": has_more,
            "next_cursor": next_cursor,
            "sorting": {
                "sort_by": normalized_sort,
                "sort_direction": normalized_direction,
            },
        }
        if include_facets:
            result["facets"] = self.query_facets(company_id, internal=internal)
        return result

    def query_facets(self, company_id: str, *, internal: bool) -> dict[str, Any]:
        """Return snapshot-wide Query Explorer choices, not page-local options."""
        return self.query_facets_snapshot(company_id, internal=internal)["facets"]

    def query_facets_snapshot(
        self,
        company_id: str,
        *,
        internal: bool,
    ) -> dict[str, Any]:
        """Return facet choices with the immutable snapshot version they describe."""
        field = "internal_json" if internal else "company_json"
        with self._connection() as connection:
            snapshot = connection.execute(
                "SELECT active_version FROM analytics_snapshots WHERE company_id = ?",
                (company_id,),
            ).fetchone()
            if snapshot is None:
                return {
                    "company_id": company_id,
                    "snapshot_version": None,
                    "facets": {
                        "request_kinds": [],
                        "cities": [],
                        "subcategories": [],
                        "ad_types": [],
                        "diagnostic_codes": [],
                    },
                }
            version = str(snapshot["active_version"])
            cache_key = (company_id, version, internal)
            with self._lock:
                cached = self._facet_cache.get(cache_key)
            if cached is not None:
                return {
                    "company_id": company_id,
                    "snapshot_version": version,
                    "facets": cached,
                }

            def choices(path: str) -> list[str]:
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT json_extract(records.{field}, ?) AS value
                    FROM analytics_query_records AS records
                    WHERE records.company_id = ?
                      AND records.snapshot_version = ?
                      AND json_extract(records.{field}, ?) IS NOT NULL
                    ORDER BY value COLLATE NOCASE
                    """,
                    (path, company_id, version, path),
                ).fetchall()
                return [str(row["value"]) for row in rows if str(row["value"] or "")]

            def id_labels(id_path: str, label_path: str) -> list[dict[str, Any]]:
                rows = connection.execute(
                    f"""
                    SELECT
                        json_extract(records.{field}, ?) AS id,
                        MAX(json_extract(records.{field}, ?)) AS label
                    FROM analytics_query_records AS records
                    WHERE records.company_id = ?
                      AND records.snapshot_version = ?
                      AND json_extract(records.{field}, ?) IS NOT NULL
                    GROUP BY id
                    ORDER BY label COLLATE NOCASE, id
                    """,
                    (id_path, label_path, company_id, version, id_path),
                ).fetchall()
                return [
                    {
                        "id": int(row["id"]),
                        "label": str(row["label"] or f"ID {row['id']}"),
                    }
                    for row in rows
                ]

            facets = {
                "request_kinds": choices("$.request_kind"),
                "cities": id_labels("$.filters.city_id", "$.filters.city"),
                "subcategories": id_labels(
                    "$.filters.subcategory_id", "$.filters.subcategory"
                ),
                "ad_types": choices("$.filters.target_ad_type"),
                "diagnostic_codes": (choices("$.diagnostics.code") if internal else []),
            }
        with self._lock:
            self._facet_cache[cache_key] = facets
        return {
            "company_id": company_id,
            "snapshot_version": version,
            "facets": facets,
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
                    "source_rows": json.loads(snapshot["source_rows_json"]),
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
                    # sqlite3.Row iterates values; keys() is required here.
                    for key in run.keys()  # noqa: SIM118
                }
                if run is not None
                else None
            ),
            "refresh_schedule": REFRESH_SCHEDULE,
        }

    def readiness(self) -> dict[str, Any]:
        """Verify the snapshot database can serve reads and accept state changes."""
        connection = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=1,
                isolation_level=None,
            )
            connection.execute("SELECT 1 FROM analytics_snapshots LIMIT 1")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE analytics_refresh_runs SET status = status WHERE 0"
            )
            connection.rollback()
        except Exception as exc:
            if connection is not None:
                with suppress(Exception):
                    connection.rollback()
            return {"ok": False, "error_type": type(exc).__name__}
        finally:
            if connection is not None:
                connection.close()
        return {"ok": True}
