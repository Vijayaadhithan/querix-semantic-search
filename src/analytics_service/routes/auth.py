"""Auth analytics HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import Cookie, FastAPI, HTTPException, Request, Response

from ..access import AnalyticsAccess, LoginRequest
from ..auth import (
    COMPANY_PORTAL,
    COMPANY_USER,
    INTERNAL_ADMIN,
    INTERNAL_PORTAL,
)
from ..config import AnalyticsRegistry, AnalyticsSettings


def register_auth_routes(
    application: FastAPI,
    settings: AnalyticsSettings,
    registry: AnalyticsRegistry,
    access: AnalyticsAccess,
) -> None:
    @application.post(
        "/api/v1/{company_endpoint}/analytics/auth/login",
        tags=["authentication"],
    )
    def company_login(
        company_endpoint: str,
        credentials: LoginRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        company = registry.resolve_endpoint(company_endpoint)
        if company is None:
            raise HTTPException(
                status_code=404,
                detail="Unknown company analytics endpoint.",
            )
        return access.role_login(
            credentials,
            request,
            response,
            required_role=COMPANY_USER,
            required_company_id=company.company_id,
        )

    @application.get(
        "/api/v1/analytics/company/auth/me",
        tags=["authentication"],
    )
    def company_current_user(
        response: Response,
        analytics_session: str | None = Cookie(
            default=None,
            alias=settings.company_session_cookie_name,
        ),
    ) -> dict[str, Any]:
        principal = access.require_user(
            analytics_session,
            portal_type=COMPANY_PORTAL,
        )
        access.set_portal_cookie(
            response,
            token=analytics_session or "",
            principal=principal,
        )
        return access.session_payload(principal)

    @application.post(
        "/api/v1/analytics/company/auth/logout",
        tags=["authentication"],
    )
    def company_logout(
        request: Request,
        response: Response,
        analytics_session: str | None = Cookie(
            default=None,
            alias=settings.company_session_cookie_name,
        ),
    ) -> dict[str, bool]:
        access.validate_browser_origin(request)
        access.revoke_session(
            analytics_session,
            request,
            portal_type=COMPANY_PORTAL,
        )
        access.delete_portal_cookie(response, COMPANY_PORTAL)
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
        return access.role_login(
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
            alias=settings.internal_session_cookie_name,
        ),
    ) -> dict[str, Any]:
        principal = access.require_user(
            analytics_session,
            portal_type=INTERNAL_PORTAL,
        )
        access.set_portal_cookie(
            response,
            token=analytics_session or "",
            principal=principal,
        )
        return access.session_payload(principal)

    @application.post(
        "/api/v1/analytics/internal/auth/logout",
        tags=["authentication"],
    )
    def internal_logout(
        request: Request,
        response: Response,
        analytics_session: str | None = Cookie(
            default=None,
            alias=settings.internal_session_cookie_name,
        ),
    ) -> dict[str, bool]:
        access.validate_browser_origin(request)
        access.revoke_session(
            analytics_session,
            request,
            portal_type=INTERNAL_PORTAL,
        )
        access.delete_portal_cookie(response, INTERNAL_PORTAL)
        return {"logged_out": True}
