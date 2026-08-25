"""Human-readable analytics definitions for progressive dashboard disclosure."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_COMPANY_GROUPS = {
    "demand": {
        "q3_trending_terms",
        "q10_language",
        "q11_typos",
        "q12_query_length",
        "q15_search_volume",
        "q16_new_subcategories",
        "q88_repeat_demand",
        "q89_category_fulfillment",
        "q94_catalog_demand",
        "q95_unmet_demand_by_city_category",
        "q97_ad_type_demand",
        "q98_demand_classification_coverage",
    },
    "demand_supply": {
        "q49_search_to_listing",
        "q50_low_supply_categories",
        "q96_demand_supply_gap",
    },
    "supply": {
        "q47_supply_by_category",
        "q48_supply_by_city",
        "q58_products_vs_services",
        "q59_top_subcategories",
        "q62_ad_status",
        "q75_marketplace_overview",
        "q76_geographic_heatmap",
        "q77_top_listings",
        "q79_temporal_patterns",
        "q80_active_listings",
    },
    "customers_sellers": {
        "q51_user_growth",
        "q52_users_by_state",
        "q54_platform",
        "q55_ads_per_user",
        "q56_verification",
        "q57_retention",
        "q81_user_acquisition_by_state",
        "q83_activation",
        "q84_seller_concentration",
    },
    "pricing_growth": {
        "q60_rental_fee_distribution",
        "q61_negotiable",
        "q65_premium_adoption",
        "q68_premium_by_category",
        "q78_pricing_benchmark",
    },
    "quality_engagement": {
        "q63_top_ads",
        "q69_contact_views",
        "q70_photos",
        "q71_description_length",
        "q73_attribute_completeness",
        "q82_engagement",
    },
}

_GROUP_LABELS = {
    "demand": "Customer demand",
    "demand_supply": "Demand and supply opportunities",
    "supply": "Marketplace supply",
    "customers_sellers": "Customers and sellers",
    "pricing_growth": "Pricing and growth",
    "quality_engagement": "Listing quality and engagement",
    "reliability": "Reliability",
    "performance": "Performance",
    "providers": "Providers and AI usage",
}

_QUESTIONS = {
    "q47_supply_by_category": "Which marketplace categories have the most supply?",
    "q48_supply_by_city": "Which cities have the most listing supply?",
    "q51_user_growth": "How is Gainr's user base growing?",
    "q56_verification": "How many Gainr users are verified?",
    "q59_top_subcategories": "Which subcategories have the most listings?",
    "q60_rental_fee_distribution": "How are rental fees distributed?",
    "q62_ad_status": "What is the current listing-status mix?",
    "q65_premium_adoption": "How widely are premium and top listings used?",
    "q70_photos": "How complete is listing photo coverage?",
    "q73_attribute_completeness": "Which structured listing attributes are used most?",
    "q75_marketplace_overview": "How does the marketplace compare across categories?",
    "q78_pricing_benchmark": "How do typical prices compare across categories?",
    "q82_engagement": "Which listings receive the strongest engagement?",
    "q83_activation": "How quickly do users create their first listing?",
    "q84_seller_concentration": "How concentrated is supply among sellers?",
    "q89_category_fulfillment": (
        "Which categories have the most no-result text searches?"
    ),
    "q95_unmet_demand_by_city_category": (
        "Where do completed searches find no listings?"
    ),
    "q97_ad_type_demand": "Which listing type are customers searching for?",
    "q98_demand_classification_coverage": "How many searches match a known category?",
}

_SOURCES = {
    "search_intelligence": ("search_history", "api_usage"),
    "deep_analytics": ("ads", "users"),
    "market_intelligence": ("ads", "users"),
    "api_performance": ("search_history", "api_usage"),
}


def _group(metric_id: str, module: str, audience: str) -> str:
    if audience == "company":
        for group, names in _COMPANY_GROUPS.items():
            if metric_id in names:
                return group
        return "demand" if module == "search_intelligence" else "supply"
    if metric_id in {
        "q21_success_rate",
        "q33_failure_reasons",
        "q35_result_distribution",
    }:
        return "reliability"
    if metric_id in {
        "q26_token_consumption",
        "q29_provider_reliability",
        "q30_latency_per_operation",
        "q31_model_latency",
        "q41_multi_attempt",
        "q93_operation_token_usage",
    }:
        return "providers"
    return "performance"


def build_metric_definitions(
    reports: Mapping[str, Mapping[str, Mapping[str, Any]]],
    profile: Mapping[str, tuple[str, ...]],
    *,
    audience: str,
    source_rows: Mapping[str, int],
) -> dict[str, dict[str, Any]]:
    definitions = {}
    for module, metric_names in profile.items():
        sources = _SOURCES[module]
        available = all(int(source_rows.get(source, 0)) > 0 for source in sources)
        for metric_id in metric_names:
            payload = reports[module][metric_id]
            group = _group(metric_id, module, audience)
            title = str(payload.get("title") or metric_id)
            definitions[metric_id] = {
                "question": _QUESTIONS.get(metric_id, title),
                "description": str(payload.get("note") or ""),
                "group": group,
                "group_label": _GROUP_LABELS[group],
                "scope": "company_snapshot"
                if audience == "company"
                else "internal_telemetry",
                "sources": list(sources),
                "available": available,
                "unavailable_reason": None
                if available
                else "Required source data is unavailable.",
            }
    return definitions
