"""Request contract for tenant-owned dashboard activity filtering."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

PERIOD_OPTIONS = ("24h", "7d", "30d", "90d", "all", "custom")
REQUEST_SCOPE_OPTIONS = ("all", "text_search", "browse")


@dataclass(frozen=True, slots=True)
class DashboardFilters:
    period: str = "all"
    request_scope: str = "all"
    created_from: datetime | None = None
    created_to: datetime | None = None
    outcome: str | None = None
    category: str | None = None
    language: str | None = None
    city: str | None = None
    city_id: int | None = None
    ad_type: str | None = None
    execution_path: str | None = None
    provider: str | None = None
    operation: str | None = None
