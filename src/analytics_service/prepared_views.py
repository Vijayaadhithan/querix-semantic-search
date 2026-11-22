"""Prepare common activity views before making a candidate snapshot visible."""

from __future__ import annotations

import gc
import json
import logging
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from .adapters import CompanyAnalyticsAdapter
from .config import CompanyAnalyticsConfig
from .filters import PERIOD_OPTIONS, REQUEST_SCOPE_OPTIONS, DashboardFilters
from .memory import log_refresh_memory
from .query_facets import build_query_facets
from .store import QUERY_RECORD_BATCH_SIZE, AnalyticsSnapshotStore, json_dumps

LOGGER = logging.getLogger(__name__)


class SnapshotActivityRecords:
    """Read large snapshot rows once, then replay compact records from disk."""

    def __init__(
        self,
        store: AnalyticsSnapshotStore,
        company_id: str,
        version: str,
        *,
        internal: bool,
    ) -> None:
        self.company_id = company_id
        self.version = version
        self.internal = internal
        self._count = 0
        self.available_filters: dict[str, Any] | None = None
        self._directory = tempfile.TemporaryDirectory(prefix="analytics-activity-")
        self._path = Path(self._directory.name) / "activity.sqlite3"
        self._connection = sqlite3.connect(self._path)
        self._connection.row_factory = sqlite3.Row
        started = time.monotonic()
        try:
            # This database is disposable and is never visible to API readers.
            self._connection.execute("PRAGMA journal_mode=OFF")
            self._connection.execute("PRAGMA synchronous=OFF")
            self._connection.execute(
                """
                CREATE TABLE analytics_query_records (
                    sequence INTEGER PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    snapshot_version TEXT NOT NULL,
                    company_json TEXT,
                    internal_json TEXT
                )
                """
            )
            batch = []
            for record in store.iter_dashboard_activity_records(
                company_id, internal=internal, version=version
            ):
                payload = json_dumps(record)
                batch.append(
                    (
                        company_id,
                        version,
                        None if internal else payload,
                        payload if internal else None,
                    )
                )
                if len(batch) >= QUERY_RECORD_BATCH_SIZE:
                    self._append(batch)
                    batch.clear()
            self._append(batch)
            self._connection.commit()
        except BaseException:
            self.close()
            raise
        LOGGER.info(
            "Staged compact activity company=%s internal=%s rows=%d bytes=%d "
            "duration_ms=%.1f",
            company_id,
            internal,
            self._count,
            self._path.stat().st_size,
            (time.monotonic() - started) * 1000,
        )

    def _append(self, batch: list[tuple[Any, ...]]) -> None:
        self._connection.executemany(
            "INSERT INTO analytics_query_records "
            "(company_id, snapshot_version, company_json, internal_json) "
            "VALUES (?, ?, ?, ?)",
            batch,
        )
        self._count += len(batch)

    def __enter__(self) -> SnapshotActivityRecords:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()
        self._directory.cleanup()

    def query_facets(self) -> dict[str, Any]:
        return build_query_facets(
            self._connection,
            self.company_id,
            self.version,
            internal=self.internal,
        )

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[dict[str, Any]]:
        field = "internal_json" if self.internal else "company_json"
        rows = self._connection.execute(
            f"SELECT {field} AS payload_json FROM analytics_query_records "
            "ORDER BY sequence"
        )
        for row in rows:
            yield json.loads(row["payload_json"])


def activity_view_key(filters: DashboardFilters) -> str | None:
    """Only standard period/scope combinations have precomputed summaries."""
    if filters.period == "custom" or filters.period not in PERIOD_OPTIONS:
        return None
    if filters.request_scope not in REQUEST_SCOPE_OPTIONS:
        return None
    if filters != DashboardFilters(
        period=filters.period, request_scope=filters.request_scope
    ):
        return None
    return f"activity:v2:{filters.period}:{filters.request_scope}"


def prepare_snapshot_views(
    store: AnalyticsSnapshotStore,
    version: str,
    *,
    company: CompanyAnalyticsConfig,
    adapter: CompanyAnalyticsAdapter,
    generated_at: str,
) -> None:
    # Periods are evaluated as of the snapshot, just like its report modules.
    # Cache only one audience's compact records on disk. Repeated passes no
    # longer reread large Explorer payloads and compete for the worker's cache.
    anchor = datetime.fromisoformat(generated_at)
    for internal in (False, True):
        with SnapshotActivityRecords(
            store, company.company_id, version, internal=internal
        ) as records:
            store.save_prepared_view(
                company.company_id,
                version,
                internal=internal,
                view_key="facets",
                payload=records.query_facets(),
            )
            availability_builder = getattr(adapter, "available_dashboard_filters", None)
            if callable(availability_builder):
                records.available_filters = availability_builder(
                    records, internal=internal
                )
            for period in PERIOD_OPTIONS:
                if period == "custom":
                    continue
                for scope in REQUEST_SCOPE_OPTIONS:
                    started = time.monotonic()
                    filters = DashboardFilters(period=period, request_scope=scope)
                    payload = adapter.dashboard_overview(
                        records,
                        internal=internal,
                        filters=filters,
                        timezone_name=company.timezone,
                        now=anchor,
                    )
                    store.save_prepared_view(
                        company.company_id,
                        version,
                        internal=internal,
                        view_key=activity_view_key(filters),
                        payload=payload,
                    )
                    LOGGER.info(
                        "Prepared activity view company=%s internal=%s period=%s "
                        "scope=%s duration_ms=%.1f",
                        company.company_id,
                        internal,
                        period,
                        scope,
                        (time.monotonic() - started) * 1000,
                    )
                log_refresh_memory(f"prepared_internal_{internal}_{period}")
        del records
        gc.collect()
