from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


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


AdapterFactory = Callable[[Any, Any, Any], CompatibilityAdapter]


def _build_gainr_legacy(profile, product_search_service, shared_cache=None):
    from tenants.gainr.compatibility import GainrCompatibilityService

    return GainrCompatibilityService(
        profile,
        product_search_service,
        shared_cache,
    )


_ADAPTER_FACTORIES: dict[str, AdapterFactory] = {
    "gainr_legacy": _build_gainr_legacy,
}


def supported_compatibility_adapters() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTER_FACTORIES))


def build_compatibility_adapter(
    name: str,
    profile,
    product_search_service,
    shared_cache=None,
) -> CompatibilityAdapter:
    adapter_name = name.strip().casefold()
    try:
        factory = _ADAPTER_FACTORIES[adapter_name]
    except KeyError as exc:
        supported = ", ".join(supported_compatibility_adapters())
        raise ValueError(
            f"Unsupported compatibility adapter {name!r}; expected one of: {supported}"
        ) from exc
    return factory(profile, product_search_service, shared_cache)
