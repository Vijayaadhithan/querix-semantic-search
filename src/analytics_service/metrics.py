"""Curated production metric catalogue for analytics dashboard audiences."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# These are deliberately small, stable API contracts. The domain modules still
# contain the original exploratory reports so that a metric can be promoted
# after validation without recovering old code, but only this catalogue is
# published or persisted in production snapshots.
COMPANY_SEARCH_METRICS = (
    "q1_category_distribution",
    "q2_product_vs_service",
    "q3_trending_terms",
    "q4_brand_mentions",
    "q5_location_mentions",
    "q6_rental_duration",
    "q7_zero_results",
    "q15_search_volume",
    "q85_zero_results_cities",
    "q86_unfulfilled_brands",
    "q87_unfulfilled_duration",
    "q88_repeat_demand",
    "q89_category_fulfillment",
)

COMPANY_DEEP_METRICS = (
    "q47_supply_by_category",
    "q48_supply_by_city",
    "q50_low_supply_categories",
    "q51_user_growth",
    "q55_ads_per_user",
    "q56_verification",
    "q60_rental_fee_distribution",
    "q62_ad_status",
    "q65_premium_adoption",
    "q69_contact_views",
)

COMPANY_MARKET_METRICS = (
    "q75_marketplace_overview",
    "q76_geographic_heatmap",
    "q78_pricing_benchmark",
    "q79_temporal_patterns",
    "q81_user_acquisition_by_state",
    "q82_engagement",
    "q83_activation",
    "q84_seller_concentration",
)

INTERNAL_API_METRICS = (
    "q21_success_rate",
    "q22_execution_paths",
    "q23_latency_stats",
    "q24_latency_by_path",
    "q26_token_consumption",
    "q29_provider_reliability",
    "q30_latency_per_operation",
    "q31_model_latency",
    "q32_reranking_fallback",
    "q33_failure_reasons",
    "q35_result_distribution",
    "q40_avg_api_calls",
    "q41_multi_attempt",
    "q45_embedding_latency",
    "q46_query_planning_bottleneck",
)

DEFAULT_COMPANY_METRIC_PROFILE = {
    "search_intelligence": COMPANY_SEARCH_METRICS,
    "deep_analytics": COMPANY_DEEP_METRICS,
    "market_intelligence": COMPANY_MARKET_METRICS,
}

DEFAULT_INTERNAL_METRIC_PROFILE = {
    "api_performance": INTERNAL_API_METRICS,
}

# A tenant can select any implemented report in its YAML profile. Defaults stay
# deliberately curated; adding a non-default metric is an explicit product
# decision for that company rather than expanding every dashboard.
AVAILABLE_METRICS = {
    "search_intelligence": (
        "q1_category_distribution",
        "q2_product_vs_service",
        "q3_trending_terms",
        "q4_brand_mentions",
        "q5_location_mentions",
        "q6_rental_duration",
        "q7_zero_results",
        "q8_route_searches",
        "q9_professions",
        "q10_language",
        "q11_typos",
        "q12_query_length",
        "q13_gibberish",
        "q14_location_specificity",
        "q15_search_volume",
        "q16_new_subcategories",
        "q17_high_demand_cities",
        "q18_vehicle_searches",
        "q19_food_searches",
        "q20_b2b_demand",
        "q85_zero_results_cities",
        "q86_unfulfilled_brands",
        "q87_unfulfilled_duration",
        "q88_repeat_demand",
        "q89_category_fulfillment",
        "q90_complexity_performance",
        "q91_path_outcomes",
        "q92_provider_reliability",
    ),
    "api_performance": (
        "q21_success_rate",
        "q22_execution_paths",
        "q23_latency_stats",
        "q23_latency_histogram",
        "q24_latency_by_path",
        "q25_det_vs_sem",
        "q26_token_consumption",
        "q27_tokens_by_path",
        "q28_provider_usage",
        "q29_provider_reliability",
        "q30_latency_per_operation",
        "q31_model_latency",
        "q32_reranking_fallback",
        "q33_failure_reasons",
        "q34_estimated_cost",
        "q35_result_distribution",
        "q36_zero_result_rate",
        "q37_results_by_path",
        "q38_zero_result_queries",
        "q39_length_vs_results",
        "q40_avg_api_calls",
        "q41_multi_attempt",
        "q42_throughput",
        "q43_latency_over_time",
        "q44_voyage_rate_limit",
        "q45_embedding_latency",
        "q46_query_planning_bottleneck",
    ),
    "deep_analytics": (
        "q47_supply_by_category",
        "q48_supply_by_city",
        "q49_search_to_listing",
        "q50_low_supply_categories",
        "q51_user_growth",
        "q52_users_by_state",
        "q53_gender_split",
        "q54_platform",
        "q55_ads_per_user",
        "q56_verification",
        "q57_retention",
        "q58_products_vs_services",
        "q59_top_subcategories",
        "q60_rental_fee_distribution",
        "q61_negotiable",
        "q62_ad_status",
        "q63_top_ads",
        "q64_city_density",
        "q65_premium_adoption",
        "q66_revenue_by_city",
        "q67_undermonetized",
        "q68_premium_by_category",
        "q69_contact_views",
        "q70_photos",
        "q71_description_length",
        "q72_keywords",
        "q73_attribute_completeness",
        "q74_common_values",
    ),
    "market_intelligence": (
        "q75_marketplace_overview",
        "q76_geographic_heatmap",
        "q77_top_listings",
        "q78_pricing_benchmark",
        "q79_temporal_patterns",
        "q80_active_listings",
        "q81_user_acquisition_by_state",
        "q82_engagement",
        "q83_activation",
        "q84_seller_concentration",
    ),
}

COMPANY_MODULES = frozenset(DEFAULT_COMPANY_METRIC_PROFILE)
INTERNAL_MODULES = frozenset(DEFAULT_INTERNAL_METRIC_PROFILE)


def validate_metric_profile(
    raw_profile: Any,
    *,
    audience: str,
) -> dict[str, tuple[str, ...]]:
    """Validate a partial tenant profile and normalize its metric lists."""
    if raw_profile is None:
        return {}
    if not isinstance(raw_profile, Mapping):
        raise ValueError(
            f"Analytics {audience} metric profile must be an object"
        )
    allowed_modules = (
        COMPANY_MODULES if audience == "company" else INTERNAL_MODULES
    )
    normalized: dict[str, tuple[str, ...]] = {}
    for module, raw_names in raw_profile.items():
        module_name = str(module).strip()
        if module_name not in allowed_modules:
            raise ValueError(
                f"Analytics {audience} metric profile has unsupported "
                f"module {module_name!r}"
            )
        if isinstance(raw_names, (str, bytes)) or not isinstance(
            raw_names,
            (list, tuple),
        ):
            raise ValueError(
                f"Analytics metric module {module_name!r} must be a list"
            )
        names = tuple(str(name).strip() for name in raw_names)
        if any(not name for name in names):
            raise ValueError(
                f"Analytics metric module {module_name!r} has an empty name"
            )
        if len(names) != len(set(names)):
            raise ValueError(
                f"Analytics metric module {module_name!r} has duplicates"
            )
        unknown = [
            name
            for name in names
            if name not in AVAILABLE_METRICS[module_name]
        ]
        if unknown:
            raise ValueError(
                f"Analytics metric module {module_name!r} has unsupported "
                f"metrics: {', '.join(unknown)}"
            )
        normalized[module_name] = names
    return normalized


def resolve_metric_profiles(
    company_overrides: Mapping[str, tuple[str, ...]],
    internal_overrides: Mapping[str, tuple[str, ...]],
) -> tuple[
    dict[str, tuple[str, ...]],
    dict[str, tuple[str, ...]],
]:
    company_profile = {
        **DEFAULT_COMPANY_METRIC_PROFILE,
        **company_overrides,
    }
    internal_profile = {
        **DEFAULT_INTERNAL_METRIC_PROFILE,
        **internal_overrides,
    }
    return company_profile, internal_profile


def select_metrics(
    report: Mapping[str, Any],
    metric_names: tuple[str, ...],
) -> dict[str, Any]:
    """Return only approved metrics and fail if the report contract drifts."""
    missing = [name for name in metric_names if name not in report]
    if missing:
        raise KeyError(
            "Analytics report is missing curated metrics: "
            + ", ".join(missing)
        )
    return {name: report[name] for name in metric_names}


def metric_counts(
    profile: Mapping[str, tuple[str, ...]],
) -> dict[str, int]:
    return {module: len(names) for module, names in profile.items()}
