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


def metric_counts() -> dict[str, int]:
    return {
        "search_intelligence": len(COMPANY_SEARCH_METRICS),
        "deep_analytics": len(COMPANY_DEEP_METRICS),
        "market_intelligence": len(COMPANY_MARKET_METRICS),
        "api_performance": len(INTERNAL_API_METRICS),
    }
