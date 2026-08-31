from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from analytics_service.adapters_base import PassthroughCompanyAnalyticsAdapter
from analytics_service.contracts import AnalyticsComputation, AnalyticsContract
from analytics_service.filters import DashboardFilters
from tenants.plugin import AnalyticsAdapterRegistration
from tenants.registry import tenant_plugins


class CompanyAnalyticsAdapter(Protocol):
    """Company-owned projection of the shared analytics snapshot contract."""

    analytics_contract: AnalyticsContract

    def build_computation(
        self,
        data: dict[str, Any],
        modules: frozenset[str],
    ) -> AnalyticsComputation: ...

    def metric_definitions(
        self,
        reports: dict[str, dict[str, Any]],
        profile: dict[str, tuple[str, ...]],
        *,
        audience: str,
        source_rows: dict[str, int],
    ) -> dict[str, dict[str, Any]]: ...

    def dashboard_overview(
        self,
        records: list[dict[str, Any]],
        *,
        internal: bool,
        filters: DashboardFilters,
        timezone_name: str,
        now: Any = None,
    ) -> dict[str, Any]: ...

    def dashboard_response(
        self,
        dashboard: dict[str, Any],
    ) -> dict[str, Any]: ...

    def queries_response(
        self,
        queries: dict[str, Any],
    ) -> dict[str, Any]: ...

    def facets_response(
        self,
        facets: dict[str, Any],
    ) -> dict[str, Any]: ...

    def status_response(
        self,
        status: dict[str, Any],
    ) -> dict[str, Any]: ...


DefaultCompanyAnalyticsAdapter = PassthroughCompanyAnalyticsAdapter


AnalyticsAdapterFactory = Callable[[Any], CompanyAnalyticsAdapter]


def _adapter_registrations() -> dict[str, AnalyticsAdapterRegistration]:
    return {
        name: registration
        for plugin in tenant_plugins().values()
        for name, registration in plugin.analytics_adapters.items()
    }


def supported_analytics_adapters() -> tuple[str, ...]:
    return tuple(sorted(_adapter_registrations()))


def analytics_adapter_contract(adapter_name: str) -> AnalyticsContract:
    normalized = adapter_name.strip().casefold() or "default"
    registration = _adapter_registrations().get(normalized)
    if registration is None:
        supported = ", ".join(supported_analytics_adapters())
        raise ValueError(
            f"Unsupported analytics adapter {adapter_name!r}; "
            f"supported adapters: {supported}"
        )
    return registration.contract_factory()


def build_analytics_adapter(
    adapter_name: str,
    company: Any,
) -> CompanyAnalyticsAdapter:
    normalized = adapter_name.strip().casefold() or "default"
    registration = _adapter_registrations().get(normalized)
    if registration is None:
        supported = ", ".join(supported_analytics_adapters())
        raise ValueError(
            f"Unsupported analytics adapter {adapter_name!r}; "
            f"supported adapters: {supported}"
        )
    return registration.factory(company)
