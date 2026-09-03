from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from analytics_service.contracts import AnalyticsComputation, AnalyticsContract
from analytics_service.filters import DashboardFilters

EMPTY_ANALYTICS_CONTRACT = AnalyticsContract(
    dataset_specs={},
    default_tables={},
    available_metrics={},
    company_modules=frozenset(),
    internal_modules=frozenset(),
)


@dataclass(frozen=True, slots=True)
class PassthroughCompanyAnalyticsAdapter:
    """Stable analytics contract with a tenant-owned extension point."""

    company_id: str
    plugin_name: str = "default"
    analytics_contract: AnalyticsContract = EMPTY_ANALYTICS_CONTRACT

    def build_computation(
        self,
        data: dict[str, Any],
        modules: frozenset[str],
    ) -> AnalyticsComputation:
        del data
        if modules:
            raise ValueError(
                f"Analytics adapter {self.plugin_name!r} has no metric modules"
            )
        return AnalyticsComputation(
            reports={},
            query_pairs=(),
            query_record_count=0,
            company_overview={},
        )

    def metric_definitions(
        self,
        reports: dict[str, dict[str, Any]],
        profile: dict[str, tuple[str, ...]],
        *,
        audience: str,
        source_rows: dict[str, int],
    ) -> dict[str, dict[str, Any]]:
        del reports, profile, audience, source_rows
        return {}

    def dashboard_overview(
        self,
        records: list[dict[str, Any]],
        *,
        internal: bool,
        filters: DashboardFilters,
        timezone_name: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        del records, internal, filters, timezone_name, now
        return {
            "filtering": {"applied": {}, "available": {}, "matched_records": 0},
            "filtered_overview": {},
        }

    def dashboard_response(self, dashboard: dict[str, Any]) -> dict[str, Any]:
        return dashboard

    def queries_response(self, queries: dict[str, Any]) -> dict[str, Any]:
        return queries

    def facets_response(self, facets: dict[str, Any]) -> dict[str, Any]:
        return facets

    def status_response(self, status: dict[str, Any]) -> dict[str, Any]:
        return status
