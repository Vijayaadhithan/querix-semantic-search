"""Api request metrics."""

import logging
from collections import Counter

from .report_utils import _rounded_stat

LOGGER = logging.getLogger(__name__)


def request_outcome_metrics(results, api) -> None:
    # Q21: Success vs failure rate
    status_counts = api["status"].value_counts().to_dict()
    results["q21_success_rate"] = {
        "labels": list(status_counts.keys()),
        "values": list(status_counts.values()),
        "title": "Request Success vs Failure Rate",
        "chart_type": "doughnut",
    }

    # Q22: Execution path distribution
    path_counts = api["execution_path"].value_counts().to_dict()
    results["q22_execution_paths"] = {
        "labels": list(path_counts.keys()),
        "values": list(path_counts.values()),
        "title": "Execution Path Distribution",
        "chart_type": "doughnut",
    }


def latency_metrics(results, api) -> None:
    # Q23: Latency stats
    latencies = api["duration_ms"].dropna()
    results["q23_latency_stats"] = {
        "avg": _rounded_stat(latencies.mean()),
        "median": _rounded_stat(latencies.median()),
        "p95": _rounded_stat(latencies.quantile(0.95)),
        "p99": _rounded_stat(latencies.quantile(0.99)),
        "min": _rounded_stat(latencies.min()),
        "max": _rounded_stat(latencies.max()),
        "std": _rounded_stat(latencies.std()),
        "title": "Latency Statistics (ms)",
        "chart_type": "stats_card",
    }
    # Latency histogram
    bins = [0, 500, 1000, 2000, 3000, 4000, 5000, 7000, 10000]
    hist_vals = []
    hist_labels = []
    for i in range(len(bins) - 1):
        count = int(((latencies >= bins[i]) & (latencies < bins[i + 1])).sum())
        hist_vals.append(count)
        hist_labels.append(f"{bins[i]}-{bins[i + 1]}")
    count = int((latencies >= bins[-1]).sum())
    hist_vals.append(count)
    hist_labels.append(f"{bins[-1]}+")
    results["q23_latency_histogram"] = {
        "labels": hist_labels,
        "values": hist_vals,
        "title": "Latency Distribution (ms)",
        "chart_type": "bar",
    }

    # Q24: Latency by execution path
    path_latency = {}
    for path in api["execution_path"].unique():
        path_data = api[api["execution_path"] == path]["duration_ms"].dropna()
        path_latency[path] = {
            "avg": round(float(path_data.mean()), 1),
            "median": round(float(path_data.median()), 1),
            "p95": round(float(path_data.quantile(0.95)), 1)
            if len(path_data) > 1
            else round(float(path_data.max()), 1),
            "count": int(len(path_data)),
        }
    results["q24_latency_by_path"] = {
        "data": path_latency,
        "labels": list(path_latency.keys()),
        "avg_values": [v["avg"] for v in path_latency.values()],
        "median_values": [v["median"] for v in path_latency.values()],
        "p95_values": [v["p95"] for v in path_latency.values()],
        "title": "Latency by Execution Path",
        "chart_type": "grouped_bar",
    }

    # Q25: Deterministic vs Semantic comparison
    det = api[api["execution_path"] == "deterministic_filter"]
    sem = api[api["execution_path"] == "semantic"]
    dsem = api[api["execution_path"] == "direct_semantic"]
    results["q25_det_vs_sem"] = {
        "comparison": {
            "deterministic_filter": {
                "count": int(len(det)),
                "avg_latency": round(float(det["duration_ms"].mean()), 1)
                if len(det) > 0
                else 0,
                "avg_results": round(float(det["total_results"].mean()), 1)
                if len(det) > 0
                else 0,
                "avg_tokens": round(float(det["total_tokens"].mean()), 1)
                if len(det) > 0
                else 0,
            },
            "semantic": {
                "count": int(len(sem)),
                "avg_latency": round(float(sem["duration_ms"].mean()), 1)
                if len(sem) > 0
                else 0,
                "avg_results": round(float(sem["total_results"].mean()), 1)
                if len(sem) > 0
                else 0,
                "avg_tokens": round(float(sem["total_tokens"].mean()), 1)
                if len(sem) > 0
                else 0,
            },
            "direct_semantic": {
                "count": int(len(dsem)),
                "avg_latency": round(float(dsem["duration_ms"].mean()), 1)
                if len(dsem) > 0
                else 0,
                "avg_results": round(float(dsem["total_results"].mean()), 1)
                if len(dsem) > 0
                else 0,
                "avg_tokens": round(float(dsem["total_tokens"].mean()), 1)
                if len(dsem) > 0
                else 0,
            },
        },
        "title": "Execution Path Comparison",
        "chart_type": "comparison_table",
    }


