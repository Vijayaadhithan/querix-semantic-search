"""Api bottleneck metrics."""

import logging
from collections import Counter

import pandas as pd

from .report_utils import _rounded_stat
from .utils import parse_attempts_json

LOGGER = logging.getLogger(__name__)


def request_volume_metrics(
    results, api, merged, parsed_attempts, total_requests
) -> None:
    # Q39: Query length vs result count correlation
    merged["query_length"] = (
        merged["query_text"].fillna("").apply(lambda x: len(x.split()))
    )
    length_results = merged.groupby("query_length")["total_results"].mean()
    results["q39_length_vs_results"] = {
        "labels": [
            str(int(query_length)) for query_length in length_results.index[:15]
        ],
        "values": [round(float(v), 1) for v in length_results.values[:15]],
        "title": "Query Length vs Average Results",
        "chart_type": "line",
    }

    # Q40: Average API calls per request
    results["q40_avg_api_calls"] = {
        "avg": _rounded_stat(api["api_call_count"].mean(), 2),
        "distribution": api["api_call_count"].value_counts().sort_index().to_dict(),
        "labels": [str(k) for k in sorted(api["api_call_count"].value_counts().index)],
        "values": [
            int(api["api_call_count"].value_counts().sort_index()[k])
            for k in sorted(api["api_call_count"].value_counts().index)
        ],
        "title": "API Calls per Request Distribution",
        "chart_type": "bar",
    }

    # Q41: Requests with >3 attempts
    multi_attempt = sum(1 for attempts in parsed_attempts() if len(attempts) > 3)
    results["q41_multi_attempt"] = {
        "count": multi_attempt,
        "percentage": (
            round(multi_attempt / total_requests * 100, 1) if total_requests else 0
        ),
        "title": "Requests with >3 Attempts (Fallbacks)",
        "chart_type": "stat",
    }

    # Q42: Search throughput over time
    api["created_at"] = pd.to_datetime(api["created_at"])
    api["minute_bucket"] = api["created_at"].dt.floor("5min")
    throughput = api.groupby("minute_bucket").size()
    results["q42_throughput"] = {
        "labels": [str(t.time()) for t in throughput.index],
        "values": throughput.tolist(),
        "title": "Search Throughput (requests per 5-min bucket)",
        "chart_type": "line",
    }

    # Q43: Latency spikes
    api["minute_bucket2"] = api["created_at"].dt.floor("5min")
    latency_over_time = api.groupby("minute_bucket2")["duration_ms"].mean()
    results["q43_latency_over_time"] = {
        "labels": [str(t.time()) for t in latency_over_time.index],
        "values": [round(float(v), 1) for v in latency_over_time.values],
        "title": "Average Latency Over Time (5-min buckets)",
        "chart_type": "line",
    }


def provider_bottleneck_metrics(results, api, parsed_attempts) -> None:
    # Q44: Voyage rate limiting
    voyage_events = []
    for attempts in parsed_attempts():
        for attempt in attempts:
            if isinstance(attempt, dict) and attempt.get("provider") == "voyage-2.5":
                voyage_events.append(
                    {
                        "status": attempt.get("status", ""),
                        "failure_reason": attempt.get("failure_reason", ""),
                        "duration_ms": attempt.get("duration_ms", 0),
                    }
                )
    voyage_ok = sum(1 for e in voyage_events if e["status"] == "success")
    voyage_fail = sum(1 for e in voyage_events if e["status"] != "success")
    results["q44_voyage_rate_limit"] = {
        "total_calls": len(voyage_events),
        "success": voyage_ok,
        "failures": voyage_fail,
        "failure_rate": round(voyage_fail / len(voyage_events) * 100, 1)
        if voyage_events
        else 0,
        "failure_reasons": dict(
            Counter(e["failure_reason"] for e in voyage_events if e["failure_reason"])
        ),
        "title": "Voyage-2.5 Rate Limiting Analysis",
        "chart_type": "stat",
    }

    # Q45: Embedding latency distribution
    embed_latencies = []
    for attempts in parsed_attempts():
        for attempt in attempts:
            if isinstance(attempt, dict) and attempt.get("operation") == "embedding":
                embed_latencies.append(attempt.get("duration_ms", 0))
    if embed_latencies:
        results["q45_embedding_latency"] = {
            "avg": round(sum(embed_latencies) / len(embed_latencies), 1),
            "min": round(min(embed_latencies), 1),
            "max": round(max(embed_latencies), 1),
            "median": round(sorted(embed_latencies)[len(embed_latencies) // 2], 1),
            "count": len(embed_latencies),
            "title": "Embedding Model (embeddinggemma) Latency",
            "chart_type": "stat",
        }
    else:
        results["q45_embedding_latency"] = {
            "title": "Embedding Latency",
            "chart_type": "stat",
            "avg": 0,
        }

    # Q46: Query planning bottleneck
    qp_latencies = []
    total_latencies_for_qp = []
    for raw_attempts, total_dur in zip(
        api["attempts_json"], api["duration_ms"], strict=True
    ):
        attempts = parse_attempts_json(raw_attempts)
        for attempt in attempts:
            if (
                isinstance(attempt, dict)
                and attempt.get("operation") == "query_planning"
            ):
                qp_latencies.append(attempt.get("duration_ms", 0))
                total_latencies_for_qp.append(total_dur)
    if qp_latencies and total_latencies_for_qp:
        qp_pct = [
            round(qp / total * 100, 1)
            for qp, total in zip(qp_latencies, total_latencies_for_qp)
            if total > 0
        ]
        results["q46_query_planning_bottleneck"] = {
            "avg_qp_latency": round(sum(qp_latencies) / len(qp_latencies), 1),
            "avg_pct_of_total": round(sum(qp_pct) / len(qp_pct), 1) if qp_pct else 0,
            "count": len(qp_latencies),
            "title": "Query Planning as % of Total Latency",
            "chart_type": "stat",
        }
    else:
        results["q46_query_planning_bottleneck"] = {
            "title": "Query Planning Bottleneck",
            "chart_type": "stat",
            "avg_qp_latency": 0,
        }

    LOGGER.info("Part B complete: %d questions processed", len(results))
