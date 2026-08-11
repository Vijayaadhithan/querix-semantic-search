"""Internal dependency readiness checks with bounded response caching."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import requests
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from core.settings import (
    API_READINESS_CACHE_SECONDS,
    EMBED_MODEL,
    OLLAMA_BASE_URL,
)


def readiness_response(application: FastAPI) -> JSONResponse:
    """Return the private readiness result without exposing component details."""
    now = time.monotonic()
    with application.state.readiness_cache_lock:
        cached = application.state.readiness_cache
        if (
            cached is not None
            and API_READINESS_CACHE_SECONDS > 0
            and now - cached["created_monotonic"] < API_READINESS_CACHE_SECONDS
        ):
            return JSONResponse(
                {**cached["payload"], "cached": True},
                status_code=cached["status_code"],
            )

        tenant_mode = bool(application.state.tenant_mode)
        registry = getattr(application.state, "tenant_registry", None)
        checks: dict[str, Any] = {}
        if tenant_mode:
            pool = application.state.tenant_service_pool
            for company_id in registry.profiles:
                try:
                    checks[company_id] = pool.get(company_id).readiness()
                except Exception as exc:
                    checks[company_id] = {
                        "ok": False,
                        "components": {},
                        "error_type": type(exc).__name__,
                    }
        else:
            checks["legacy"] = application.state.search_service.readiness()

        ollama = _ollama_readiness(application)
        checks["ollama"] = ollama
        ready_now = all(check.get("ok", False) for check in checks.values())
        payload = {
            "status": "ok" if ready_now else "not_ready",
            "tenant_mode": tenant_mode,
            "configured_companies": (
                len(registry.profiles) if registry is not None else 1
            ),
            "checked_at_utc": datetime.now(UTC).isoformat(),
            "cache_seconds": API_READINESS_CACHE_SECONDS,
            "cached": False,
        }
        status_code = 200 if ready_now else 503
        if ready_now and API_READINESS_CACHE_SECONDS > 0:
            application.state.readiness_cache = {
                "created_monotonic": now,
                "status_code": status_code,
                "payload": payload,
            }
        return JSONResponse(payload, status_code=status_code)


def _ollama_readiness(application: FastAPI) -> dict[str, Any]:
    if not application.state.check_ollama_readiness:
        return {"ok": True, "checked": False}
    try:
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=2)
        response.raise_for_status()
        names = {model.get("name") for model in response.json().get("models", [])}
        return {
            "ok": EMBED_MODEL in names,
            "checked": True,
            "model": EMBED_MODEL,
        }
    except Exception as exc:
        return {
            "ok": False,
            "checked": True,
            "model": EMBED_MODEL,
            "error_type": type(exc).__name__,
        }
