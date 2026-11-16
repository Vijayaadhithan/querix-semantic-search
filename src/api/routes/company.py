"""Company search HTTP routes."""

import time
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import ValidationError

from api.context import SearchRequestContext
from api.contracts import (
    SearchResponse,
)


def register_company_routes(
    application: FastAPI, context: SearchRequestContext
) -> None:
    @application.get(
        "/api/v1/{company_endpoint}/auth/verify",
        tags=["company"],
    )
    def company_auth_verify(
        company_endpoint: str,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        profile = context.resolve_company_profile(
            x_api_key,
            company_endpoint=company_endpoint,
        )
        return {
            "authorized": True,
            "company_id": profile.company_id,
            "endpoint_slug": profile.endpoint_slug,
        }

    @application.post(
        "/api/v1/{company_endpoint}/search",
        response_model=SearchResponse,
        tags=["company"],
    )
    def company_search(
        company_endpoint: str,
        payload: dict[str, Any],
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> SearchResponse:
        request = context.company_search_request(company_endpoint, payload)
        return context.execute_search(
            request,
            x_api_key,
            company_endpoint=company_endpoint,
        )

    @application.post(
        "/api/v1/{company_endpoint}/search-suggestions",
        tags=["company-compatibility"],
    )
    def company_search_suggestions(
        company_endpoint: str,
        payload: dict[str, Any],
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        try:
            compatibility_service = context.resolve_compatibility_service(
                x_api_key,
                company_endpoint=company_endpoint,
            )
            request = context.parse_compatibility_payload(
                compatibility_service,
                "parse_search_suggestions",
                payload,
            )
            return compatibility_service.search_suggestions(request)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.post(
        "/api/v1/{company_endpoint}/filter-data",
        tags=["company-compatibility"],
    )
    def company_filter_data(
        company_endpoint: str,
        payload: dict[str, Any],
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        try:
            compatibility_service = context.resolve_compatibility_service(
                x_api_key,
                company_endpoint=company_endpoint,
            )
            request = context.parse_compatibility_payload(
                compatibility_service,
                "parse_filter_data",
                payload,
            )
            return compatibility_service.filter_data(request)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.post(
        "/api/v1/{company_endpoint}/filter-result",
        tags=["company-compatibility"],
    )
    def company_filter_result(
        company_endpoint: str,
        payload: dict[str, Any],
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        x_user_id: str | None = Header(default=None, alias="X-User-ID"),
    ) -> dict[str, Any]:
        compatibility_service = context.resolve_compatibility_service(
            x_api_key,
            company_endpoint=company_endpoint,
        )
        try:
            request = compatibility_service.parse_filter_result(payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=exc.errors(include_context=False),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        search_started = time.perf_counter()
        try:
            return compatibility_service.filter_results(
                request,
                user_id=x_user_id,
            )
        except RuntimeError as exc:
            failure_context = getattr(
                compatibility_service,
                "analytics_failure_context",
                lambda _request: {},
            )(request)
            compatibility_service.product_search_service.record_search_failure(
                request.searchTerm,
                duration_ms=(time.perf_counter() - search_started) * 1000,
                error_type=type(exc).__name__,
                context=failure_context,
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            failure_context = getattr(
                compatibility_service,
                "analytics_failure_context",
                lambda _request: {},
            )(request)
            compatibility_service.product_search_service.record_search_failure(
                request.searchTerm,
                duration_ms=(time.perf_counter() - search_started) * 1000,
                error_type=type(exc).__name__,
                context=failure_context,
            )
            raise

    @application.get(
        "/api/v1/{company_endpoint}/recent-search",
        tags=["company-compatibility"],
    )
    def company_recent_search(
        company_endpoint: str,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        x_user_id: str | None = Header(default=None, alias="X-User-ID"),
    ) -> dict[str, Any]:
        return context.resolve_compatibility_service(
            x_api_key,
            company_endpoint=company_endpoint,
        ).recent_searches(x_user_id)

    @application.get(
        "/api/v1/{company_endpoint}/usage",
        tags=["company"],
    )
    def company_usage(
        company_endpoint: str,
        month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        try:
            return context.resolve_service(
                x_api_key,
                apply_rate_limit=False,
                company_endpoint=company_endpoint,
            ).usage_summary(month)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
