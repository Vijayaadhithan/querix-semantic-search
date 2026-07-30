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

from .auth import AnalyticsAuthStore, AnalyticsPrincipal
from .config import (
    AnalyticsRegistry,
    AnalyticsSettings,
    CompanyAnalyticsConfig,
    load_analytics_registry,
)
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
) -> FastAPI:
    active_settings = settings or AnalyticsSettings.from_env()
    active_registry = registry or load_analytics_registry(
        active_settings.tenant_config_dir
    )
    active_store = store or AnalyticsSnapshotStore(
        active_settings.snapshot_db_path
    )
    active_auth_store = auth_store or AnalyticsAuthStore(
        active_settings.snapshot_db_path,
        session_ttl_seconds=active_settings.session_ttl_seconds,
        max_login_attempts=active_settings.login_max_attempts,
        lock_seconds=active_settings.login_lock_seconds,
        password_min_length=active_settings.password_min_length,
    )
    application = FastAPI(
        title="Company Analytics API",
        version="1.0.0",
        description=(
            "Daily company dashboards and internal operational analytics."
        ),
    )
    application.state.settings = active_settings
    application.state.registry = active_registry
    application.state.store = active_store
    application.state.auth_store = active_auth_store

    if active_settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(active_settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-API-Key"],
        )

    @application.middleware("http")
    async def protect_analytics_responses(request: Request, call_next):
        response = await call_next(request)
        if "/analytics/" in request.url.path:
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def session_principal(
        session_token: str | None,
    ) -> AnalyticsPrincipal | None:
        return active_auth_store.resolve_session(session_token)

    def require_user(
        session_token: str | None,
    ) -> AnalyticsPrincipal:
        principal = session_principal(session_token)
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail="Authentication required.",
            )
        return principal

    def require_admin(
        session_token: str | None,
    ) -> AnalyticsPrincipal:
        principal = require_user(session_token)
        if not principal.internal:
            raise HTTPException(
                status_code=403,
                detail="Internal analytics access is required.",
            )
        return principal

    def require_company(
        company_endpoint: str,
        api_key: str | None,
        session_token: str | None,
    ) -> CompanyAnalyticsConfig:
        company = active_registry.resolve_endpoint(company_endpoint)
        if company is None:
            raise HTTPException(
                status_code=404,
                detail="Unknown company analytics endpoint.",
            )
        principal = session_principal(session_token)
        if principal is not None:
            if (
                principal.internal
                or principal.company_id != company.company_id
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Session is not authorized for this company.",
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
    ) -> CompanyAnalyticsConfig:
        require_admin(session_token)
        company = active_registry.resolve_endpoint(company_endpoint)
        if company is None:
            raise HTTPException(
                status_code=404,
                detail="Unknown company analytics endpoint.",
            )
        return company

    def get_dashboard(
        company: CompanyAnalyticsConfig,
        *,
        internal: bool,
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
                    "Run the daily analytics refresh."
                ),
            )
        return dashboard

    def get_queries(
        company: CompanyAnalyticsConfig,
        *,
        internal: bool,
        limit: int,
        cursor: str | None,
        query: str | None,
        outcome: str | None,
        category: str | None,
        language: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
    ) -> dict[str, Any]:
        if not active_store.company_status(company.company_id)["has_snapshot"]:
            raise HTTPException(
                status_code=503,
                detail=(
                    "No completed analytics snapshot is available. "
                    "Run the daily analytics refresh."
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
                language=language,
                created_from=(
                    created_from.isoformat() if created_from else None
                ),
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
            "refresh_schedule": "daily at 03:00 Asia/Kolkata",
        }

    @application.post("/api/v1/analytics/auth/login", tags=["authentication"])
    def login(
        credentials: LoginRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        authenticated = active_auth_store.authenticate(
            username=credentials.username,
            password=credentials.password.get_secret_value(),
            remote_address=(
                request.client.host if request.client is not None else None
            ),
        )
        if authenticated is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid username or password.",
            )
        response.set_cookie(
            key=active_settings.session_cookie_name,
            value=authenticated.token,
            max_age=active_settings.session_ttl_seconds,
            httponly=True,
            secure=active_settings.session_cookie_secure,
            samesite="strict",
            path="/api/v1",
        )
        principal = authenticated.principal
        return {
            "user": {
                "username": principal.username,
                "role": principal.role,
                "company_id": principal.company_id,
            },
            "expires_at": principal.session_expires_at,
        }

    @application.get("/api/v1/analytics/auth/me", tags=["authentication"])
    def current_user(
        analytics_session: str | None = Cookie(
            default=None,
            alias=active_settings.session_cookie_name,
        ),
    ) -> dict[str, Any]:
        principal = require_user(analytics_session)
        return {
            "user": {
                "username": principal.username,
                "role": principal.role,
                "company_id": principal.company_id,
            },
            "expires_at": principal.session_expires_at,
        }

    @application.post("/api/v1/analytics/auth/logout", tags=["authentication"])
    def logout(
        request: Request,
        response: Response,
        analytics_session: str | None = Cookie(
            default=None,
            alias=active_settings.session_cookie_name,
        ),
    ) -> dict[str, bool]:
        active_auth_store.revoke_session(
            analytics_session,
            remote_address=(
                request.client.host if request.client is not None else None
            ),
        )
        response.delete_cookie(
            key=active_settings.session_cookie_name,
            path="/api/v1",
            secure=active_settings.session_cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return {"logged_out": True}

    @application.get(
        "/api/v1/admin/analytics/companies",
        tags=["internal-analytics"],
    )
    def admin_companies(
        analytics_session: str | None = Cookie(
            default=None,
            alias=active_settings.session_cookie_name,
        ),
    ) -> dict[str, Any]:
        require_admin(analytics_session)
        return {
            "companies": [
                {
                    "company_id": company.company_id,
                    "endpoint_slug": company.endpoint_slug,
                    **active_store.company_status(company.company_id),
                }
                for company in active_registry.companies.values()
            ],
            "refresh_schedule": "daily at 03:00 Asia/Kolkata",
        }

    @application.get(
        "/api/v1/admin/analytics/{company_endpoint}/dashboard",
        tags=["internal-analytics"],
    )
    def admin_dashboard(
        company_endpoint: str,
        analytics_session: str | None = Cookie(
            default=None,
            alias=active_settings.session_cookie_name,
        ),
    ) -> dict[str, Any]:
        company = require_admin_company(
            company_endpoint,
            analytics_session,
        )
        return get_dashboard(company, internal=True)

    @application.get(
        "/api/v1/admin/analytics/{company_endpoint}/queries",
        tags=["internal-analytics"],
    )
    def admin_queries(
        company_endpoint: str,
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
        analytics_session: str | None = Cookie(
            default=None,
            alias=active_settings.session_cookie_name,
        ),
    ) -> dict[str, Any]:
        company = require_admin_company(
            company_endpoint,
            analytics_session,
        )
        return get_queries(
            company,
            internal=True,
            limit=limit,
            cursor=cursor,
            query=query,
            outcome=outcome,
            category=category,
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
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        analytics_session: str | None = Cookie(
            default=None,
            alias=active_settings.session_cookie_name,
        ),
    ) -> dict[str, Any]:
        company = require_company(
            company_endpoint,
            x_api_key,
            analytics_session,
        )
        return get_dashboard(company, internal=False)

    @application.get(
        "/api/v1/{company_endpoint}/analytics/queries",
        tags=["company-analytics"],
    )
    def company_queries(
        company_endpoint: str,
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
            alias=active_settings.session_cookie_name,
        ),
    ) -> dict[str, Any]:
        company = require_company(
            company_endpoint,
            x_api_key,
            analytics_session,
        )
        return get_queries(
            company,
            internal=False,
            limit=limit,
            cursor=cursor,
            query=query,
            outcome=outcome,
            category=category,
            language=language,
            created_from=created_from,
            created_to=created_to,
        )

    @application.get(
        "/api/v1/{company_endpoint}/analytics/status",
        tags=["company-analytics"],
    )
    def company_status(
        company_endpoint: str,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        analytics_session: str | None = Cookie(
            default=None,
            alias=active_settings.session_cookie_name,
        ),
    ) -> dict[str, Any]:
        company = require_company(
            company_endpoint,
            x_api_key,
            analytics_session,
        )
        status = active_store.company_status(company.company_id)
        return {
            "company_id": status["company_id"],
            "has_snapshot": status["has_snapshot"],
            "snapshot": status["snapshot"],
            "refresh_schedule": status["refresh_schedule"],
        }

    return application
