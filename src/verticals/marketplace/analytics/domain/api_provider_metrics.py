"""Api provider metrics."""

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass

from analytics_service.metrics import operation_group as _operation_group

from .report_utils import _nonnegative_int

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderAttemptSummary:
    any_provider_fallback_requests: int
    failure_reasons: Counter
    model_stats: dict
    operation_stats: dict
    operation_tokens: dict
    provider_stats: dict
    query_planner_fallback_requests: int
    reranker_fallback_requests: int
    total_reranking_requests: int
    voyage_to_nemotron_rescue_requests: int


def provider_attempt_metrics(parsed_attempts) -> ProviderAttemptSummary:
    # Q28-33: Provider-level analytics (from attempts_json)
    provider_stats = defaultdict(
        lambda: {
            "count": 0,
            "success": 0,
            "failure": 0,
            "fallback": 0,
            "total_latency": 0,
            "total_tokens": 0,
        }
    )
    operation_stats = defaultdict(lambda: {"count": 0, "total_latency": 0})
    operation_tokens = defaultdict(Counter)
    model_stats = defaultdict(
        lambda: {"count": 0, "total_latency": 0, "success": 0, "failure": 0}
    )
    failure_reasons = Counter()
    total_attempts_count = 0
    any_provider_fallback_requests = 0
    query_planner_fallback_requests = 0
    reranker_fallback_requests = 0
    total_reranking_requests = 0
    voyage_to_nemotron_rescue_requests = 0

    for attempts in parsed_attempts():
        fallback_groups = set()
        has_reranking_attempt = False
        voyage_fell_back = False
        nemotron_rescued = False
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            total_attempts_count += 1
            provider = attempt.get("provider", "unknown")
            operation = attempt.get("operation", "unknown")
            model = attempt.get("model", "unknown")
            status = attempt.get("status", "unknown")
            operation_group = _operation_group(operation)
            latency = attempt.get("duration_ms", 0)
            tokens = attempt.get("total_tokens", 0)
            reason = attempt.get("failure_reason", "")

            provider_stats[provider]["count"] += 1
            provider_stats[provider]["total_latency"] += latency
            provider_stats[provider]["total_tokens"] += tokens
            if status == "success":
                provider_stats[provider]["success"] += 1
            elif status == "fallback":
                provider_stats[provider]["fallback"] += 1
                fallback_groups.add(operation_group)
            else:
                provider_stats[provider]["failure"] += 1

            if operation_group == "reranker":
                has_reranking_attempt = True
                if provider == "voyage-2.5" and status == "fallback":
                    voyage_fell_back = True
                elif (
                    provider == "openrouter-nemotron"
                    and status == "success"
                    and voyage_fell_back
                ):
                    nemotron_rescued = True

            operation_stats[operation]["count"] += 1
            operation_stats[operation]["total_latency"] += latency
            token_group = operation_tokens[_operation_group(operation)]
            token_group["attempts"] += 1
            token_group["api_calls"] += _nonnegative_int(attempt.get("api_calls"))
            reported = False
            for token_name in (
                "input_tokens",
                "output_tokens",
                "thought_tokens",
                "total_tokens",
            ):
                token_value = _nonnegative_int(attempt.get(token_name))
                token_group[token_name] += token_value
                reported = reported or token_value > 0
            if reported:
                token_group["attempts_with_reported_tokens"] += 1

            model_key = f"{provider} / {model}"
            model_stats[model_key]["count"] += 1
            model_stats[model_key]["total_latency"] += latency
            if status == "success":
                model_stats[model_key]["success"] += 1
            else:
                model_stats[model_key]["failure"] += 1

            if reason:
                failure_reasons[reason] += 1

        if fallback_groups:
            any_provider_fallback_requests += 1
        if "llm" in fallback_groups:
            query_planner_fallback_requests += 1
        if "reranker" in fallback_groups:
            reranker_fallback_requests += 1
        if has_reranking_attempt:
            total_reranking_requests += 1
        if nemotron_rescued:
            voyage_to_nemotron_rescue_requests += 1
    return ProviderAttemptSummary(
        any_provider_fallback_requests=any_provider_fallback_requests,
        failure_reasons=failure_reasons,
        model_stats=model_stats,
        operation_stats=operation_stats,
        operation_tokens=operation_tokens,
        provider_stats=provider_stats,
        query_planner_fallback_requests=query_planner_fallback_requests,
        reranker_fallback_requests=reranker_fallback_requests,
        total_reranking_requests=total_reranking_requests,
        voyage_to_nemotron_rescue_requests=voyage_to_nemotron_rescue_requests,
    )


