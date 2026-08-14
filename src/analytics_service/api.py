from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import (
    Cookie,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, SecretStr

from .adapters import (
    AnalyticsAdapterFactory,
    CompanyAnalyticsAdapter,
    build_analytics_adapter,
)
from .auth import (
    COMPANY_PORTAL,
    COMPANY_USER,
    INTERNAL_ADMIN,
    INTERNAL_PORTAL,
    AnalyticsAuthStore,
    AnalyticsPrincipal,
    AuthenticatedSession,
    PortalType,
)
from .config import (
    AnalyticsRegistry,
    AnalyticsSettings,
    CompanyAnalyticsConfig,
    load_analytics_registry,
)
from .dashboard_filters import DashboardFilters, build_dashboard_overview
from .schedule import REFRESH_SCHEDULE
from .store import AnalyticsSnapshotStore


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=191)
    password: SecretStr = Field(min_length=1, max_length=1024)


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
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def remote_address(request: Request) -> str | None:
        return request.client.host if request.client is not None else None

    def validate_browser_origin(request: Request) -> None:
        origin = request.headers.get("Origin")
        if origin is not None and origin not in active_settings.cors_origins:
            raise HTTPException(
                status_code=403,
                detail="Request origin is not allowed.",
            )

    def portal_cookie_name(portal_type: PortalType) -> str:
        if portal_type == COMPANY_PORTAL:
            return active_settings.company_session_cookie_name
        return active_settings.internal_session_cookie_name

    def set_portal_cookie(
        response: Response,
        *,
        token: str,
        principal: AnalyticsPrincipal,
    ) -> None:
        response.set_cookie(
            key=portal_cookie_name(principal.portal_type),
            value=token,
            max_age=principal.session_max_age_seconds,
            expires=datetime.fromisoformat(principal.session_expires_at),
            httponly=True,
            secure=active_settings.session_cookie_secure,
            samesite="lax",
            path="/",
        )

    def delete_portal_cookie(
        response: Response,
        portal_type: PortalType,
    ) -> None:
        response.delete_cookie(
            key=portal_cookie_name(portal_type),
            path="/",
            secure=active_settings.session_cookie_secure,
            httponly=True,
            samesite="lax",
        )

    def set_legacy_cookie(
        response: Response,
        *,
        token: str,
        principal: AnalyticsPrincipal,
    ) -> None:
        response.set_cookie(
            key=active_settings.session_cookie_name,
            value=token,
            max_age=principal.session_max_age_seconds,
            expires=datetime.fromisoformat(principal.session_expires_at),
            httponly=True,
            secure=active_settings.session_cookie_secure,
            samesite="strict",
            path="/api/v1",
        )

    def authenticate(
        credentials: LoginRequest,
        request: Request,
        *,
        required_role: str | None,
    ) -> AuthenticatedSession | None:
        try:
            return active_auth_store.authenticate(
                username=credentials.username,
                password=credentials.password.get_secret_value(),
                required_role=required_role,
                remote_address=remote_address(request),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Authentication service is unavailable.",
            ) from exc

    def session_principal(
        session_token: str | None,
        *,
        portal_type: PortalType | None = None,
    ) -> AnalyticsPrincipal | None:
        try:
            return active_auth_store.resolve_session(
                session_token,
                portal_type=portal_type,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Authentication service is unavailable.",
            ) from exc

    def revoke_session(
        session_token: str | None,
        request: Request,
        *,
        portal_type: PortalType | None = None,
    ) -> None:
        try:
            active_auth_store.revoke_session(
                session_token,
                portal_type=portal_type,
                remote_address=remote_address(request),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Authentication service is unavailable.",
            ) from exc

    def require_user(
        session_token: str | None,
        *,
        portal_type: PortalType | None = None,
    ) -> AnalyticsPrincipal:
        principal = session_principal(
            session_token,
            portal_type=portal_type,
        )
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail="Authentication required.",
            )
        return principal

    def require_admin(
        session_token: str | None,
        response: Response,
    ) -> AnalyticsPrincipal:
        principal = require_user(
            session_token,
            portal_type=INTERNAL_PORTAL,
        )
        if not principal.internal:
            raise HTTPException(
                status_code=403,
                detail="Internal analytics access is required.",
            )
        set_portal_cookie(
            response,
            token=session_token or "",
            principal=principal,
        )
        return principal

    def require_company(
        company_endpoint: str,
        api_key: str | None,
        session_token: str | None,
        response: Response,
    ) -> CompanyAnalyticsConfig:
        company = active_registry.resolve_endpoint(company_endpoint)
        if company is None:
            raise HTTPException(
                status_code=404,
                detail="Unknown company analytics endpoint.",
            )
        principal = session_principal(
            session_token,
            portal_type=COMPANY_PORTAL,
        )
        if principal is not None:
            if principal.internal or principal.company_id != company.company_id:
                raise HTTPException(
                    status_code=403,
                    detail="Session is not authorized for this company.",
                )
            set_portal_cookie(
                response,
                token=session_token or "",
                principal=principal,
            )
            return company
        if api_key:
            authenticated = active_registry.authenticate(
                company_endpoint,
                api_key,
            )
            if authenticated is not None:
                return authenticated
        if not api_key:
            raise HTTPException(
                status_code=401,
                detail="Authentication required.",
            )
        else:
            raise HTTPException(
                status_code=403,
                detail="API key does not match the company endpoint.",
            )

    def require_admin_company(
        company_endpoint: str,
        session_token: str | None,
        response: Response,
    ) -> CompanyAnalyticsConfig:
        require_admin(session_token, response)
        company = active_registry.resolve_endpoint(company_endpoint)
        if company is None:
            raise HTTPException(
                status_code=404,
                detail="Unknown company analytics endpoint.",
            )
        return company

    def company_adapter(
        company: CompanyAnalyticsConfig,
    ) -> CompanyAnalyticsAdapter:
        try:
            return active_adapters[company.company_id]
        except KeyError as exc:
            raise HTTPException(
                status_code=503,
                detail="Company analytics adapter is unavailable.",
            ) from exc

    def get_dashboard(
        company: CompanyAnalyticsConfig,
        *,
        internal: bool,
        filters: DashboardFilters,
    ) -> dict[str, Any]:
        dashboard = active_store.dashboard(
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
            activity = build_dashboard_overview(
                list(
                    active_store.dashboard_activity_records(
                        company.company_id,
                        internal=internal,
                    )
                ),
                internal=internal,
                filters=filters,
                timezone_name=company.timezone,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {**dashboard, **activity}

    def get_queries(
        company: CompanyAnalyticsConfig,
        *,
        internal: bool,
        limit: int,
        cursor: str | None,
        query: str | None,
        outcome: str | None,
        category: str | None,
        execution_path: str | None,
        language: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
    ) -> dict[str, Any]:
        if not active_store.company_status(company.company_id)["has_snapshot"]:
            raise HTTPException(
                status_code=503,
                detail=(
                    "No completed analytics snapshot is available. "
                    "Run the analytics refresh."
                ),
            )
        try:
            return active_store.query_records(
                company.company_id,
                internal=internal,
                limit=limit,
                cursor=cursor,
                query=query,
                outcome=outcome,
                category=category,
                execution_path=execution_path if internal else None,
                language=language,
                created_from=(created_from.isoformat() if created_from else None),
                created_to=created_to.isoformat() if created_to else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.get("/api/v1/live", tags=["system"])
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/v1/ready", tags=["system"])
    def ready(response: Response) -> dict[str, Any]:
        statuses = [
            active_store.company_status(company_id)
            for company_id in active_registry.companies
        ]
        ready_now = bool(statuses) and all(
            status["has_snapshot"] for status in statuses
        )
        if not ready_now:
            response.status_code = 503
        return {
            "status": "ok" if ready_now else "not_ready",
            "configured_companies": len(statuses),
            "companies_with_snapshots": sum(
                int(status["has_snapshot"]) for status in statuses
            ),
            "refresh_schedule": REFRESH_SCHEDULE,
        }

    def session_payload(principal: AnalyticsPrincipal) -> dict[str, Any]:
        return {
            "user": {
                "username": principal.username,
                "role": principal.role,
                "company_id": principal.company_id,
            },
            "expires_at": principal.session_expires_at,
        }

    def role_login(
        credentials: LoginRequest,
        request: Request,
        response: Response,
        *,
        required_role: str,
    ) -> dict[str, Any]:
        validate_browser_origin(request)
        authenticated = authenticate(
            credentials,
            request,
            required_role=required_role,
        )
        if authenticated is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid username or password.",
            )
        set_portal_cookie(
            response,
            token=authenticated.token,
            principal=authenticated.principal,
        )
        return session_payload(authenticated.principal)

    @application.post(
        "/api/v1/analytics/company/auth/login",
        tags=["authentication"],
    )
    def company_login(
        credentials: LoginRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        return role_login(
            credentials,
            request,
            response,
            required_role=COMPANY_USER,
        )

    @application.get(
        "/api/v1/analytics/company/auth/me",
        tags=["authentication"],
    )
    def company_current_user(
        response: Response,
        analytics_session: str | None = Cookie(
            default=None,
            alias=active_settings.company_session_cookie_name,
        ),
    ) -> dict[str, Any]:
        principal = require_user(
            analytics_session,
            portal_type=COMPANY_PORTAL,
        )
        set_portal_cookie(
            response,
            token=analytics_session or "",
            principal=principal,
        )
        return session_payload(principal)

    @application.post(
        "/api/v1/analytics/company/auth/logout",
        tags=["authentication"],
    )
    def company_logout(
        request: Request,
        response: Response,
        analytics_session: str | None = Cookie(
            default=None,
            alias=active_settings.company_session_cookie_name,
        ),
    ) -> dict[str, bool]:
        validate_browser_origin(request)
        revoke_session(
            analytics_session,
            request,
            portal_type=COMPANY_PORTAL,
        )
        delete_portal_cookie(response, COMPANY_PORTAL)
        return {"logged_out": True}

    @application.post(
        "/api/v1/analytics/internal/auth/login",
        tags=["authentication"],
    )
    def internal_login(
        credentials: LoginRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        return role_login(
            credentials,
            request,
            response,
            required_role=INTERNAL_ADMIN,
        )

    @application.get(
        "/api/v1/analytics/internal/auth/me",
        tags=["authentication"],
    )
    def internal_current_user(
        response: Response,
        analytics_session: str | None = Cookie(
            default=None,
            alias=active_settings.internal_session_cookie_name,
        ),
    ) -> dict[str, Any]:
        principal = require_user(
            analytics_session,
            portal_type=INTERNAL_PORTAL,
        )
        set_portal_cookie(
            response,
            token=analytics_session or "",
            principal=principal,
        )
        return session_payload(principal)

    @application.post(
        "/api/v1/analytics/internal/auth/logout",
        tags=["authentication"],
    )
    def internal_logout(
        request: Request,
        response: Response,
        analytics_session: str | None = Cookie(
            default=None,
            alias=active_settings.internal_session_cookie_name,
        ),
    ) -> dict[str, bool]:
        validate_browser_origin(request)
        revoke_session(
            analytics_session,
            request,
            portal_type=INTERNAL_PORTAL,
        )
        delete_portal_cookie(response, INTERNAL_PORTAL)
        return {"logged_out": True}

    # Deprecated compatibility endpoints. Remove only after both frontend
    # portals have rolled out the role-specific authentication endpoints.
    @application.post("/api/v1/analytics/auth/login", tags=["authentication"])
    def login(
        credentials: LoginRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        validate_browser_origin(request)
        authenticated = authenticate(
            credentials,
            request,
            required_role=None,
        )
        if authenticated is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid username or password.",
            )
        set_legacy_cookie(
            response,
            token=authenticated.token,
            principal=authenticated.principal,
        )
        set_portal_cookie(
            response,
            token=authenticated.token,
            principal=authenticated.principal,
        )
        return session_payload(authenticated.principal)

    @application.get("/api/v1/analytics/auth/me", tags=["authentication"])
    def current_user(
        response: Response,
        analytics_session: str | None = Cookie(
            default=None,
            alias=active_settings.session_cookie_name,
        ),
    ) -> dict[str, Any]:
        principal = require_user(analytics_session)
        set_legacy_cookie(
            response,
            token=analytics_session or "",
            principal=principal,
        )
        set_portal_cookie(
            response,
            token=analytics_session or "",
            principal=principal,
        )
        return session_payload(principal)

    @application.post("/api/v1/analytics/auth/logout", tags=["authentication"])
    def logout(
        request: Request,
        response: Response,
        analytics_session: str | None = Cookie(
            default=None,
            alias=active_settings.session_cookie_name,
        ),
    ) -> dict[str, bool]:
        validate_browser_origin(request)
        principal = session_principal(analytics_session)
        revoke_session(analytics_session, request)
        response.delete_cookie(
            key=active_settings.session_cookie_name,
            path="/api/v1",
            secure=active_settings.session_cookie_secure,
            httponly=True,
            samesite="strict",
        )
        if principal is not None:
            delete_portal_cookie(response, principal.portal_type)
        return {"logged_out": True}

    @application.get(
        "/api/v1/admin/analytics/companies",
        tags=["internal-analytics"],
    )
    def admin_companies(
        response: Response,
        analytics_session: str | None = Cookie(
            default=None,
            alias=active_settings.internal_session_cookie_name,
        ),
    ) -> dict[str, Any]:
        require_admin(analytics_session, response)
        return {
            "companies": [
                {
                    "company_id": company.company_id,
                    "endpoint_slug": company.endpoint_slug,
                    **active_store.company_status(company.company_id),
                }
                for company in active_registry.companies.values()
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
            alias=active_settings.internal_session_cookie_name,
        ),
    ) -> dict[str, Any]:
        company = require_admin_company(
            company_endpoint,
            analytics_session,
            response,
        )
        return get_dashboard(
            company,
            internal=True,
            filters=DashboardFilters(
                period=period,
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
            default=active_settings.query_page_size,
            ge=1,
            le=active_settings.query_max_page_size,
        ),
        cursor: str | None = Query(default=None, max_length=1024),
        query: str | None = Query(default=None, max_length=1000),
        outcome: str | None = Query(
            default=None,
            pattern="^(fulfilled|zero_result|failure|telemetry_missing)$",
        ),
        category: str | None = Query(default=None, max_length=191),
        execution_path: str | None = Query(default=None, max_length=128),
        language: str | None = Query(default=None, max_length=64),
        created_from: datetime | None = Query(default=None, alias="from"),
        created_to: datetime | None = Query(default=None, alias="to"),
        analytics_session: str | None = Cookie(
            default=None,
            alias=active_settings.internal_session_cookie_name,
        ),
    ) -> dict[str, Any]:
        company = require_admin_company(
            company_endpoint,
            analytics_session,
            response,
        )
        return get_queries(
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
        )

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
            alias=active_settings.company_session_cookie_name,
        ),
    ) -> dict[str, Any]:
        company = require_company(
            company_endpoint,
            x_api_key,
            analytics_session,
            response,
        )
        return company_adapter(company).dashboard_response(
            get_dashboard(
                company,
                internal=False,
                filters=DashboardFilters(
                    period=period,
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
            default=active_settings.query_page_size,
            ge=1,
            le=active_settings.query_max_page_size,
        ),
        cursor: str | None = Query(default=None, max_length=1024),
        query: str | None = Query(default=None, max_length=1000),
        outcome: str | None = Query(
            default=None,
            pattern="^(fulfilled|zero_result|failure|telemetry_missing)$",
        ),
        category: str | None = Query(default=None, max_length=191),
        language: str | None = Query(default=None, max_length=64),
        created_from: datetime | None = Query(default=None, alias="from"),
        created_to: datetime | None = Query(default=None, alias="to"),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        analytics_session: str | None = Cookie(
            default=None,
            alias=active_settings.company_session_cookie_name,
        ),
    ) -> dict[str, Any]:
        company = require_company(
            company_endpoint,
            x_api_key,
            analytics_session,
            response,
        )
        return company_adapter(company).queries_response(
            get_queries(
                company,
                internal=False,
                limit=limit,
                cursor=cursor,
                query=query,
                outcome=outcome,
                category=category,
                execution_path=None,
                language=language,
                created_from=created_from,
                created_to=created_to,
            )
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
            alias=active_settings.company_session_cookie_name,
        ),
    ) -> dict[str, Any]:
        company = require_company(
            company_endpoint,
            x_api_key,
            analytics_session,
            response,
        )
        status = active_store.company_status(company.company_id)
        response_payload = {
            "company_id": status["company_id"],
            "has_snapshot": status["has_snapshot"],
            "snapshot": status["snapshot"],
            "refresh_schedule": status["refresh_schedule"],
        }
        return company_adapter(company).status_response(response_payload)

    return application
