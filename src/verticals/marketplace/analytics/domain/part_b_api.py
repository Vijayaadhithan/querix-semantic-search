import logging

from .api_bottleneck_metrics import provider_bottleneck_metrics, request_volume_metrics
from .api_provider_metrics import (
    provider_attempt_metrics,
    provider_model_metrics,
    provider_outcome_metrics,
    provider_token_metrics,
)
from .api_request_metrics import (
    latency_metrics,
    request_outcome_metrics,
    result_distribution_metrics,
    token_consumption_metrics,
)
from .utils import parse_attempts_json

LOGGER = logging.getLogger(__name__)


def process_part_b(data):
    LOGGER.info("Processing Part B: API Performance Analytics")
    api = data["api_usage"].copy()
    sh = data["search_history"].copy()
    text_searches = sh[
        sh["query_text"].fillna("").astype(str).str.strip().ne("")
    ].copy()
    text_request_ids = set(text_searches["request_id"].dropna().astype(str))
    text_api = api[api["request_id"].astype(str).isin(text_request_ids)].copy()
    successful_text_api = text_api[
        text_api["status"]
        .fillna("")
        .astype(str)
        .str.casefold()
        .isin({"success", "successful", "ok"})
    ].copy()
    results = {}

    total_requests = len(api)

    # Reparse compact JSON during the few attempt metrics that need it. Holding
    # one Python list/dict tree per request can exhaust the refresh worker.
    def parsed_attempts():
        for raw in api["attempts_json"]:
            yield parse_attempts_json(raw)

    request_outcome_metrics(results, api)
    latency_metrics(results, api)
    token_consumption_metrics(results, api)
    provider_summary = provider_attempt_metrics(parsed_attempts)
    provider_outcome_metrics(results, provider_summary)
    provider_token_metrics(results, provider_summary)
    provider_model_metrics(results, provider_summary)
    merged = result_distribution_metrics(
        results,
        api,
        provider_summary.provider_stats,
        successful_text_api,
        text_api,
        text_searches,
        total_requests,
    )
    request_volume_metrics(results, api, merged, parsed_attempts, total_requests)
    provider_bottleneck_metrics(results, api, parsed_attempts)

    return results
