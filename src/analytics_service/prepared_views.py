"""Prepare common activity views before making a candidate snapshot visible."""

from __future__ import annotations

import gc
from datetime import datetime

from .adapters import CompanyAnalyticsAdapter
from .config import CompanyAnalyticsConfig
from .filters import PERIOD_OPTIONS, REQUEST_SCOPE_OPTIONS, DashboardFilters
from .store import AnalyticsSnapshotStore


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
        records = list(
            store.dashboard_activity_records(
                company.company_id, internal=internal, version=version
            )
        )
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
        del records
        gc.collect()
