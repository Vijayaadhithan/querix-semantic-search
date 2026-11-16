"""Portal authentication, authorization, and cookie policy."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr

from .auth import (
    COMPANY_PORTAL,
    INTERNAL_PORTAL,
    AnalyticsAuthStore,
    AnalyticsPrincipal,
    AuthenticatedSession,
    PortalType,
)
from .config import AnalyticsRegistry, AnalyticsSettings, CompanyAnalyticsConfig


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=191)
    password: SecretStr = Field(min_length=1, max_length=1024)


class AnalyticsAccess:
    """Keep portal policy shared across company and internal routes."""

    def __init__(
        self,
        settings: AnalyticsSettings,
        registry: AnalyticsRegistry,
        auth_store: AnalyticsAuthStore,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.auth_store = auth_store

    def remote_address(self, request: Request) -> str | None:
        return request.client.host if request.client is not None else None

    def validate_browser_origin(self, request: Request) -> None:
        origin = request.headers.get("Origin")
        if origin is not None and origin not in self.settings.cors_origins:
            raise HTTPException(
                status_code=403,
                detail="Request origin is not allowed.",
            )

    def portal_cookie_name(self, portal_type: PortalType) -> str:
        if portal_type == COMPANY_PORTAL:
            return self.settings.company_session_cookie_name
        return self.settings.internal_session_cookie_name

    def set_portal_cookie(
        self,
        response: Response,
        *,
        token: str,
        principal: AnalyticsPrincipal,
    ) -> None:
        response.set_cookie(
            key=self.portal_cookie_name(principal.portal_type),
            value=token,
            max_age=principal.session_max_age_seconds,
            expires=datetime.fromisoformat(principal.session_expires_at),
            httponly=True,
            secure=self.settings.session_cookie_secure,
            samesite="lax",
            path="/",
        )

    def delete_portal_cookie(
        self,
        response: Response,
        portal_type: PortalType,
    ) -> None:
        response.delete_cookie(
            key=self.portal_cookie_name(portal_type),
            path="/",
            secure=self.settings.session_cookie_secure,
            httponly=True,
            samesite="lax",
        )

    def authenticate(
        self,
        credentials: LoginRequest,
        request: Request,
        *,
        required_role: str | None,
        required_company_id: str | None = None,
    ) -> AuthenticatedSession | None:
        try:
            return self.auth_store.authenticate(
                username=credentials.username,
                password=credentials.password.get_secret_value(),
                required_role=required_role,
                required_company_id=required_company_id,
                remote_address=self.remote_address(request),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Authentication service is unavailable.",
            ) from exc

    def session_principal(
        self,
        session_token: str | None,
        *,
        portal_type: PortalType | None = None,
    ) -> AnalyticsPrincipal | None:
        try:
            return self.auth_store.resolve_session(
                session_token,
                portal_type=portal_type,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Authentication service is unavailable.",
            ) from exc

    def revoke_session(
        self,
        session_token: str | None,
        request: Request,
        *,
        portal_type: PortalType | None = None,
    ) -> None:
        try:
            self.auth_store.revoke_session(
                session_token,
                portal_type=portal_type,
                remote_address=self.remote_address(request),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Authentication service is unavailable.",
            ) from exc

    def require_user(
        self,
        session_token: str | None,
        *,
        portal_type: PortalType | None = None,
    ) -> AnalyticsPrincipal:
        principal = self.session_principal(
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
        self,
        session_token: str | None,
        response: Response,
    ) -> AnalyticsPrincipal:
        principal = self.require_user(
            session_token,
            portal_type=INTERNAL_PORTAL,
        )
        if not principal.internal:
            raise HTTPException(
                status_code=403,
                detail="Internal analytics access is required.",
            )
        self.set_portal_cookie(
            response,
            token=session_token or "",
            principal=principal,
        )
        return principal

    def require_company(
        self,
        company_endpoint: str,
        api_key: str | None,
        session_token: str | None,
        response: Response,
    ) -> CompanyAnalyticsConfig:
        company = self.registry.resolve_endpoint(company_endpoint)
        if company is None:
            raise HTTPException(
                status_code=404,
                detail="Unknown company analytics endpoint.",
            )
        principal = self.session_principal(
            session_token,
            portal_type=COMPANY_PORTAL,
        )
        if principal is not None:
            if principal.internal or principal.company_id != company.company_id:
                raise HTTPException(
                    status_code=403,
                    detail="Session is not authorized for this company.",
                )
            self.set_portal_cookie(
                response,
                token=session_token or "",
                principal=principal,
            )
            return company
        if api_key:
            authenticated = self.registry.authenticate(
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
        self,
        company_endpoint: str,
        session_token: str | None,
        response: Response,
    ) -> CompanyAnalyticsConfig:
        self.require_admin(session_token, response)
        company = self.registry.resolve_endpoint(company_endpoint)
        if company is None:
            raise HTTPException(
                status_code=404,
                detail="Unknown company analytics endpoint.",
            )
        return company

    def session_payload(self, principal: AnalyticsPrincipal) -> dict[str, Any]:
        return {
            "user": {
                "username": principal.username,
                "role": principal.role,
                "company_id": principal.company_id,
            },
            "expires_at": principal.session_expires_at,
        }

    def role_login(
        self,
        credentials: LoginRequest,
        request: Request,
        response: Response,
        *,
        required_role: str,
        required_company_id: str | None = None,
    ) -> dict[str, Any]:
        self.validate_browser_origin(request)
        authenticated = self.authenticate(
            credentials,
            request,
            required_role=required_role,
            required_company_id=required_company_id,
        )
        if authenticated is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid username or password.",
            )
        self.set_portal_cookie(
            response,
            token=authenticated.token,
            principal=authenticated.principal,
        )
        return self.session_payload(authenticated.principal)
