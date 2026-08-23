"""Internal dependency readiness checks with bounded response caching."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import requests
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from core.settings import EMBED_MODEL, OLLAMA_BASE_URL


def readiness_response(application: FastAPI) -> JSONResponse:
    """Return a fresh readiness result without exposing component details."""
    with application.state.readiness_cache_lock:
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
            # Preserve these response fields for compatibility. Readiness
            # successes are never cached because routing must stop as soon as
            # a critical dependency fails.
            "cache_seconds": 0,
            "cached": False,
        }
        status_code = 200 if ready_now else 503
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
