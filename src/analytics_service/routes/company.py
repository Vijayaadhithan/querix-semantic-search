"""Company analytics HTTP routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import Cookie, FastAPI, Header, Query, Response

from ..access import AnalyticsAccess
from ..config import AnalyticsSettings
from ..dashboard import AnalyticsDashboardService
from ..filters import DashboardFilters
from ..store import AnalyticsSnapshotStore


def register_company_routes(
    application: FastAPI,
    settings: AnalyticsSettings,
    store: AnalyticsSnapshotStore,
    access: AnalyticsAccess,
    dashboards: AnalyticsDashboardService,
) -> None:
    @application.get(
        "/api/v1/{company_endpoint}/analytics/dashboard",
        tags=["company-analytics"],
    )
    def company_dashboard(
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
        created_from: datetime | None = Query(default=None, alias="from"),
        created_to: datetime | None = Query(default=None, alias="to"),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        analytics_session: str | None = Cookie(
            default=None,
            alias=settings.company_session_cookie_name,
        ),
    ) -> dict[str, Any]:
        company = access.require_company(
            company_endpoint,
            x_api_key,
            analytics_session,
            response,
        )
        return dashboards.company_adapter(company).dashboard_response(
            dashboards.get_dashboard(
                company,
                internal=False,
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
                ),
            )
        )

    @application.get(
        "/api/v1/{company_endpoint}/analytics/queries",
        tags=["company-analytics"],
    )
    def company_queries(
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
            pattern="^(created_at|results|outcome)$",
        ),
        sort_direction: str = Query(default="desc", pattern="^(asc|desc)$"),
        query: str | None = Query(default=None, max_length=1000),
        exact_phrase: bool = Query(default=False),
        outcome: str | None = Query(
            default=None,
            pattern="^(fulfilled|zero_result|failure|telemetry_missing)$",
        ),
        category: str | None = Query(default=None, max_length=191),
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
        has_filters: bool | None = Query(default=None),
        created_from: datetime | None = Query(default=None, alias="from"),
        created_to: datetime | None = Query(default=None, alias="to"),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        analytics_session: str | None = Cookie(
            default=None,
            alias=settings.company_session_cookie_name,
        ),
    ) -> dict[str, Any]:
        company = access.require_company(
            company_endpoint,
            x_api_key,
            analytics_session,
            response,
        )
        return dashboards.company_adapter(company).queries_response(
            dashboards.get_queries(
                company,
                internal=False,
                limit=limit,
                cursor=cursor,
                query=query,
                exact_phrase=exact_phrase,
                outcome=outcome,
                category=category,
                execution_path=None,
                language=language,
                created_from=created_from,
                created_to=created_to,
                include_filtered_results=include_filtered_results,
                request_kind=request_kind,
                city_id=city_id,
                subcategory_id=subcategory_id,
                ad_type=ad_type,
                diagnostic_code=None,
                has_filters=has_filters,
                sort_by=sort_by,
                sort_direction=sort_direction,
                include_facets=include_facets,
            )
        )

    @application.get(
        "/api/v1/{company_endpoint}/analytics/query-facets",
        tags=["company-analytics"],
        response_model=None,
    )
    def company_query_facets(
        company_endpoint: str,
        response: Response,
        if_none_match: str | None = Header(default=None, alias="If-None-Match"),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        analytics_session: str | None = Cookie(
            default=None,
            alias=settings.company_session_cookie_name,
        ),
    ) -> dict[str, Any] | Response:
        company = access.require_company(
            company_endpoint,
            x_api_key,
            analytics_session,
            response,
        )
        payload = store.query_facets_snapshot(
            company.company_id,
            internal=False,
        )
        adapted = dashboards.company_adapter(company).facets_response(payload)
        return dashboards.facet_response(
            response,
            adapted,
            snapshot_version=payload["snapshot_version"],
            audience="company",
            if_none_match=if_none_match,
        )

    @application.get(
        "/api/v1/{company_endpoint}/analytics/status",
        tags=["company-analytics"],
    )
    def company_status(
        company_endpoint: str,
        response: Response,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        analytics_session: str | None = Cookie(
            default=None,
            alias=settings.company_session_cookie_name,
        ),
    ) -> dict[str, Any]:
        company = access.require_company(
            company_endpoint,
            x_api_key,
            analytics_session,
            response,
        )
        status = store.company_status(company.company_id)
        response_payload = {
            "company_id": status["company_id"],
            "has_snapshot": status["has_snapshot"],
            "snapshot": status["snapshot"],
            "refresh_schedule": status["refresh_schedule"],
        }
        return dashboards.company_adapter(company).status_response(response_payload)
