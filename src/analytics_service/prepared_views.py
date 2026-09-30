"""Prepare common activity views before making a candidate snapshot visible."""

from __future__ import annotations

import gc
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from .adapters import CompanyAnalyticsAdapter
from .config import CompanyAnalyticsConfig
from .filters import PERIOD_OPTIONS, REQUEST_SCOPE_OPTIONS, DashboardFilters
from .memory import log_refresh_memory
from .store import AnalyticsSnapshotStore


class SnapshotActivityRecords:
    """Replay one snapshot from SQLite without retaining its query payloads."""

    def __init__(
        self,
        store: AnalyticsSnapshotStore,
        company_id: str,
        version: str,
        *,
        internal: bool,
    ) -> None:
        self.store = store
        self.company_id = company_id
        self.version = version
        self.internal = internal
        self._count = store.count_dashboard_activity_records(company_id, version)
        self.available_filters: dict[str, Any] | None = None

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return self.store.iter_dashboard_activity_records(
            self.company_id,
            internal=self.internal,
            version=self.version,
        )


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
    return f"activity:{filters.period}:{filters.request_scope}"


def prepare_snapshot_views(
    store: AnalyticsSnapshotStore,
    version: str,
    *,
    company: CompanyAnalyticsConfig,
    adapter: CompanyAnalyticsAdapter,
    generated_at: str,
) -> None:
    # Periods are evaluated as of the snapshot, just like its report modules.
    # Keep only one audience's compact records, and never load them in the API
    # for the standard dashboards.
    anchor = datetime.fromisoformat(generated_at)
    for internal in (False, True):
        store.save_prepared_view(
            company.company_id,
            version,
            internal=internal,
            view_key="facets",
            payload=store.build_query_facets(
                company.company_id, version, internal=internal
            ),
        )
        records = SnapshotActivityRecords(
            store, company.company_id, version, internal=internal
        )
        availability_builder = getattr(adapter, "available_dashboard_filters", None)
        if callable(availability_builder):
            records.available_filters = availability_builder(records, internal=internal)
        for period in PERIOD_OPTIONS:
            if period == "custom":
                continue
            for scope in REQUEST_SCOPE_OPTIONS:
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
            log_refresh_memory(f"prepared_internal_{internal}_{period}")
        del records
        gc.collect()
