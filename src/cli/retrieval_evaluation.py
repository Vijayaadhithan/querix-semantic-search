"""Evaluate repeated reranking runs against an identical candidate set."""

import hashlib
import time
from copy import deepcopy
from statistics import mean, median

from cli.retrieval_ground_truth import (
    precision_at_k,
    reciprocal_rank,
    relevant_ids_for_case,
)
from search.engine import ProductSearchEngine
from search.retrieval import extract_product_ids


class RerankerPacer:
    """Space hosted reranker calls so the evaluator does not create fallbacks."""

    def __init__(self, minimum_interval_seconds: float):
        if minimum_interval_seconds < 0:
            raise ValueError("reranker interval must not be negative")
        self.minimum_interval_seconds = minimum_interval_seconds
        self.last_started: float | None = None

    def wait(self) -> None:
        now = time.monotonic()
        if self.last_started is not None:
            remaining = self.minimum_interval_seconds - (now - self.last_started)
            if remaining > 0:
                time.sleep(remaining)
        self.last_started = time.monotonic()


def fixed_candidate_fingerprint(candidate_ids: list[str]) -> str:
    payload = "\0".join(candidate_ids).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def reranker_record(ranked: dict) -> dict:
    attempts = deepcopy(ranked.get("attempts") or [])
    return {
        "provider": str(ranked.get("provider") or "none"),
        "degraded": bool(ranked.get("degraded")),
        "error_type": ranked.get("error_type"),
        "attempts": attempts,
        "fallbacks": sum(attempt.get("status") == "fallback" for attempt in attempts),
    }


def result_ids_from_rank(
    engine: ProductSearchEngine,
    ranked: dict,
    continuation_ids: list,
    result_limit: int,
) -> list[str]:
    primary_ids = extract_product_ids(
        ranked.get("results") or [],
        search_table=engine.search_table,
        search_id_column=engine.search_id_column,
    )
    return [str(value) for value in dict.fromkeys((*primary_ids, *continuation_ids))][
        :result_limit
    ]


def run_with_fixed_candidates(
    engine: ProductSearchEngine,
    case: dict,
    planned: dict,
    runs: int,
    reranker_pacer: RerankerPacer | None = None,
) -> dict:
    query = str(case["query"])
    result_limit = int(case.get("result_limit", 20))
    captured: dict = {}
    original_rank = engine.rank

    def capture_rank(
        ranking_query,
        candidates,
        query_plan=None,
        top_k=None,
        trace_id="-",
    ):
        if reranker_pacer is not None:
            reranker_pacer.wait()
        ranked = original_rank(
            ranking_query,
            candidates,
            query_plan,
            top_k,
            trace_id,
        )
        captured.update(
            {
                "candidates": deepcopy(candidates),
                "query_plan": deepcopy(query_plan),
                "top_k": top_k,
                "first_ranked": deepcopy(ranked),
            }
        )
        return ranked

    engine.rank = capture_rank
    try:
        first = engine.search(
            query,
            limit=result_limit,
            planned_result=planned,
        )
    finally:
        engine.rank = original_rank

    if not captured:
        result_ids = [str(value) for value in first["product_ids"]]
        return {
            "execution_path": planned["query_plan"].get(
                "execution_path",
                "semantic",
            ),
            "candidate_ids": result_ids,
            "candidate_fingerprint": fixed_candidate_fingerprint(result_ids),
            "result_ids_by_run": [result_ids for _index in range(runs)],
            "reranker_runs": [
                {
                    "provider": "none",
                    "degraded": False,
                    "error_type": None,
                    "attempts": [],
                    "fallbacks": 0,
                }
                for _index in range(runs)
            ],
        }

    candidates = captured["candidates"]
    candidate_ids = [
        str(value)
        for value in extract_product_ids(
            candidates,
            search_table=engine.search_table,
            search_id_column=engine.search_id_column,
        )
    ]
    continuation_ids = [
        *extract_product_ids(
            first.get("hybrid_tail_candidates") or [],
            search_table=engine.search_table,
            search_id_column=engine.search_id_column,
        ),
        *(first.get("related_product_ids") or []),
    ]
    ranked_runs = [captured["first_ranked"]]
    for run_index in range(1, runs):
        if reranker_pacer is not None:
            reranker_pacer.wait()
        ranked_runs.append(
            original_rank(
                query,
                deepcopy(candidates),
                deepcopy(captured["query_plan"]),
                captured["top_k"],
                trace_id=f"eval:{case['name']}:{run_index + 1}",
            )
        )
    return {
        "execution_path": planned["query_plan"].get(
            "execution_path",
            "semantic",
        ),
        "candidate_ids": candidate_ids,
        "candidate_fingerprint": fixed_candidate_fingerprint(candidate_ids),
        "result_ids_by_run": [
            result_ids_from_rank(
                engine,
                ranked,
                continuation_ids,
                result_limit,
            )
            for ranked in ranked_runs
        ],
        "reranker_runs": [reranker_record(ranked) for ranked in ranked_runs],
    }


