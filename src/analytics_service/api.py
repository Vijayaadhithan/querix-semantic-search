"""Construct the analytics application and its application dependencies."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .access import AnalyticsAccess, LoginRequest
from .adapters import AnalyticsAdapterFactory, build_analytics_adapter
from .auth import AnalyticsAuthStore
from .config import AnalyticsRegistry, AnalyticsSettings, load_analytics_registry
from .dashboard import AnalyticsDashboardService
from .routes.auth import register_auth_routes
from .routes.company import register_company_routes
from .routes.internal import register_internal_routes
from .routes.system import register_system_routes
from .store import AnalyticsSnapshotStore

__all__ = ("LoginRequest", "create_app")


def create_app(
    *,
    settings: AnalyticsSettings | None = None,
    registry: AnalyticsRegistry | None = None,
    store: AnalyticsSnapshotStore | None = None,
    auth_store: AnalyticsAuthStore | None = None,
    analytics_adapter_factory: AnalyticsAdapterFactory | None = None,
) -> FastAPI:
    active_settings = settings or AnalyticsSettings.from_env()
    active_registry = registry or load_analytics_registry(
        active_settings.tenant_config_dir
    )
    active_store = store or AnalyticsSnapshotStore(active_settings.snapshot_db_path)
    active_auth_store = auth_store or AnalyticsAuthStore(
        active_settings.snapshot_db_path,
        session_ttl_seconds=active_settings.session_ttl_seconds,
        company_session_idle_seconds=(active_settings.company_session_idle_seconds),
        company_session_absolute_seconds=(
            active_settings.company_session_absolute_seconds
        ),
        internal_session_idle_seconds=(active_settings.internal_session_idle_seconds),
        internal_session_absolute_seconds=(
            active_settings.internal_session_absolute_seconds
        ),
        max_login_attempts=active_settings.login_max_attempts,
        lock_seconds=active_settings.login_lock_seconds,
        password_min_length=active_settings.password_min_length,
    )
    active_adapters = {
        company.company_id: (
            analytics_adapter_factory(company)
            if analytics_adapter_factory is not None
            else build_analytics_adapter(company.adapter, company)
        )
        for company in active_registry.companies.values()
    }

    application = FastAPI(
        title="Company Analytics API",
        version="1.0.0",
        description=("Daily company dashboards and internal operational analytics."),
    )
    application.state.settings = active_settings
    application.state.registry = active_registry
    application.state.store = active_store
    application.state.auth_store = active_auth_store
    application.state.analytics_adapters = active_adapters

    if active_settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(active_settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )

    @application.middleware("http")
    async def protect_analytics_responses(request: Request, call_next):
        response = await call_next(request)
        if "/analytics/" in request.url.path:
            if request.url.path.endswith("/query-facets"):
                response.headers.setdefault(
                    "Cache-Control",
                    "private, max-age=300, must-revalidate",
                )
            else:
                response.headers["Cache-Control"] = "private, no-store"
                response.headers["Pragma"] = "no-cache"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    access = AnalyticsAccess(active_settings, active_registry, active_auth_store)
    dashboards = AnalyticsDashboardService(active_store, active_adapters)
    register_system_routes(
        application, active_registry, active_store, active_auth_store
    )
    register_auth_routes(application, active_settings, active_registry, access)
    register_internal_routes(
        application,
        active_settings,
        active_registry,
        active_store,
        access,
        dashboards,
    )
    register_company_routes(
        application,
        active_settings,
        active_store,
        access,
        dashboards,
    )
    return application