def token_consumption_metrics(results, api) -> None:
    # Q26: Token consumption breakdown
    results["q26_token_consumption"] = {
        "input_tokens": {
            "total": int(api["input_tokens"].sum()),
            "avg": _rounded_stat(api["input_tokens"].mean()),
        },
        "output_tokens": {
            "total": int(api["output_tokens"].sum()),
            "avg": _rounded_stat(api["output_tokens"].mean()),
        },
        "thought_tokens": {
            "total": int(api["thought_tokens"].sum()),
            "avg": _rounded_stat(api["thought_tokens"].mean()),
        },
        "total_tokens": {
            "total": int(api["total_tokens"].sum()),
            "avg": _rounded_stat(api["total_tokens"].mean()),
        },
        "labels": ["Input Tokens", "Output Tokens", "Thought Tokens"],
        "values": [
            int(api["input_tokens"].sum()),
            int(api["output_tokens"].sum()),
            int(api["thought_tokens"].sum()),
        ],
        "title": "Token Consumption Breakdown",
        "chart_type": "doughnut",
    }

    # Q27: Token usage per execution path
    token_by_path = {}
    for path in api["execution_path"].unique():
        path_data = api[api["execution_path"] == path]
        token_by_path[path] = {
            "avg_total": round(float(path_data["total_tokens"].mean()), 1),
            "total": int(path_data["total_tokens"].sum()),
            "avg_input": round(float(path_data["input_tokens"].mean()), 1),
            "avg_output": round(float(path_data["output_tokens"].mean()), 1),
        }
    results["q27_tokens_by_path"] = {
        "data": token_by_path,
        "labels": list(token_by_path.keys()),
        "values": [v["avg_total"] for v in token_by_path.values()],
        "title": "Average Token Usage per Execution Path",
        "chart_type": "bar",
    }


def result_distribution_metrics(
    results,
    api,
    provider_stats,
    successful_text_api,
    text_api,
    text_searches,
    total_requests,
) -> object:
    # Q34: Estimated API cost
    groq_input = provider_stats.get("groq", {}).get("total_tokens", 0)
    voyage_input = provider_stats.get("voyage-2.5", {}).get("total_tokens", 0)
    estimated_cost = (groq_input * 0.05 / 1_000_000) + (voyage_input * 0.05 / 1_000_000)
    results["q34_estimated_cost"] = {
        "total_tokens_all": int(api["total_tokens"].sum()),
        "groq_tokens": int(groq_input),
        "voyage_tokens": int(voyage_input),
        "estimated_cost_usd": round(estimated_cost, 4),
        "cost_per_search": round(estimated_cost / total_requests * 1000, 4)
        if total_requests > 0
        else 0,
        "title": "Estimated API Cost",
        "chart_type": "stat",
    }

    # Q35: Result count distribution
    rc = successful_text_api["result_count"].dropna()
    rc_dist = Counter()
    for r in rc:
        if r == 0:
            rc_dist["0 results"] += 1
        elif r <= 5:
            rc_dist["1-5"] += 1
        elif r <= 10:
            rc_dist["6-10"] += 1
        elif r <= 20:
            rc_dist["11-20"] += 1
        else:
            rc_dist["20+"] += 1
    results["q35_result_distribution"] = {
        "labels": ["0 results", "1-5", "6-10", "11-20", "20+"],
        "values": [
            rc_dist.get(k, 0) for k in ["0 results", "1-5", "6-10", "11-20", "20+"]
        ],
        "avg_result_count": _rounded_stat(successful_text_api["result_count"].mean()),
        "avg_total_results": _rounded_stat(successful_text_api["total_results"].mean()),
        "title": "Text-Search Result Count Distribution",
        "chart_type": "bar",
    }

    # Q36: Zero-result rate
    zero_results = int((successful_text_api["total_results"] == 0).sum())
    results["q36_zero_result_rate"] = {
        "zero_count": zero_results,
        "total": len(successful_text_api),
        "total_text_requests": len(text_api),
        "failed_text_requests": len(text_api) - len(successful_text_api),
        "percentage": (
            round(zero_results / len(successful_text_api) * 100, 1)
            if len(successful_text_api)
            else 0
        ),
        "title": "Text-Search Zero-Result Rate",
        "chart_type": "stat",
    }

    # Q37: Average total_results by path
    tr_by_path = {}
    for path in successful_text_api["execution_path"].unique():
        path_data = successful_text_api[successful_text_api["execution_path"] == path]
        tr_by_path[path] = round(float(path_data["total_results"].mean()), 1)
    results["q37_results_by_path"] = {
        "labels": list(tr_by_path.keys()),
        "values": list(tr_by_path.values()),
        "title": "Average Text-Search Results by Execution Path",
        "chart_type": "bar",
    }

    # Q38: Zero-result query terms
    merged = successful_text_api.merge(
        text_searches[["request_id", "query_text"]],
        on="request_id",
        how="left",
    )
    zero_q = merged[merged["total_results"] == 0]["query_text"].dropna().tolist()
    results["q38_zero_result_queries"] = {
        "queries": zero_q,
        "count": len(zero_q),
        "title": "Zero-Result Search Terms",
        "chart_type": "list",
    }
    return merged
