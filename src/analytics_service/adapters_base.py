from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PassthroughCompanyAnalyticsAdapter:
    """Stable analytics contract with a tenant-owned extension point."""

    company_id: str
    plugin_name: str = "default"

    def dashboard_response(self, dashboard: dict[str, Any]) -> dict[str, Any]:
        return dashboard

    def queries_response(self, queries: dict[str, Any]) -> dict[str, Any]:
        return queries

    def facets_response(self, facets: dict[str, Any]) -> dict[str, Any]:
        return facets

    def status_response(self, status: dict[str, Any]) -> dict[str, Any]:
        return status