def evaluate_fixed_case(
    profile,
    case: dict,
    fixed: dict,
) -> dict:
    result_runs = fixed["result_ids_by_run"]
    all_observed_ids = list(
        dict.fromkeys(
            (
                *fixed["candidate_ids"],
                *(value for result_ids in result_runs for value in result_ids),
            )
        )
    )
    relevant_ids, matched_group = relevant_ids_for_case(
        profile,
        case,
        all_observed_ids,
    )
    if case.get("expected_empty"):
        reciprocal_ranks = [
            1.0 if not result_ids else 0.0 for result_ids in result_runs
        ]
        precision_3 = [0.0 for _result_ids in result_runs]
        hit_10 = [False for _result_ids in result_runs]
    else:
        reciprocal_ranks = [
            reciprocal_rank(result_ids, relevant_ids) for result_ids in result_runs
        ]
        precision_3 = [
            precision_at_k(result_ids, relevant_ids, 3) for result_ids in result_runs
        ]
        hit_10 = [
            bool(set(result_ids[:10]) & relevant_ids) for result_ids in result_runs
        ]
    median_rr = float(median(reciprocal_ranks))
    median_precision_3 = float(median(precision_3))
    hit_rate_10 = mean(hit_10) if hit_10 else 0.0
    candidate_hits = set(fixed["candidate_ids"]) & relevant_ids
    candidate_recall = (
        len(candidate_hits) / len(relevant_ids)
        if relevant_ids
        else (1.0 if case.get("expected_empty") else 0.0)
    )
    forbidden_ids = {str(value) for value in case.get("forbidden_ids", [])}
    forbidden_by_run = [
        sorted(set(result_ids[:10]) & forbidden_ids) for result_ids in result_runs
    ]
    failures = []
    expected_execution_path = case.get("expected_execution_path")
    if (
        expected_execution_path is not None
        and fixed["execution_path"] != expected_execution_path
    ):
        failures.append("execution_path!=" + str(expected_execution_path))
    minimum_rr = float(case.get("min_reciprocal_rank", 0.0))
    if median_rr < minimum_rr:
        failures.append(f"median_rr<{minimum_rr:.3f}")
    minimum_precision = float(case.get("min_precision_at_3", 0.0))
    if median_precision_3 < minimum_precision:
        failures.append(f"median_p@3<{minimum_precision:.3f}")
    minimum_hit_rate = float(case.get("min_hit_rate_at_10", 0.0))
    if hit_rate_10 < minimum_hit_rate:
        failures.append(f"hit_rate@10<{minimum_hit_rate:.3f}")
    minimum_results = int(case.get("min_result_count", 0))
    if any(len(result_ids) < minimum_results for result_ids in result_runs):
        failures.append(f"result_count<{minimum_results}")
    minimum_candidate_recall = float(case.get("min_candidate_recall", 0.0))
    if candidate_recall < minimum_candidate_recall:
        failures.append(f"candidate_recall<{minimum_candidate_recall:.3f}")
    if any(forbidden_by_run):
        failures.append("forbidden_id_in_top_10")
    if not case.get("expected_empty") and median_rr <= 0:
        failures.append("no_relevant_result")
    if case.get("expected_empty") and median_rr < 1:
        failures.append("expected_empty")
    return {
        "name": case["name"],
        "success": not failures,
        "failures": failures,
        "execution_path": fixed["execution_path"],
        "candidate_count": len(fixed["candidate_ids"]),
        "candidate_fingerprint": fixed["candidate_fingerprint"],
        "candidate_recall": candidate_recall,
        "matched_filter_group": matched_group,
        "reciprocal_ranks": reciprocal_ranks,
        "median_reciprocal_rank": median_rr,
        "precision_at_3": precision_3,
        "median_precision_at_3": median_precision_3,
        "hit_rate_at_10": hit_rate_10,
        "result_ids_by_run": result_runs,
        "reranker_runs": fixed["reranker_runs"],
    }
