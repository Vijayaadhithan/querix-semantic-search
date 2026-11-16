from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from analytics_service.serialization import (
    json_dumps,
)

from .activity_records import dashboard_activity_record
from .query_facets import build_query_facets as build_facets
from .query_reader import query_records as read_query_records
from .schedule import REFRESH_SCHEDULE
from .snapshot_schema import initialize_snapshot_schema
from .sqlite import analytics_connection

__all__ = (
    "AnalyticsSnapshotStore",
    "LOGGER",
    "QUERY_RECORD_BATCH_SIZE",
    "STALE_REFRESH_AFTER",
    "json_dumps",
    "utc_now_iso",
)

LOGGER = logging.getLogger(__name__)
STALE_REFRESH_AFTER = timedelta(hours=6)
QUERY_RECORD_BATCH_SIZE = 1_000


def _query_record_batches(
    company_id: str,
    version: str,
    query_records: Iterable[tuple[dict[str, Any], dict[str, Any]]],
    *,
    batch_size: int = QUERY_RECORD_BATCH_SIZE,
) -> Iterator[list[tuple[Any, ...]]]:
    """Serialize query rows in bounded batches for SQLite publication."""

    if batch_size < 1:
        raise ValueError("Analytics query batch size must be positive")
    batch: list[tuple[Any, ...]] = []
    for company_record, internal_record in query_records:
        categories = [str(value) for value in company_record.get("categories", [])]
        batch.append(
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
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


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

    _dashboard_activity_record = staticmethod(dashboard_activity_record)

    def _connection(self) -> AbstractContextManager[sqlite3.Connection]:
        return analytics_connection(self.path)

    def _initialize(self) -> None:
        with self._connection() as connection:
            initialize_snapshot_schema(connection)

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

    def interrupt_refreshes_started_since(
        self,
        company_id: str,
        started_at: datetime,
        *,
        error_type: str = "process_interrupted",
    ) -> int:
        """Close runs whose worker exited before it could record a failure."""
        if started_at.tzinfo is None:
            raise ValueError("started_at must include a timezone")
        cutoff = started_at.astimezone(UTC).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE analytics_refresh_runs
                SET status = 'interrupted',
                    completed_at = ?,
                    error_type = ?
                WHERE company_id = ?
                  AND status = 'running'
                  AND started_at >= ?
                """,
                (utc_now_iso(), error_type[:191], company_id, cutoff),
            )
        return max(int(cursor.rowcount), 0)

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
        query_records: Iterable[tuple[dict[str, Any], dict[str, Any]]],
        expected_query_records: int | None = None,
        prepare_views: Callable[[AnalyticsSnapshotStore, str], None] | None = None,
    ) -> str:
        version = uuid.uuid4().hex
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
                inserted_query_records = 0
                for query_batch in _query_record_batches(
                    company_id,
                    version,
                    query_records,
                ):
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
                        query_batch,
                    )
                    inserted_query_records += len(query_batch)
                if (
                    expected_query_records is not None
                    and inserted_query_records != expected_query_records
                ):
                    raise ValueError(
                        "Analytics query stream produced "
                        f"{inserted_query_records} records; expected "
                        f"{expected_query_records}"
                    )
                # Commit the invisible candidate before expensive preparation.
                # Readers keep using the old active version, and authentication
                # can still write sessions while dashboard summaries are built.
                connection.commit()
                if prepare_views is not None:
                    prepare_views(self, version)
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT active_version FROM analytics_snapshots "
                    "WHERE company_id = ?",
                    (company_id,),
                ).fetchone()
                if (current[0] if current else None) != (
                    previous[0] if previous else None
                ):
                    raise RuntimeError("Analytics snapshot changed during preparation")
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
                    connection.execute(
                        "DELETE FROM analytics_prepared_views "
                        "WHERE company_id = ? AND snapshot_version = ?",
                        (company_id, previous["active_version"]),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                # Candidate rows may already be committed, but are never active
                # until every prepared view succeeds.
                with connection:
                    connection.execute(
                        "DELETE FROM analytics_query_records "
                        "WHERE company_id = ? AND snapshot_version = ?",
                        (company_id, version),
                    )
                    connection.execute(
                        "DELETE FROM analytics_prepared_views "
                        "WHERE company_id = ? AND snapshot_version = ?",
                        (company_id, version),
                    )
                raise
        with self._lock:
            self._facet_cache = {
                key: value
                for key, value in self._facet_cache.items()
                if key[0] != company_id
            }
        return version

    def save_prepared_view(
        self,
        company_id: str,
        version: str,
        *,
        internal: bool,
        view_key: str,
        payload: dict[str, Any],
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO analytics_prepared_views "
                "VALUES (?, ?, ?, ?, ?)",
                (company_id, version, int(internal), view_key, json_dumps(payload)),
            )

    def prepared_view(
        self,
        company_id: str,
        version: str,
        *,
        internal: bool,
        view_key: str,
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM analytics_prepared_views "
                "WHERE company_id = ? AND snapshot_version = ? "
                "AND internal = ? AND view_key = ?",
                (company_id, version, int(internal), view_key),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

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
                SELECT active_version, generated_at, source_watermark,
                       source_rows_json, {field}
                FROM analytics_snapshots
                WHERE company_id = ?
                """,
                (company_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[field])
        payload["snapshot"] = {
            "version": row["active_version"],
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
        version: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            self.iter_dashboard_activity_records(
                company_id, internal=internal, version=version
            )
        )

    def iter_dashboard_activity_records(
        self,
        company_id: str,
        *,
        internal: bool,
        version: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Decode compact activity records one at a time for refresh jobs."""

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
            if snapshot is None and version is None:
                return
            version = version or str(snapshot["active_version"])
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
            for row in rows:
                yield self._dashboard_activity_record(
                    row["payload_json"],
                    internal=internal,
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
        return read_query_records(
            self._connection,
            self.query_facets,
            company_id,
            internal=internal,
            limit=limit,
            cursor=cursor,
            query=query,
            outcome=outcome,
            category=category,
            execution_path=execution_path,
            language=language,
            created_from=created_from,
            created_to=created_to,
            include_filtered_results=include_filtered_results,
            request_kind=request_kind,
            city_id=city_id,
            subcategory_id=subcategory_id,
            ad_type=ad_type,
            diagnostic_code=diagnostic_code,
            has_filters=has_filters,
            sort_by=sort_by,
            sort_direction=sort_direction,
            include_facets=include_facets,
        )

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

            facets = self.prepared_view(
                company_id, version, internal=internal, view_key="facets"
            )
            if facets is None:
                facets = self.build_query_facets(company_id, version, internal=internal)
        with self._lock:
            self._facet_cache[cache_key] = facets
        return {
            "company_id": company_id,
            "snapshot_version": version,
            "facets": facets,
        }

    def build_query_facets(
        self,
        company_id: str,
        version: str,
        *,
        internal: bool,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            return build_facets(connection, company_id, version, internal=internal)

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
