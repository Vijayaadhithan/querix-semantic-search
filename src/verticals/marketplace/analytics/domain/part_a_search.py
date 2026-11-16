import logging

from .search_demand_metrics import category_demand_metrics, search_preferences_metrics
from .search_quality_metrics import query_quality_metrics, unfulfilled_demand_metrics
from .search_segment_metrics import segment_demand_metrics

LOGGER = logging.getLogger(__name__)


def process_part_a(data):
    LOGGER.info("Processing Part A: Search Demand Intelligence")
    sh = data["search_history"][["request_id", "query_text", "created_at"]].copy()
    # Empty search terms are catalogue/filter browsing requests. They remain
    # available to internal API-performance analytics, but must not inflate
    # textual demand, fulfillment, language, or zero-result metrics.
    sh = sh[sh["query_text"].fillna("").astype(str).str.strip().ne("")].copy()
    data = {**data, "search_history": sh}
    api = data["api_usage"][["request_id", "total_results", "result_count", "status"]]
    results = {}

    queries = sh["query_text"].tolist()
    total_q = len(queries)

    query_cats = category_demand_metrics(results, queries)
    zero_queries = search_preferences_metrics(results, api, queries, sh, total_q)
    query_quality_metrics(results, queries, query_cats, sh, total_q)
    segment_demand_metrics(results, queries, total_q)
    unfulfilled_demand_metrics(results, data, zero_queries)

    return results
