import hashlib
import hmac
import logging
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from api.contracts import (
    PROCESS_STARTED_MONOTONIC,
    PUBLIC_PRODUCT_FIELDS,
    ExpiredCursorError,
    HealthResponse,
    InvalidCursorError,
    PaginationResponse,
    SearchCapacityError,
    SearchRequest,
    SearchResponse,
    SearchSession,
    SearchSessionStore,
    decode_cursor,
    encode_cursor,
    process_monitor_status,
)
from api.lifecycle import build_lifespan
from api.readiness import readiness_response
from api.service import (
    ProductSearchService,
    product_is_visible,
    public_product,
)
from api.tenants import TenantServicePool
from core.rate_limit import TenantRateLimiter
from core.settings import (
    API_ADMIN_KEY,
    API_CORS_ORIGINS,
    API_RATE_LIMIT_ENABLED,
    APP_NAME,
)
from core.tenant_config import TenantProfile, TenantRegistry
from observability.admin_logs import LOG_LEVELS
from search.engine import ProductSearchEngine
from storage.usage import MonthlyUsageStore
from storage.vector import get_tenant_vector_collection

LOGGER = logging.getLogger("uvicorn.error")


__all__ = (
    "ExpiredCursorError",
    "HealthResponse",
    "InvalidCursorError",
    "PROCESS_STARTED_MONOTONIC",
    "PUBLIC_PRODUCT_FIELDS",
    "PaginationResponse",
    "ProductSearchService",
    "SearchCapacityError",
    "SearchRequest",
    "SearchResponse",
    "SearchSession",
    "SearchSessionStore",
    "TenantServicePool",
    "app",
    "create_app",
    "decode_cursor",
    "encode_cursor",
    "get_tenant_vector_collection",
    "process_monitor_status",
    "product_is_visible",
    "public_product",
)


