"""Pure search usage, filter, and timing projections for durable telemetry."""

from collections.abc import Mapping
from typing import Any

from core.settings import EMBED_MODEL
from storage.search_analytics import SEARCH_ANALYTICS_TIMING_FIELDS, SearchApiUsageEvent

TIMING_SECONDS_FIELDS = {
    "planning_ms": "seconds",
    "vector_search_ms": "vector_seconds",
    "bm25_search_ms": "bm25_seconds",
    "retrieval_ms": "retrieval_seconds",
    "parallel_retrieval_ms": "parallel_retrieval_seconds",
    "fusion_ms": "fusion_seconds",
    "type_lookup_ms": "type_lookup_seconds",
    "reranker_load_ms": "reranker_load_seconds",
    "reranking_ms": "reranker_seconds",
    "related_tail_ms": "related_tail_seconds",
    "result_cache_ms": "result_cache_seconds",
}
EXPLICIT_TIMING_FIELDS = SEARCH_ANALYTICS_TIMING_FIELDS


def search_api_usage_events(
    result: dict[str, Any],
) -> tuple[SearchApiUsageEvent, ...]:
    events: list[SearchApiUsageEvent] = []
    query_metrics = result.get("query_model_metrics") or {}
    query_attempts = query_metrics.get("attempts") or (
        [query_metrics] if query_metrics.get("model") else []
    )
    for attempt in query_attempts:
        model = str(attempt.get("model") or "")
        if not model:
            continue
        reason = str(attempt.get("reason") or "")
        provider = str(attempt.get("provider") or "")
        if not provider:
            provider = "groq" if model.startswith("groq:") else "google"
        events.append(
            SearchApiUsageEvent(
                provider=provider,
                model=model,
                operation="query_planning",
                status=str(attempt.get("status") or "success"),
                api_calls=(
                    0 if reason in {"missing_api_key", "local_rate_limit"} else 1
                ),
                input_tokens=int(attempt.get("input_tokens", 0) or 0),
                output_tokens=int(attempt.get("output_tokens", 0) or 0),
                thought_tokens=int(attempt.get("thought_tokens", 0) or 0),
                total_tokens=int(attempt.get("total_tokens", 0) or 0),
                duration_ms=float(attempt.get("total_ms", 0.0) or 0.0),
                failure_reason=reason,
            )
        )

    embedding = result.get("embedding_model_metrics") or {}
    if embedding and not result.get("result_cache_hit"):
        events.append(
            SearchApiUsageEvent(
                provider="ollama",
                model=EMBED_MODEL,
                operation="embedding",
                status="success",
                api_calls=1,
                duration_ms=float(embedding.get("total_ms", 0.0) or 0.0),
            )
        )

    for attempt in result.get("reranker_attempts") or []:
        reason = str(attempt.get("reason") or "")
        local_rejection = (
            "request budget exhausted" in reason or "provider cooldown active" in reason
        )
        usage = attempt.get("usage") or {}
        events.append(
            SearchApiUsageEvent(
                provider=str(attempt.get("provider") or "unknown"),
                model=str(attempt.get("model") or ""),
                operation="reranking",
                status=str(attempt.get("status") or "success"),
                api_calls=0 if local_rejection else 1,
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                thought_tokens=int(usage.get("thought_tokens", 0) or 0),
                total_tokens=int(usage.get("total_tokens", 0) or 0),
                duration_ms=float(attempt.get("duration_ms", 0.0) or 0.0),
                failure_reason=reason,
            )
        )
    return tuple(events)


