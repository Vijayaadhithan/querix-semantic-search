"""Admin search HTTP routes."""

from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Response

from api.context import SearchRequestContext
from api.contracts import (
    process_monitor_status,
)
from observability.admin_logs import LOG_LEVELS


def register_admin_routes(application: FastAPI, context: SearchRequestContext) -> None:
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
        context.require_admin_key(x_admin_key)
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
                    "index_generation": (
                        getattr(service, "index_generation", None)
                        if service is not None
                        else None
                    ),
                    "index_generation_slot": (
                        getattr(service, "index_generation_slot", None)
                        if service is not None
                        else None
                    ),
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
        context.require_admin_key(x_admin_key)
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

    @application.post(
        "/api/v1/{company_endpoint}/admin/reload-index",
        tags=["company-admin"],
    )
    def company_admin_reload_index(
        company_endpoint: str,
        slot: str | None = Query(default=None, pattern="^(a|b)$"),
        generation: str | None = Query(default=None, min_length=1, max_length=128),
        x_admin_key: str | None = Header(
            default=None,
            alias="X-Admin-Key",
        ),
    ) -> dict[str, Any]:
        context.require_admin_key(x_admin_key)
        profile = application.state.tenant_registry.resolve_endpoint(company_endpoint)
        if profile is None:
            raise HTTPException(status_code=404, detail="Unknown company endpoint.")
        try:
            if slot is not None or generation is not None:
                if slot is None or generation is None:
                    raise HTTPException(
                        status_code=422,
                        detail="slot and generation must be supplied together.",
                    )
                return application.state.tenant_service_pool.activate_candidate(
                    profile.company_id,
                    slot=slot,
                    generation=generation,
                )
            return application.state.tenant_service_pool.reload_generation(
                profile.company_id
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

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
        service = context.resolve_admin_service(
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
            "index_generation": getattr(service, "index_generation", None),
            "index_generation_slot": getattr(
                service,
                "index_generation_slot",
                None,
            ),
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

    @application.post(
        "/api/v1/{company_endpoint}/admin/rollback-index",
        tags=["company-admin"],
    )
    def company_admin_rollback_index(
        company_endpoint: str,
        x_admin_key: str | None = Header(
            default=None,
            alias="X-Admin-Key",
        ),
    ) -> dict[str, Any]:
        context.require_admin_key(x_admin_key)
        profile = application.state.tenant_registry.resolve_endpoint(company_endpoint)
        if profile is None:
            raise HTTPException(status_code=404, detail="Unknown company endpoint.")
        try:
            return application.state.tenant_service_pool.rollback_generation(
                profile.company_id
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

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
        service = context.resolve_admin_service(
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
