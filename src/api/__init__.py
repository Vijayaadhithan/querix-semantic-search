from collections.abc import Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.context import LOGGER as LOGGER
from api.context import SearchRequestContext
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
from api.routes.admin import register_admin_routes
from api.routes.company import register_company_routes
from api.routes.system import register_system_routes
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
    APP_NAME,
)
from core.tenant_config import TenantRegistry
from search.engine import ProductSearchEngine
from storage.usage import MonthlyUsageStore
from storage.vector import get_tenant_vector_collection

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

    context = SearchRequestContext(application, admin_key=lambda: API_ADMIN_KEY)
    register_system_routes(application, context)
    register_admin_routes(application, context)
    register_company_routes(application, context)
    return application


app = create_app()
