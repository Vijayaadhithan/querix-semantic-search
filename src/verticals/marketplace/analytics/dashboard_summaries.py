"""Calculate company and operator dashboard summaries."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from math import ceil
from statistics import mean
from typing import Any

from analytics_service.filters import (
    DashboardFilters,
)
from analytics_service.metrics import operation_group as _operation_group
from verticals.marketplace.analytics.dashboard_records import (
    _SUCCESS_STATUSES,
    _normalized_choice,
    _record_attempts,
    _record_categories,
    _record_request_kind,
)


def _counter_chart(title: str, counter: Counter) -> dict[str, Any]:
    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return {
        "title": title,
        "chart_type": "bar",
        "labels": [name for name, _ in ordered],
        "values": [int(value) for _, value in ordered],
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(ceil(percentile * len(ordered)) - 1, 0)
    return round(float(ordered[min(rank, len(ordered) - 1)]), 1)


def _company_overview(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    outcomes: Counter = Counter()
    browse_outcomes: Counter = Counter()
    request_kinds: Counter = Counter()
    categories: Counter = Counter()
    languages: Counter = Counter()
    all_requests = 0
    total = 0
    returned_results_sum = 0
    total_results_sum = 0
    for record in records:
        all_requests += 1
        request_kind = _record_request_kind(record)
        request_kinds[request_kind] += 1
        outcome = str(record.get("outcome") or "unknown")
        if request_kind != "text_search":
            browse_outcomes[outcome] += 1
            continue
        total += 1
        outcomes[outcome] += 1
        categories.update(_record_categories(record))
        languages[str(record.get("language") or "Unknown")] += 1
        search = dict(record.get("search") or {})
        returned_results_sum += int(search.get("result_count") or 0)
        total_results_sum += int(search.get("total_results") or 0)
    fulfilled = outcomes.get("fulfilled", 0)
    return {
        "summary": {
            "all_requests": all_requests,
            "searches": total,
            "browse_requests": all_requests - total,
            "fulfilled": int(fulfilled),
            "zero_results": int(outcomes.get("zero_result", 0)),
            "failures": int(outcomes.get("failure", 0)),
            "browse_fulfilled": int(browse_outcomes.get("fulfilled", 0)),
            "browse_zero_results": int(browse_outcomes.get("zero_result", 0)),
            "browse_failures": int(browse_outcomes.get("failure", 0)),
            "fulfillment_rate": round(fulfilled / total * 100, 1) if total else 0,
            "average_returned_results": round(returned_results_sum / total, 1)
            if total
            else 0,
            "average_total_results": round(total_results_sum / total, 1)
            if total
            else 0,
        },
        "breakdowns": {
            "outcomes": _counter_chart("Text-Search Outcomes", outcomes),
            "request_kinds": _counter_chart("Request Types", request_kinds),
            "browse_outcomes": _counter_chart(
                "Catalogue/Filter Browse Outcomes",
                browse_outcomes,
            ),
            "categories": _counter_chart("Searches by Category", categories),
            "languages": _counter_chart("Search Language", languages),
        },
    }


def _internal_overview(
    records: Iterable[dict[str, Any]], filters: DashboardFilters
) -> dict[str, Any]:
    paths: Counter = Counter()
    providers: Counter = Counter()
    operations: Counter = Counter()
    statuses: Counter = Counter()
    durations: list[float] = []
    stage_values: dict[str, list[float]] = defaultdict(list)
    token_groups: dict[str, Counter] = defaultdict(Counter)
    plan_cache: Counter = Counter()
    result_cache: Counter = Counter()
    downstream_calls = 0
    diagnostics: Counter = Counter()
    outcomes: Counter = Counter()
    total = 0

    provider_filter = _normalized_choice(filters.provider)
    operation_filter = _normalized_choice(filters.operation)
    for record in records:
        total += 1
        performance = dict(record.get("performance") or {})
        api = dict(record.get("api") or {})
        path = str(performance.get("execution_path") or "unknown")
        paths[path] += 1
        status = str(api.get("status") or "unknown")
        statuses[status] += 1
        diagnostic_code = str(
            dict(record.get("diagnostics") or {}).get("code") or "not_recorded"
        )
        diagnostics[diagnostic_code] += 1
        outcomes[str(record.get("outcome") or "telemetry_missing")] += 1
        duration = performance.get("total_server_duration_ms")
        if isinstance(duration, (int, float)):
            durations.append(float(duration))
        downstream_calls += int(performance.get("downstream_api_calls") or 0)
        cache = dict(performance.get("cache") or {})
        if cache.get("plan_hit") is not None:
            plan_cache[bool(cache["plan_hit"])] += 1
        if cache.get("result_hit") is not None:
            result_cache[bool(cache["result_hit"])] += 1
        for name, value in dict(performance.get("stages_ms") or {}).items():
            if isinstance(value, (int, float)):
                stage_values[str(name)].append(float(value))

        for attempt in _record_attempts(record):
            provider = str(attempt.get("provider") or "unknown")
            operation = str(attempt.get("operation") or "unknown")
            if provider_filter and provider.casefold() != provider_filter.casefold():
                continue
            if operation_filter and operation.casefold() != operation_filter.casefold():
                continue
            providers[provider] += 1
            operations[operation] += 1
            group = token_groups[_operation_group(operation)]
            group["attempts"] += 1
            group["api_calls"] += max(int(attempt.get("api_calls") or 0), 0)
            nonzero_tokens = False
            for name in (
                "input_tokens",
                "output_tokens",
                "thought_tokens",
                "total_tokens",
            ):
                value = max(int(attempt.get(name) or 0), 0)
                group[name] += value
                nonzero_tokens = nonzero_tokens or value > 0
            if nonzero_tokens:
                group["attempts_with_reported_tokens"] += 1

    successful = sum(
        count
        for status, count in statuses.items()
        if status.casefold() in _SUCCESS_STATUSES
    )
    token_payload = {
        group_name: {
            key: int(values.get(key, 0))
            for key in (
                "attempts",
                "api_calls",
                "attempts_with_reported_tokens",
                "input_tokens",
                "output_tokens",
                "thought_tokens",
                "total_tokens",
            )
        }
        for group_name, values in sorted(token_groups.items())
    }
    for required in ("llm", "reranker"):
        token_payload.setdefault(
            required,
            {
                "attempts": 0,
                "api_calls": 0,
                "attempts_with_reported_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "thought_tokens": 0,
                "total_tokens": 0,
            },
        )
    stage_latency = {
        name: {
            "count": len(values),
            "avg_ms": round(mean(values), 1),
            "p95_ms": _percentile(values, 0.95),
        }
        for name, values in sorted(stage_values.items())
        if values
    }
    return {
        "summary": {
            "requests": total,
            "successful": int(successful),
            "failed": int(total - successful),
            "zero_results": int(outcomes["zero_result"]),
            "success_rate": round(successful / total * 100, 1) if total else 0,
            "average_latency_ms": round(mean(durations), 1) if durations else 0,
            "p50_latency_ms": _percentile(durations, 0.50),
            "p95_latency_ms": _percentile(durations, 0.95),
            "p99_latency_ms": _percentile(durations, 0.99),
            "downstream_api_calls": downstream_calls,
            "plan_cache_hit_rate": (
                round(plan_cache[True] / sum(plan_cache.values()) * 100, 1)
                if plan_cache
                else None
            ),
            "result_cache_hit_rate": (
                round(result_cache[True] / sum(result_cache.values()) * 100, 1)
                if result_cache
                else None
            ),
        },
        "token_usage_by_operation": {
            "title": "Reported Token Usage: LLM vs Reranker",
            "chart_type": "comparison_table",
            "note": (
                "Token totals are provider-reported; attempts with zero tokens "
                "may represent zero usage or unavailable provider telemetry."
            ),
            "data": token_payload,
        },
        "breakdowns": {
            "execution_paths": _counter_chart("Execution Paths", paths),
            "providers": _counter_chart("Provider Attempts", providers),
            "operations": _counter_chart("Operations", operations),
            "statuses": _counter_chart("Request Statuses", statuses),
            "diagnostic_reasons": _counter_chart(
                "Observed Result Diagnoses", diagnostics
            ),
        },
        "stage_latency": {
            "title": "Measured Stage Latency",
            "chart_type": "comparison_table",
            "timing_semantics": "stages_may_overlap_do_not_sum",
            "data": stage_latency,
        },
    }