def analytics_filter_context(result: dict[str, Any]) -> dict[str, Any]:
    """Project only resolved, non-sensitive request filters to analytics."""
    resolved = dict(result.get("resolved_filters") or {})
    categorical = dict(resolved.get("categorical") or {})
    context = {}
    for source, target in (
        ("main_category_name", "main_category"),
        ("main_category_id", "main_category_id"),
        ("subcategory_id", "subcategory_id"),
        ("subcategory_name", "subcategory"),
        ("state_name", "state"),
        ("city_id", "city_id"),
        ("city_name", "city"),
        ("locality_id", "locality_id"),
        ("locality_name", "locality"),
        ("rental_duration", "rental_duration"),
    ):
        value = categorical.get(source)
        if isinstance(value, (str, int, float, bool)) and value != "":
            context[target] = value
    for name in ("min_rental_fee", "max_rental_fee"):
        value = resolved.get(name)
        if isinstance(value, (int, float)):
            context[name] = value
    target_ad_type = result.get("_analytics_target_ad_type") or dict(
        result.get("query_plan") or {}
    ).get("target_ad_type")
    if isinstance(target_ad_type, (str, int, float, bool)) and (target_ad_type != ""):
        context["target_ad_type"] = target_ad_type
    query_plan = dict(result.get("query_plan") or {})
    route_reason = str(query_plan.get("route_reason") or "").strip()
    if route_reason:
        context["route_reason"] = route_reason
    for name, value in dict(result.get("_analytics_candidate_counts") or {}).items():
        if name in {
            "retrieved_candidates",
            "eligible_candidates",
            "hydrated_results",
            "returned_results",
        } and isinstance(value, (int, float)):
            context[name] = max(int(value), 0)
    for target, source in (
        ("explicit_filters", "_analytics_explicit_filters"),
        ("inferred_filters", "_analytics_inferred_filters"),
    ):
        values = result.get(source)
        if isinstance(values, dict) and values:
            context[target] = values
    ignored = result.get("_analytics_ignored_filter_names")
    if isinstance(ignored, (list, tuple)) and ignored:
        context["ignored_filter_names"] = list(ignored)
    filter_diagnostics = result.get("_analytics_filter_diagnostics")
    if isinstance(filter_diagnostics, dict):
        counts = {
            str(name): max(int(value), 0)
            for name, value in dict(
                filter_diagnostics.get("counterfactual_counts") or {}
            ).items()
            if isinstance(value, (int, float))
        }
        context["filter_diagnostics"] = {
            "evidence_complete": bool(filter_diagnostics.get("evidence_complete")),
            "diagnosis": str(filter_diagnostics.get("diagnosis") or ""),
            "counterfactual_counts": counts,
            "blocking_filters": [
                str(value)
                for value in filter_diagnostics.get("blocking_filters") or ()
                if isinstance(value, (str, int, float))
            ],
        }
    return context


def search_timings_ms(
    result: dict[str, Any],
    total_server_ms: float,
    *,
    seconds_fields: Mapping[str, str] = TIMING_SECONDS_FIELDS,
    explicit_fields: frozenset[str] = EXPLICIT_TIMING_FIELDS,
) -> dict[str, float]:
    """Return safe measured stages; stages may overlap and are not totals."""
    timings: dict[str, float] = {}

    def add(name: str, value: Any, multiplier: float = 1.0) -> None:
        try:
            measured = float(value) * multiplier
        except (TypeError, ValueError):
            return
        if measured >= 0:
            timings[name] = round(measured, 3)

    for canonical, source in seconds_fields.items():
        if source in result:
            add(canonical, result[source], 1000.0)

    query_metrics = result.get("query_model_metrics") or {}
    embedding_metrics = result.get("embedding_model_metrics") or {}
    for canonical, metrics, source in (
        ("query_model_ms", query_metrics, "total_ms"),
        ("query_model_load_ms", query_metrics, "load_ms"),
        ("embedding_ms", embedding_metrics, "total_ms"),
        ("embedding_load_ms", embedding_metrics, "load_ms"),
    ):
        if source in metrics:
            add(canonical, metrics[source])
    if "query_model_ms" not in timings:
        query_attempts = query_metrics.get("attempts") or []
        attempt_total_ms = sum(
            max(float(attempt.get("total_ms", 0.0) or 0.0), 0.0)
            for attempt in query_attempts
        )
        if query_attempts:
            add("query_model_ms", attempt_total_ms)

    for name, value in (result.get("_analytics_timings_ms") or {}).items():
        if name in explicit_fields:
            add(name, value)

    # This is the only authoritative whole-request duration. It always
    # wins over caller-supplied stage data and must not be reconstructed
    # by summing overlapping stages.
    add("total_server_ms", total_server_ms)
    return timings
