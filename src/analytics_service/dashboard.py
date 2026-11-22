"""Serve prepared analytics views with bounded fallback calculations."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException, Response

from .adapters import CompanyAnalyticsAdapter
from .config import CompanyAnalyticsConfig
from .dashboard_cache import DashboardActivityCache
from .filters import DashboardFilters
from .prepared_views import activity_view_key
from .store import AnalyticsSnapshotStore


class AnalyticsDashboardService:
    def __init__(
        self,
        store: AnalyticsSnapshotStore,
        adapters: dict[str, CompanyAnalyticsAdapter],
    ) -> None:
        self.store = store
        self.adapters = adapters
        self.activity_cache = DashboardActivityCache()

    def cached_dashboard_activity(
        self,
        company_id: str,
        generated_at: str,
        internal: bool,
        filters: DashboardFilters,
        timezone_name: str,
        time_bucket: datetime | None,
    ) -> dict[str, Any]:
        return self.activity_cache.get(
            (company_id, generated_at, internal, filters, timezone_name, time_bucket),
            lambda: self.adapters[company_id].dashboard_overview(
                list(
                    self.store.dashboard_activity_records(
                        company_id,
                        internal=internal,
                    )
                ),
                internal=internal,
                filters=filters,
                timezone_name=timezone_name,
                now=time_bucket,
            ),
        )

    def company_adapter(
        self,
        company: CompanyAnalyticsConfig,
    ) -> CompanyAnalyticsAdapter:
        try:
            return self.adapters[company.company_id]
        except KeyError as exc:
            raise HTTPException(
                status_code=503,
                detail="Company analytics adapter is unavailable.",
            ) from exc

    def get_dashboard(
        self,
        company: CompanyAnalyticsConfig,
        *,
        internal: bool,
        filters: DashboardFilters,
    ) -> dict[str, Any]:
        dashboard = self.store.dashboard(
            company.company_id,
            internal=internal,
        )
        if dashboard is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "No completed analytics snapshot is available. "
                    "Run the analytics refresh."
                ),
            )
        try:
            snapshot = dict(dashboard.get("snapshot") or {})
            view_key = activity_view_key(filters)
            prepared = (
                self.store.prepared_view(
                    company.company_id,
                    str(snapshot.get("version") or ""),
                    internal=internal,
                    view_key=view_key,
                )
                if view_key is not None
                else None
            )
            if prepared is not None:
                return {**dashboard, **prepared}
            # All activity periods share the snapshot's as-of time. Custom
            # filters still use the bounded cache; legacy snapshots can also be
            # served until their first refresh prepares the standard views.
            time_bucket = (
                datetime.fromisoformat(str(snapshot["generated_at"]))
                if filters.period in {"24h", "7d", "30d", "90d"}
                else None
            )
            activity = self.cached_dashboard_activity(
                company.company_id,
                str(
                    dict(dashboard.get("snapshot") or {}).get("generated_at")
                    or dict(dashboard.get("metadata") or {}).get("generated_at")
                    or "unknown"
                ),
                internal,
                filters,
                company.timezone,
                time_bucket,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {**dashboard, **activity}

    def get_queries(
        self,
        company: CompanyAnalyticsConfig,
        *,
        internal: bool,
        limit: int,
        cursor: str | None,
        query: str | None,
        exact_phrase: bool = False,
        outcome: str | None,
        category: str | None,
        execution_path: str | None,
        language: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        include_filtered_results: bool,
        request_kind: str | None,
        city_id: int | None,
        subcategory_id: int | None,
        ad_type: str | None,
        diagnostic_code: str | None,
        has_filters: bool | None,
        sort_by: str,
        sort_direction: str,
        include_facets: bool,
    ) -> dict[str, Any]:
        if not self.store.company_status(company.company_id)["has_snapshot"]:
            raise HTTPException(
                status_code=503,
                detail=(
                    "No completed analytics snapshot is available. "
                    "Run the analytics refresh."
                ),
            )
        try:
            return self.store.query_records(
                company.company_id,
                internal=internal,
                limit=limit,
                cursor=cursor,
                query=query,
                exact_phrase=exact_phrase,
                outcome=outcome,
                category=category,
                execution_path=execution_path if internal else None,
                language=language,
                created_from=(created_from.isoformat() if created_from else None),
                created_to=created_to.isoformat() if created_to else None,
                include_filtered_results=include_filtered_results,
                request_kind=request_kind,
                city_id=city_id,
                subcategory_id=subcategory_id,
                ad_type=ad_type,
                diagnostic_code=diagnostic_code if internal else None,
                has_filters=has_filters,
                sort_by=sort_by,
                sort_direction=sort_direction,
                include_facets=include_facets,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def facet_response(
        self,
        response: Response,
        payload: dict[str, Any],
        *,
        snapshot_version: str | None,
        audience: str,
        if_none_match: str | None,
    ) -> dict[str, Any] | Response:
        if not snapshot_version:
            raise HTTPException(
                status_code=503,
                detail=(
                    "No completed analytics snapshot is available. "
                    "Run the analytics refresh."
                ),
            )
        etag = f'"query-facets-{audience}-{snapshot_version}"'
        candidates = {
            candidate.strip() for candidate in str(if_none_match or "").split(",")
        }
        if "*" in candidates or etag in candidates or f"W/{etag}" in candidates:
            return Response(
                status_code=304,
                headers={
                    "ETag": etag,
                    "Cache-Control": "private, max-age=300, must-revalidate",
                },
            )
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "private, max-age=300, must-revalidate"
        return payload
