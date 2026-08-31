from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

SearchPolicyFactory = Callable[[], Any]
CompatibilityAdapterFactory = Callable[[Any, Any, Any], Any]
AnalyticsAdapterFactory = Callable[[Any], Any]
AnalyticsContractFactory = Callable[[], Any]
SearchPayloadFactory = Callable[[Any, str, int, int | None], dict[str, Any]]


def generic_search_payload(
    profile: Any,
    query: str,
    page_size: int,
    _city_id: int | None,
) -> dict[str, Any]:
    mapping = profile.payload.request_mapping
    return {
        mapping["query"]: query,
        mapping["page_size"]: page_size,
    }


@dataclass(frozen=True, slots=True)
class SearchClientContract:
    """How tooling and the shared API expose a tenant's search endpoint."""

    route: str = "search"
    blocks_generic_search: bool = False
    requires_city_id: bool = False
    payload_factory: SearchPayloadFactory = generic_search_payload

    def build_payload(
        self,
        profile: Any,
        query: str,
        page_size: int,
        city_id: int | None = None,
    ) -> dict[str, Any]:
        if self.requires_city_id and (city_id is None or city_id <= 0):
            raise ValueError("A positive city ID is required by this tenant contract.")
        return self.payload_factory(profile, query, page_size, city_id)


@dataclass(frozen=True, slots=True)
class CompatibilityAdapterRegistration:
    factory: CompatibilityAdapterFactory
    client_contract: SearchClientContract


@dataclass(frozen=True, slots=True)
class AnalyticsAdapterRegistration:
    factory: AnalyticsAdapterFactory
    contract_factory: AnalyticsContractFactory


@dataclass(frozen=True, slots=True)
class TenantPlugin:
    """One tenant/vertical bundle registered with the company-neutral core."""

    name: str
    default_search_policy: str = "default"
    default_planner_adapter: str = "default"
    search_policies: Mapping[str, SearchPolicyFactory] = field(default_factory=dict)
    compatibility_adapters: Mapping[
        str,
        CompatibilityAdapterRegistration,
    ] = field(default_factory=dict)
    analytics_adapters: Mapping[str, AnalyticsAdapterRegistration] = field(
        default_factory=dict
    )
    logger_names: tuple[str, ...] = ()
