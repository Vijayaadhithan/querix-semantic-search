"""Search HTTP authentication, request normalization, and error translation."""

import hashlib
import hmac
import logging
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from api.contracts import (
    ExpiredCursorError,
    InvalidCursorError,
    SearchCapacityError,
    SearchRequest,
    SearchResponse,
)
from api.service import (
    ProductSearchService,
)
from core.settings import (
    API_RATE_LIMIT_ENABLED,
)
from core.tenant_config import TenantProfile
from tenants.compatibility import search_client_contract

LOGGER = logging.getLogger("uvicorn.error")


class SearchRequestContext:
    def __init__(self, application: FastAPI, admin_key: Callable[[], str]) -> None:
        self.application = application
        self.admin_key = admin_key

    def resolve_company_profile(
        self,
        api_key: str | None,
        *,
        company_endpoint: str | None = None,
    ) -> TenantProfile:
        if not self.application.state.tenant_mode:
            raise HTTPException(
                status_code=404,
                detail="Company authentication requires tenant mode.",
            )
        if not api_key:
            raise HTTPException(
                status_code=401,
                detail="Missing API key.",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        profile = self.application.state.tenant_registry.resolve_api_key(api_key)
        if profile is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid API key.",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        if company_endpoint is not None:
            endpoint_profile = self.application.state.tenant_registry.resolve_endpoint(
                company_endpoint
            )
            if endpoint_profile is None:
                raise HTTPException(
                    status_code=404,
                    detail="Unknown company endpoint.",
                )
            if endpoint_profile.company_id != profile.company_id:
                raise HTTPException(
                    status_code=403,
                    detail="API key does not match the company endpoint.",
                )
        return profile

    def resolve_service(
        self,
        api_key: str | None,
        *,
        apply_rate_limit: bool,
        company_endpoint: str | None = None,
    ) -> ProductSearchService:
        if not self.application.state.tenant_mode:
            if company_endpoint is not None:
                raise HTTPException(
                    status_code=404,
                    detail="Company endpoints require tenant mode.",
                )
            return self.application.state.search_service
        profile = self.resolve_company_profile(
            api_key,
            company_endpoint=company_endpoint,
        )
        if apply_rate_limit and API_RATE_LIMIT_ENABLED:
            allowed, _remaining = self.application.state.rate_limiter.allow(
                profile,
                hashlib.sha256(api_key.encode("utf-8")).hexdigest(),
            )
            if not allowed:
                raise HTTPException(
                    status_code=429,
                    detail="Company rate limit exceeded.",
                    headers={"Retry-After": "1"},
                )
        try:
            return self.application.state.tenant_service_pool.get(profile.company_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def resolve_compatibility_service(
        self,
        api_key: str | None,
        *,
        company_endpoint: str,
    ):
        search_service = self.resolve_service(
            api_key,
            apply_rate_limit=True,
            company_endpoint=company_endpoint,
        )
        compatibility_service = getattr(
            search_service,
            "compatibility_service",
            None,
        )
        if compatibility_service is None:
            raise HTTPException(
                status_code=404,
                detail="This company has no compatibility API configured.",
            )
        return compatibility_service

    def parse_compatibility_payload(
        self,
        compatibility_service,
        parser_name: str,
        payload: dict[str, Any],
    ):
        parser = getattr(compatibility_service, parser_name, None)
        if parser is None:
            return payload
        try:
            return parser(payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=exc.errors(include_context=False),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def require_admin_key(self, admin_key: str | None) -> None:
        if not self.application.state.tenant_mode or not self.admin_key():
            raise HTTPException(status_code=404, detail="Not found.")
        if not admin_key or not hmac.compare_digest(
            admin_key,
            self.admin_key(),
        ):
            raise HTTPException(status_code=401, detail="Invalid admin key.")

    def resolve_admin_service(
        self,
        admin_key: str | None,
        *,
        company_endpoint: str,
    ) -> ProductSearchService:
        self.require_admin_key(admin_key)
        profile = self.application.state.tenant_registry.resolve_endpoint(
            company_endpoint
        )
        if profile is None:
            raise HTTPException(status_code=404, detail="Unknown company endpoint.")
        return self.application.state.tenant_service_pool.get(profile.company_id)

    def company_search_request(
        self,
        company_endpoint: str,
        payload: dict[str, Any],
    ) -> SearchRequest:
        if not self.application.state.tenant_mode:
            raise HTTPException(
                status_code=404,
                detail="Company endpoints require tenant mode.",
            )
        profile = self.application.state.tenant_registry.resolve_endpoint(
            company_endpoint
        )
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail="Unknown company endpoint.",
            )
        if search_client_contract(profile.compatibility.adapter).blocks_generic_search:
            raise HTTPException(
                status_code=404,
                detail=("This tenant uses the compatibility filter-result endpoint."),
            )
        mapping = profile.payload.request_mapping or {
            "query": "query",
            "cursor": "cursor",
            "page_size": "page_size",
        }
        allowed_fields = set(mapping.values())
        unexpected = sorted(set(payload) - allowed_fields)
        if unexpected:
            raise HTTPException(
                status_code=422,
                detail=f"Unexpected request fields: {unexpected}",
            )
        normalized = {
            canonical: payload[company_field]
            for canonical, company_field in mapping.items()
            if company_field in payload
        }
        try:
            return SearchRequest.model_validate(normalized)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=exc.errors(include_context=False),
            ) from exc

    def execute_search(
        self,
        request: SearchRequest,
        x_api_key: str | None,
        *,
        company_endpoint: str | None = None,
    ) -> SearchResponse:
        search_service: ProductSearchService | None = None
        search_started: float | None = None

        def record_processing_failure(exc: Exception) -> None:
            if (
                search_service is None
                or search_started is None
                or request.query is None
            ):
                return
            search_service.record_search_failure(
                request.query,
                duration_ms=(time.perf_counter() - search_started) * 1000,
                error_type=type(exc).__name__,
            )

        try:
            search_service = self.resolve_service(
                x_api_key,
                apply_rate_limit=True,
                company_endpoint=company_endpoint,
            )
            search_started = time.perf_counter()
            return search_service.search(request)
        except InvalidCursorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ExpiredCursorError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        except HTTPException:
            # Authentication, authorization, validation, and rate limits are
            # expected client outcomes, not backend system failures.
            raise
        except SearchCapacityError as exc:
            record_processing_failure(exc)
            raise HTTPException(
                status_code=503,
                detail=str(exc),
                headers={"Retry-After": "2"},
            ) from exc
        except RuntimeError as exc:
            record_processing_failure(exc)
            LOGGER.exception(
                "search_request status=failed company=%s error_type=%s query_chars=%d",
                company_endpoint or "legacy",
                type(exc).__name__,
                len(request.query or ""),
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            record_processing_failure(exc)
            LOGGER.exception(
                "search_request status=failed company=%s error_type=%s query_chars=%d",
                company_endpoint or "legacy",
                type(exc).__name__,
                len(request.query or ""),
            )
            raise