def create_app(
    engine_factory: Callable[[], ProductSearchEngine] = ProductSearchEngine,
    service: ProductSearchService | None = None,
    tenant_registry: TenantRegistry | None = None,
    tenant_engine_factory=None,
    compatibility_factory=None,
    rate_limiter: TenantRateLimiter | None = None,
    preload_models: bool | None = None,
    usage_store: MonthlyUsageStore | None = None,
) -> FastAPI:
    lifespan = build_lifespan(
        engine_factory=engine_factory,
        service=service,
        tenant_registry=tenant_registry,
        tenant_engine_factory=tenant_engine_factory,
        compatibility_factory=compatibility_factory,
        rate_limiter=rate_limiter,
        preload_models=preload_models,
        usage_store=usage_store,
    )

    application = FastAPI(
        title=f"{APP_NAME} API",
        version="1.0.0",
        lifespan=lifespan,
    )
    if API_CORS_ORIGINS:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=API_CORS_ORIGINS,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=[
                "Content-Type",
                "Authorization",
                "X-API-Key",
                "X-User-ID",
            ],
        )

    def resolve_company_profile(
        api_key: str | None,
        *,
        company_endpoint: str | None = None,
    ) -> TenantProfile:
        if not application.state.tenant_mode:
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
        profile = application.state.tenant_registry.resolve_api_key(api_key)
        if profile is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid API key.",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        if company_endpoint is not None:
            endpoint_profile = application.state.tenant_registry.resolve_endpoint(
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
        api_key: str | None,
        *,
        apply_rate_limit: bool,
        company_endpoint: str | None = None,
    ) -> ProductSearchService:
        if not application.state.tenant_mode:
            if company_endpoint is not None:
                raise HTTPException(
                    status_code=404,
                    detail="Company endpoints require tenant mode.",
                )
            return application.state.search_service
        profile = resolve_company_profile(
            api_key,
            company_endpoint=company_endpoint,
        )
        if apply_rate_limit and API_RATE_LIMIT_ENABLED:
            allowed, _remaining = application.state.rate_limiter.allow(
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
            return application.state.tenant_service_pool.get(profile.company_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def resolve_compatibility_service(
        api_key: str | None,
        *,
        company_endpoint: str,
    ):
        search_service = resolve_service(
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

    def require_admin_key(admin_key: str | None) -> None:
        if not application.state.tenant_mode or not API_ADMIN_KEY:
            raise HTTPException(status_code=404, detail="Not found.")
        if not admin_key or not hmac.compare_digest(
            admin_key,
            API_ADMIN_KEY,
        ):
            raise HTTPException(status_code=401, detail="Invalid admin key.")

    def resolve_admin_service(
        admin_key: str | None,
        *,
        company_endpoint: str,
    ) -> ProductSearchService:
        require_admin_key(admin_key)
        profile = application.state.tenant_registry.resolve_endpoint(company_endpoint)
        if profile is None:
            raise HTTPException(status_code=404, detail="Unknown company endpoint.")
        return application.state.tenant_service_pool.get(profile.company_id)

    def company_search_request(
        company_endpoint: str,
        payload: dict[str, Any],
    ) -> SearchRequest:
        if not application.state.tenant_mode:
            raise HTTPException(
                status_code=404,
                detail="Company endpoints require tenant mode.",
            )
        profile = application.state.tenant_registry.resolve_endpoint(company_endpoint)
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail="Unknown company endpoint.",
            )
        if profile.compatibility.adapter == "gainr_legacy":
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
            search_service = resolve_service(
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

    @application.get("/api/v1/ready", tags=["system"])
    def ready():
        return readiness_response(application)

    @application.get("/api/v1/live", tags=["system"])
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get(
        "/api/v1/health",
        response_model=HealthResponse,
        tags=["system"],
    )
    def health(
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> HealthResponse:
        try:
            return resolve_service(
                x_api_key,
                apply_rate_limit=False,
            ).health()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.post(
        "/api/v1/search",
        response_model=SearchResponse,
        tags=["search"],
    )
    def search(
        request: SearchRequest,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> SearchResponse:
        return execute_search(request, x_api_key)

    @application.get(
        "/api/v1/{company_endpoint}/health",
        response_model=HealthResponse,
        tags=["company"],
    )
    def company_health(
        company_endpoint: str,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> HealthResponse:
        try:
            return resolve_service(
                x_api_key,
                apply_rate_limit=False,
                company_endpoint=company_endpoint,
            ).health()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.get(
        "/api/v1/admin/status",
        tags=["admin"],
    )
    def admin_status(
        x_admin_key: str | None = Header(
            default=None,
            alias="X-Admin-Key",
        ),
    ) -> dict[str, Any]:
        require_admin_key(x_admin_key)
        registry = application.state.tenant_registry
        loaded = application.state.tenant_service_pool.loaded_services()
        companies = []
        for company_id, profile in registry.profiles.items():
            service = loaded.get(company_id)
            companies.append(
                {
                    "company_id": company_id,
                    "endpoint_slug": (profile.endpoint_slug or profile.company_id),
                    "loaded": service is not None,
                    "health": (
                        service.health().model_dump() if service is not None else None
                    ),
                    "searches": (
                        service.monitor_status() if service is not None else None
                    ),
                }
            )
        return {
            "status": "ok",
            "process": process_monitor_status(),
            "configured_companies": len(companies),
            "loaded_companies": len(loaded),
            "companies": companies,
        }

    @application.get(
        "/api/v1/admin/logs",
        tags=["admin"],
    )
    def admin_logs(
        response: Response,
        limit: int = Query(default=100, ge=1, le=200),
        level: str = Query(
            default="INFO",
            pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$",
        ),
        after_id: int | None = Query(default=None, ge=0),
        x_admin_key: str | None = Header(
            default=None,
            alias="X-Admin-Key",
        ),
    ) -> dict[str, Any]:
        require_admin_key(x_admin_key)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        if level not in LOG_LEVELS:
            raise HTTPException(status_code=422, detail="Invalid log level.")
        return {
            "status": "ok",
            **application.state.admin_log_buffer.snapshot(
                limit=limit,
                minimum_level=level,
                after_id=after_id,
            ),
        }

    @application.get(
        "/api/v1/{company_endpoint}/admin/status",
        tags=["company-admin"],
    )
    def company_admin_status(
        company_endpoint: str,
        x_admin_key: str | None = Header(
            default=None,
            alias="X-Admin-Key",
        ),
    ) -> dict[str, Any]:
        service = resolve_admin_service(
            x_admin_key,
            company_endpoint=company_endpoint,
        )
        usage = (
            service.usage_store.summary(service.company_id or "legacy")
            if service.usage_store is not None
            else None
        )
        return {
            "status": "ok",
            "company_id": service.company_id,
            "process": process_monitor_status(),
            "health": service.health().model_dump(),
            "searches": service.monitor_status(),
            "usage": usage,
            "analytics": (
                service.analytics_store.status()
                if service.analytics_store is not None
                else None
            ),
        }

    @application.get(
        "/api/v1/{company_endpoint}/admin/search-events",
        tags=["company-admin"],
    )
    def company_admin_search_events(
        company_endpoint: str,
        limit: int = Query(default=20, ge=1, le=100),
        event_status: str | None = Query(
            default=None,
            alias="status",
            pattern="^(success|failed)$",
        ),
        x_admin_key: str | None = Header(
            default=None,
            alias="X-Admin-Key",
        ),
    ) -> dict[str, Any]:
        service = resolve_admin_service(
            x_admin_key,
            company_endpoint=company_endpoint,
        )
        return {
            "status": "ok",
            "company_id": service.company_id,
            **service.monitor_events(
                limit=limit,
                event_status=event_status,
            ),
        }

    @application.get(
        "/api/v1/{company_endpoint}/auth/verify",
        tags=["company"],
    )
    def company_auth_verify(
        company_endpoint: str,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        profile = resolve_company_profile(
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
        request = company_search_request(company_endpoint, payload)
        return execute_search(
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
            compatibility_service = resolve_compatibility_service(
                x_api_key,
                company_endpoint=company_endpoint,
            )
            request = parse_compatibility_payload(
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
            compatibility_service = resolve_compatibility_service(
                x_api_key,
                company_endpoint=company_endpoint,
            )
            request = parse_compatibility_payload(
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
        compatibility_service = resolve_compatibility_service(
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
            compatibility_service.product_search_service.record_search_failure(
                request.searchTerm,
                duration_ms=(time.perf_counter() - search_started) * 1000,
                error_type=type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            compatibility_service.product_search_service.record_search_failure(
                request.searchTerm,
                duration_ms=(time.perf_counter() - search_started) * 1000,
                error_type=type(exc).__name__,
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
        return resolve_compatibility_service(
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
            return resolve_service(
                x_api_key,
                apply_rate_limit=False,
                company_endpoint=company_endpoint,
            ).usage_summary(month)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return application


app = create_app()