def provider_outcome_metrics(results: dict, summary: ProviderAttemptSummary) -> None:
    provider_usage = {k: v["count"] for k, v in summary.provider_stats.items()}
    results["q28_provider_usage"] = {
        "labels": list(provider_usage.keys()),
        "values": list(provider_usage.values()),
        "title": "Provider Usage Distribution",
        "chart_type": "doughnut",
    }

    # Q29: Provider success/failure/fallback
    provider_reliability = {}
    for p, s in summary.provider_stats.items():
        provider_reliability[p] = {
            "success": s["success"],
            "failure": s["failure"],
            "fallback": s["fallback"],
            "success_rate": round(s["success"] / s["count"] * 100, 1)
            if s["count"] > 0
            else 0,
            "avg_latency": round(s["total_latency"] / s["count"], 1)
            if s["count"] > 0
            else 0,
        }
    results["q29_provider_reliability"] = {
        "data": provider_reliability,
        "labels": list(provider_reliability.keys()),
        "success_values": [v["success"] for v in provider_reliability.values()],
        "failure_values": [v["failure"] for v in provider_reliability.values()],
        "fallback_values": [v["fallback"] for v in provider_reliability.values()],
        "fallback_requests": {
            "any_provider": summary.any_provider_fallback_requests,
            "query_planner": summary.query_planner_fallback_requests,
            "reranker": summary.reranker_fallback_requests,
        },
        "title": "Provider Reliability (Success/Failure/Fallback)",
        "chart_type": "stacked_bar",
    }

    # Q30: Latency per operation
    op_latency = {}
    for op, s in summary.operation_stats.items():
        op_latency[op] = {
            "count": s["count"],
            "avg_latency": round(s["total_latency"] / s["count"], 1)
            if s["count"] > 0
            else 0,
            "total_latency": round(s["total_latency"], 1),
        }
    results["q30_latency_per_operation"] = {
        "data": op_latency,
        "labels": list(op_latency.keys()),
        "values": [v["avg_latency"] for v in op_latency.values()],
        "title": "Average Latency per Operation (ms)",
        "chart_type": "bar",
    }


def provider_token_metrics(results: dict, summary: ProviderAttemptSummary) -> None:
    token_fields = (
        "attempts",
        "api_calls",
        "attempts_with_reported_tokens",
        "input_tokens",
        "output_tokens",
        "thought_tokens",
        "total_tokens",
    )
    operation_token_data = {
        group: {
            name: int(summary.operation_tokens[group].get(name, 0))
            for name in token_fields
        }
        for group in ("llm", "reranker", "embedding")
    }
    results["q93_operation_token_usage"] = {
        "data": operation_token_data,
        "labels": ["LLM", "Reranker", "Embedding"],
        "values": [
            operation_token_data[group]["total_tokens"]
            for group in ("llm", "reranker", "embedding")
        ],
        "title": "Reported Tokens and Calls by Operation",
        "chart_type": "comparison_table",
        "note": (
            "Tokens are provider-reported. Zero can mean no token usage or "
            "that the provider did not return token telemetry; attempts and "
            "API calls remain authoritative."
        ),
    }


def provider_model_metrics(results: dict, summary: ProviderAttemptSummary) -> None:
    model_latency = {}
    for m, s in summary.model_stats.items():
        model_latency[m] = {
            "count": s["count"],
            "avg_latency": round(s["total_latency"] / s["count"], 1)
            if s["count"] > 0
            else 0,
        }
    # Sort by count descending
    model_latency = dict(
        sorted(model_latency.items(), key=lambda x: x[1]["count"], reverse=True)
    )
    results["q31_model_latency"] = {
        "data": model_latency,
        "labels": list(model_latency.keys()),
        "values": [v["avg_latency"] for v in model_latency.values()],
        "counts": [v["count"] for v in model_latency.values()],
        "title": "Average Latency per Provider + Model (ms)",
        "chart_type": "bar",
    }

    # Q32: Reranking fallback frequency
    rerank_total = summary.operation_stats.get("reranking", {}).get("count", 0)
    voyage_fallbacks = summary.provider_stats.get("voyage-2.5", {}).get("fallback", 0)
    results["q32_reranking_fallback"] = {
        "total_reranking_calls": rerank_total,
        "total_reranking_requests": summary.total_reranking_requests,
        "voyage_fallbacks": voyage_fallbacks,
        "nemotron_rescues": summary.voyage_to_nemotron_rescue_requests,
        "fallback_rate": round(
            summary.reranker_fallback_requests / summary.total_reranking_requests * 100,
            1,
        )
        if summary.total_reranking_requests > 0
        else 0,
        # Backward-compatible field with corrected reranker-only semantics.
        "requests_with_fallback": summary.reranker_fallback_requests,
        "any_provider_fallback_requests": summary.any_provider_fallback_requests,
        "query_planner_fallback_requests": summary.query_planner_fallback_requests,
        "reranker_fallback_requests": summary.reranker_fallback_requests,
        "voyage_to_nemotron_rescue_requests": (
            summary.voyage_to_nemotron_rescue_requests
        ),
        "title": "Reranking Fallback Analysis",
        "chart_type": "stat",
    }

    # Q33: Failure reasons
    results["q33_failure_reasons"] = {
        "labels": [r for r in summary.failure_reasons if r],
        "values": [v for r, v in summary.failure_reasons.items() if r],
        "title": "Failure/Fallback Reasons",
        "chart_type": "bar",
    }
