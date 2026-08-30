from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from analytics_service.adapters_base import PassthroughCompanyAnalyticsAdapter
from tenants.registry import tenant_plugins


class CompanyAnalyticsAdapter(Protocol):
    """Company-owned projection of the shared analytics snapshot contract."""

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


def _adapter_factories() -> dict[str, AnalyticsAdapterFactory]:
    return {
        name: factory
        for plugin in tenant_plugins().values()
        for name, factory in plugin.analytics_adapters.items()
    }


def supported_analytics_adapters() -> tuple[str, ...]:
    return tuple(sorted(_adapter_factories()))


def build_analytics_adapter(
    adapter_name: str,
    company: Any,
) -> CompanyAnalyticsAdapter:
    normalized = adapter_name.strip().casefold() or "default"
    factory = _adapter_factories().get(normalized)
    if factory is None:
        supported = ", ".join(supported_analytics_adapters())
        raise ValueError(
            f"Unsupported analytics adapter {adapter_name!r}; "
            f"supported adapters: {supported}"
        )
    return factory(company)
