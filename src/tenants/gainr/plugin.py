from __future__ import annotations

from typing import Any

from tenants.plugin import (
    AnalyticsAdapterRegistration,
    CompatibilityAdapterRegistration,
    SearchClientContract,
    TenantPlugin,
)


def _build_gainr_policy():
    from tenants.gainr.policy import GainrSearchPolicy

    return GainrSearchPolicy()


def _build_gainr_compatibility(profile, product_search_service, shared_cache=None):
    from tenants.gainr.compatibility import GainrCompatibilityService

    return GainrCompatibilityService(
        profile,
        product_search_service,
        shared_cache,
    )


def _gainr_search_payload(
    _profile: Any,
    query: str,
    _page_size: int,
    city_id: int | None,
) -> dict[str, Any]:
    return {
        "searchTerm": query,
        "filter": {"city_id": city_id},
        "page": 1,
    }


def _build_gainr_analytics(company):
    from tenants.gainr.analytics import (
        GAINR_ANALYTICS_CONTRACT,
        GainrAnalyticsAdapter,
    )

    return GainrAnalyticsAdapter(
        company_id=company.company_id,
        plugin_name="gainr",
        analytics_contract=GAINR_ANALYTICS_CONTRACT,
    )


def _gainr_analytics_contract():
    from tenants.gainr.analytics import GAINR_ANALYTICS_CONTRACT

    return GAINR_ANALYTICS_CONTRACT


PLUGIN = TenantPlugin(
    name="gainr",
    default_search_policy="gainr",
    default_planner_adapter="gainr",
    search_policies={"gainr": _build_gainr_policy},
    compatibility_adapters={
        "gainr_legacy": CompatibilityAdapterRegistration(
            factory=_build_gainr_compatibility,
            client_contract=SearchClientContract(
                route="filter-result",
                blocks_generic_search=True,
                requires_city_id=True,
                payload_factory=_gainr_search_payload,
            ),
        )
    },
    analytics_adapters={
        "gainr": AnalyticsAdapterRegistration(
            factory=_build_gainr_analytics,
            contract_factory=_gainr_analytics_contract,
        )
    },
    logger_names=("gainr_compat",),
)
