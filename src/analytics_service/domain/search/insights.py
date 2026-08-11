"""Additional aggregate questions derived from query-level outcomes."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

import pandas as pd

from ..utils import parse_attempts_json
from .classification import classify_search_query, normalize_query
from .records import merge_search_api


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _repeat_demand(merged: pd.DataFrame) -> dict[str, Any]:
    normalized = merged["query_text"].map(normalize_query)
    counts = Counter(query for query in normalized if query)
    repeated = [(query, count) for query, count in counts.most_common(20) if count > 1]
    return {
        "labels": [query for query, _ in repeated],
        "values": [count for _, count in repeated],
        "unique_queries": len(counts),
        "repeated_query_count": sum(1 for count in counts.values() if count > 1),
        "title": "Repeated Exact Search Demand",
        "chart_type": "bar",
    }


def _category_fulfillment(merged: pd.DataFrame) -> dict[str, Any]:
    buckets: dict[str, dict[str, list[float] | int]] = defaultdict(
        lambda: {"searches": 0, "zero": 0, "results": [], "latency": []}
    )
    for _, row in merged.iterrows():
        for category in classify_search_query(row["query_text"]):
            bucket = buckets[category]
            bucket["searches"] += 1
            results = _number(row.get("total_results"))
            if results == 0:
                bucket["zero"] += 1
            bucket["results"].append(results)
            bucket["latency"].append(_number(row.get("duration_ms")))

    rows = []
    for category, bucket in buckets.items():
        searches = int(bucket["searches"])
        zero = int(bucket["zero"])
        result_values = bucket["results"]
        latency_values = bucket["latency"]
        rows.append(
            {
                "category": category,
                "searches": searches,
                "zero_results": zero,
                "zero_result_rate": round(zero / searches * 100, 1) if searches else 0,
                "avg_results": round(sum(result_values) / len(result_values), 1),
                "avg_latency_ms": round(sum(latency_values) / len(latency_values), 1),
            }
        )
    rows.sort(
        key=lambda item: (item["zero_result_rate"], item["searches"]), reverse=True
    )
    return {
        "rows": rows,
        "labels": [row["category"] for row in rows],
        "values": [row["zero_result_rate"] for row in rows],
        "title": "Fulfillment Gaps by Search Category",
        "chart_type": "bar",
    }


def _complexity_performance(merged: pd.DataFrame) -> dict[str, Any]:
    labels = ("1 word", "2 words", "3 words", "4–5 words", "6+ words")
    aggregates: dict[str, dict[str, list[float] | int]] = {
        label: {"latency": [], "zero": 0, "count": 0} for label in labels
    }
    for _, row in merged.iterrows():
        words = len(normalize_query(row["query_text"]).split())
        label = (
            "1 word"
            if words <= 1
            else "2 words"
            if words == 2
            else "3 words"
            if words == 3
            else "4–5 words"
            if words <= 5
            else "6+ words"
        )
        aggregate = aggregates[label]
        aggregate["count"] += 1
        aggregate["latency"].append(_number(row.get("duration_ms")))
        aggregate["zero"] += int(_number(row.get("total_results")) == 0)

    avg_latency = []
    zero_rate = []
    counts = []
    for label in labels:
        aggregate = aggregates[label]
        count = int(aggregate["count"])
        latencies = aggregate["latency"]
        counts.append(count)
        avg_latency.append(
            round(sum(latencies) / len(latencies), 1) if latencies else 0
        )
        zero_rate.append(round(int(aggregate["zero"]) / count * 100, 1) if count else 0)
    return {
        "labels": list(labels),
        "avg_latency_values": avg_latency,
        "zero_rate_values": zero_rate,
        "count_values": counts,
        "title": "Query Complexity vs Performance",
        "chart_type": "grouped_bar",
    }


def _path_outcomes(merged: pd.DataFrame) -> dict[str, Any]:
    rows = []
    for path, frame in merged.groupby("execution_path", dropna=False):
        count = len(frame)
        zero = int((frame["total_results"].fillna(0) == 0).sum())
        failures = int(
            (
                ~frame["status"]
                .fillna("missing")
                .str.casefold()
                .isin(["success", "successful", "ok"])
            ).sum()
        )
        rows.append(
            {
                "execution_path": str(path),
                "requests": count,
                "zero_result_rate": round(zero / count * 100, 1) if count else 0,
                "failure_rate": round(failures / count * 100, 1) if count else 0,
                "avg_latency_ms": round(_number(frame["duration_ms"].mean()), 1),
                "avg_results": round(_number(frame["total_results"].mean()), 1),
            }
        )
    rows.sort(key=lambda item: item["requests"], reverse=True)
    return {
        "rows": rows,
        "labels": [row["execution_path"] for row in rows],
        "values": [row["zero_result_rate"] for row in rows],
        "title": "Execution Path Outcome Quality",
        "chart_type": "bar",
    }


def _provider_reliability(merged: pd.DataFrame) -> dict[str, Any]:
    providers: dict[str, Counter[str]] = defaultdict(Counter)
    for value in merged["attempts_json"].fillna("[]"):
        for attempt in parse_attempts_json(value):
            provider = str(attempt.get("provider") or "unknown")
            status = str(attempt.get("status") or "unknown").casefold()
            providers[provider][status] += 1
    rows = []
    for provider, counts in providers.items():
        total = sum(counts.values())
        success = counts["success"]
        rows.append(
            {
                "provider": provider,
                "attempts": total,
                "successes": success,
                "success_rate": round(success / total * 100, 1) if total else 0,
            }
        )
    rows.sort(key=lambda item: item["attempts"], reverse=True)
    return {
        "rows": rows,
        "labels": [row["provider"] for row in rows],
        "values": [row["success_rate"] for row in rows],
        "title": "Provider Reliability from Search Attempts",
        "chart_type": "bar",
    }


def build_additional_search_insights(
    data: dict[str, pd.DataFrame],
) -> dict[str, dict[str, Any]]:
    merged = merge_search_api(data)
    return {
        "q88_repeat_demand": _repeat_demand(merged),
        "q89_category_fulfillment": _category_fulfillment(merged),
        "q90_complexity_performance": _complexity_performance(merged),
        "q91_path_outcomes": _path_outcomes(merged),
        "q92_provider_reliability": _provider_reliability(merged),
    }
