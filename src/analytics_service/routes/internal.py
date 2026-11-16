"""Internal analytics HTTP routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import Cookie, FastAPI, Header, Query, Response

from ..access import AnalyticsAccess
from ..config import AnalyticsRegistry, AnalyticsSettings
from ..dashboard import AnalyticsDashboardService
from ..filters import DashboardFilters
from ..schedule import REFRESH_SCHEDULE
from ..store import AnalyticsSnapshotStore


def register_internal_routes(
    application: FastAPI,
    settings: AnalyticsSettings,
    registry: AnalyticsRegistry,
    store: AnalyticsSnapshotStore,
    access: AnalyticsAccess,
    dashboards: AnalyticsDashboardService,
) -> None:
    @application.get(
        "/api/v1/admin/analytics/companies",
        tags=["internal-analytics"],
    )
    def admin_companies(
        response: Response,
        analytics_session: str | None = Cookie(
            default=None,
            alias=settings.internal_session_cookie_name,
        ),
    ) -> dict[str, Any]:
        access.require_admin(analytics_session, response)
        return {
            "companies": [
                {
                    "company_id": company.company_id,
                    "endpoint_slug": company.endpoint_slug,
                    **store.company_status(company.company_id),
                }
                for company in registry.companies.values()
            ],
            "refresh_schedule": REFRESH_SCHEDULE,
        }

    @application.get(
        "/api/v1/admin/analytics/{company_endpoint}/dashboard",
        tags=["internal-analytics"],
    )
    def admin_dashboard(
        company_endpoint: str,
        response: Response,
        period: str = Query(
            default="all",
            pattern="^(24h|7d|30d|90d|all|custom)$",
        ),
        request_scope: str = Query(
            default="all",
            pattern="^(all|text_search|browse)$",
        ),
        outcome: str | None = Query(
            default=None,
            pattern="^(fulfilled|zero_result|failure|telemetry_missing)$",
        ),
        category: str | None = Query(default=None, max_length=191),
        language: str | None = Query(default=None, max_length=64),
        city: str | None = Query(default=None, max_length=191),
        city_id: int | None = Query(default=None, gt=0),
        ad_type: str | None = Query(default=None, max_length=64),
        execution_path: str | None = Query(default=None, max_length=128),
        provider: str | None = Query(default=None, max_length=128),
        operation: str | None = Query(default=None, max_length=128),
        created_from: datetime | None = Query(default=None, alias="from"),
        created_to: datetime | None = Query(default=None, alias="to"),
        analytics_session: str | None = Cookie(
            default=None,
            alias=settings.internal_session_cookie_name,
        ),
    ) -> dict[str, Any]:
        company = access.require_admin_company(
            company_endpoint,
            analytics_session,
            response,
        )
        return dashboards.get_dashboard(
            company,
            internal=True,
            filters=DashboardFilters(
                period=period,
                request_scope=request_scope,
                created_from=created_from,
                created_to=created_to,
                outcome=outcome,
                category=category,
                language=language,
                city=city,
                city_id=city_id,
                ad_type=ad_type,
                execution_path=execution_path,
                provider=provider,
                operation=operation,
            ),
        )

    @application.get(
        "/api/v1/admin/analytics/{company_endpoint}/queries",
        tags=["internal-analytics"],
    )
    def admin_queries(
        company_endpoint: str,
        response: Response,
        limit: int = Query(
            default=settings.query_page_size,
            ge=1,
            le=settings.query_max_page_size,
        ),
        cursor: str | None = Query(default=None, max_length=1024),
        sort_by: str = Query(
            default="created_at",
            pattern="^(created_at|results|outcome|execution_path|duration|tokens)$",
        ),
        sort_direction: str = Query(default="desc", pattern="^(asc|desc)$"),
        query: str | None = Query(default=None, max_length=1000),
        outcome: str | None = Query(
            default=None,
            pattern="^(fulfilled|zero_result|failure|telemetry_missing)$",
        ),
        category: str | None = Query(default=None, max_length=191),
        execution_path: str | None = Query(default=None, max_length=128),
        language: str | None = Query(default=None, max_length=64),
        include_filtered_results: bool = Query(default=False),
        include_facets: bool = Query(default=True),
        request_kind: str | None = Query(
            default=None,
            pattern="^(text_search|filtered_browse|catalogue_browse)$",
        ),
        city_id: int | None = Query(default=None, gt=0),
        subcategory_id: int | None = Query(default=None, gt=0),
        ad_type: str | None = Query(default=None, max_length=64),
        diagnostic_code: str | None = Query(default=None, max_length=128),
        has_filters: bool | None = Query(default=None),
        created_from: datetime | None = Query(default=None, alias="from"),
        created_to: datetime | None = Query(default=None, alias="to"),
        analytics_session: str | None = Cookie(
            default=None,
            alias=settings.internal_session_cookie_name,
        ),
    ) -> dict[str, Any]:
        company = access.require_admin_company(
            company_endpoint,
            analytics_session,
            response,
        )
        return dashboards.get_queries(
            company,
            internal=True,
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

    @application.get(
        "/api/v1/admin/analytics/{company_endpoint}/query-facets",
        tags=["internal-analytics"],
        response_model=None,
    )
    def admin_query_facets(
        company_endpoint: str,
        response: Response,
        if_none_match: str | None = Header(default=None, alias="If-None-Match"),
        analytics_session: str | None = Cookie(
            default=None,
            alias=settings.internal_session_cookie_name,
        ),
    ) -> dict[str, Any] | Response:
        company = access.require_admin_company(
            company_endpoint,
            analytics_session,
            response,
        )
        payload = store.query_facets_snapshot(
            company.company_id,
            internal=True,
        )
        return dashboards.facet_response(
            response,
            payload,
            snapshot_version=payload["snapshot_version"],
            audience="internal",
            if_none_match=if_none_match,
        )
