from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


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

    def status_response(
        self,
        status: dict[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class DefaultCompanyAnalyticsAdapter:
    """Preserve the canonical analytics API response without modification."""

    company_id: str

    def dashboard_response(
        self,
        dashboard: dict[str, Any],
    ) -> dict[str, Any]:
        return dashboard

    def queries_response(
        self,
        queries: dict[str, Any],
    ) -> dict[str, Any]:
        return queries

    def status_response(
        self,
        status: dict[str, Any],
    ) -> dict[str, Any]:
        return status


AnalyticsAdapterFactory = Callable[[Any], CompanyAnalyticsAdapter]


def _build_default(company: Any) -> CompanyAnalyticsAdapter:
    return DefaultCompanyAnalyticsAdapter(company_id=company.company_id)


_ADAPTER_FACTORIES: dict[str, AnalyticsAdapterFactory] = {
    "default": _build_default,
}


def supported_analytics_adapters() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTER_FACTORIES))


def build_analytics_adapter(
    adapter_name: str,
    company: Any,
) -> CompanyAnalyticsAdapter:
    normalized = adapter_name.strip().casefold() or "default"
    factory = _ADAPTER_FACTORIES.get(normalized)
    if factory is None:
        supported = ", ".join(supported_analytics_adapters())
        raise ValueError(
            f"Unsupported analytics adapter {adapter_name!r}; "
            f"supported adapters: {supported}"
        )
    return factory(company)
