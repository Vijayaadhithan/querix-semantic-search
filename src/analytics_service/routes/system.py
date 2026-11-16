"""System analytics HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Response

from ..auth import (
    AnalyticsAuthStore,
)
from ..config import AnalyticsRegistry
from ..schedule import REFRESH_SCHEDULE
from ..store import AnalyticsSnapshotStore


def register_system_routes(
    application: FastAPI,
    registry: AnalyticsRegistry,
    store: AnalyticsSnapshotStore,
    auth_store: AnalyticsAuthStore,
) -> None:
    @application.get("/api/v1/live", tags=["system"])
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/v1/ready", tags=["system"])
    def ready(response: Response) -> dict[str, Any]:
        try:
            statuses = [
                store.company_status(company_id) for company_id in registry.companies
            ]
            store_ready = store.readiness()
        except Exception as exc:
            statuses = []
            store_ready = {"ok": False, "error_type": type(exc).__name__}
        try:
            auth_ready = auth_store.readiness()
        except Exception as exc:
            auth_ready = {"ok": False, "error_type": type(exc).__name__}
        ready_now = (
            bool(statuses)
            and all(status["has_snapshot"] for status in statuses)
            and bool(store_ready.get("ok"))
            and bool(auth_ready.get("ok"))
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
