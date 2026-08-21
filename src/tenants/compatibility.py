from __future__ import annotations

from typing import Any, Protocol

from tenants.plugin import SearchClientContract
from tenants.registry import tenant_plugins


class CompatibilityAdapter(Protocol):
    """Tenant-owned compatibility API contract.

    Core API routes call this protocol for legacy/company-specific endpoints.
    New tenants can add a custom adapter without importing their payload models
    or response shape into the shared API package.
    """

    product_search_service: Any

    def parse_search_suggestions(self, payload: dict[str, Any]) -> Any: ...

    def search_suggestions(self, request: Any) -> dict[str, Any]: ...

    def parse_filter_data(self, payload: dict[str, Any]) -> Any: ...

    def filter_data(self, request: Any) -> dict[str, Any]: ...

    def parse_filter_result(self, payload: dict[str, Any]) -> Any: ...

    def filter_results(
        self,
        request: Any,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]: ...

    def recent_searches(self, user_id: str | None) -> dict[str, Any]: ...


def _adapter_registrations():
    return {
        name: registration
        for plugin in tenant_plugins().values()
        for name, registration in plugin.compatibility_adapters.items()
    }


def supported_compatibility_adapters() -> tuple[str, ...]:
    return tuple(sorted(_adapter_registrations()))


def search_client_contract(name: str = "") -> SearchClientContract:
    normalized = name.strip().casefold()
    if not normalized:
        return SearchClientContract()
    try:
        return _adapter_registrations()[normalized].client_contract
    except KeyError as exc:
        supported = ", ".join(supported_compatibility_adapters())
        raise ValueError(
            f"Unsupported compatibility adapter {name!r}; expected one of: {supported}"
        ) from exc


def build_compatibility_adapter(
    name: str,
    profile,
    product_search_service,
    shared_cache=None,
) -> CompatibilityAdapter:
    adapter_name = name.strip().casefold()
    try:
        registration = _adapter_registrations()[adapter_name]
    except KeyError as exc:
        supported = ", ".join(supported_compatibility_adapters())
        raise ValueError(
            f"Unsupported compatibility adapter {name!r}; expected one of: {supported}"
        ) from exc
    return registration.factory(profile, product_search_service, shared_cache)
