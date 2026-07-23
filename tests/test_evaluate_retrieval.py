import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import evaluate_retrieval
from evaluate_retrieval import (
    RerankerPacer,
    evaluate_fixed_case,
    load_case_suite,
    load_plan_snapshot,
    plan_snapshot_document,
    run_with_fixed_candidates,
    source_filter_clause,
)


def candidate(product_id):
    return {
        "id": f"doc-{product_id}",
        "text": str(product_id),
        "metadata": {
            "source_type": "mysql",
            "source_table": "search_ready",
            "id": product_id,
        },
    }


def test_case_suite_supports_strict_settings_and_legacy_lists(tmp_path):
    configured = tmp_path / "configured.json"
    configured.write_text(
        json.dumps(
            {
                "reranker_runs": 3,
                "minimum_median_mrr": 0.75,
                "cases": [{"name": "one", "query": "camera"}],
            }
        ),
        encoding="utf-8",
    )
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps([{"name": "one", "query": "camera"}]),
        encoding="utf-8",
    )

    settings, cases = load_case_suite(configured)
    legacy_settings, legacy_cases = load_case_suite(legacy)

    assert settings["reranker_runs"] == 3
    assert settings["minimum_median_mrr"] == 0.75
    assert cases[0]["name"] == "one"
    assert legacy_settings == {}
    assert legacy_cases == cases


def test_plan_snapshot_rejects_a_stale_planner_fingerprint(tmp_path):
    path = tmp_path / "plans.json"
    payload = plan_snapshot_document("gainr", "old")
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="stale 'planner_fingerprint'"):
        load_plan_snapshot(
            path,
            company_id="gainr",
            planner_fingerprint="new",
            refresh=False,
        )


def test_reranker_pacer_respects_the_provider_interval(monkeypatch):
    clock = [0.0]
    sleeps = []

    monkeypatch.setattr(
        evaluate_retrieval.time,
        "monotonic",
        lambda: clock[0],
    )

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(evaluate_retrieval.time, "sleep", sleep)
    pacer = RerankerPacer(21)

    pacer.wait()
    clock[0] = 5
    pacer.wait()

    assert sleeps == [16]
    assert pacer.last_started == 21


def test_source_filter_clause_supports_ranges_and_unpriced_rows():
    config = SimpleNamespace()

    clause, params = source_filter_clause(
        config,
        "rental_fee",
        {"$lte_or_null": 1000},
    )

    assert clause == "(`rental_fee` <= %s OR `rental_fee` IS NULL)"
    assert params == [1000]


def test_source_filter_clause_rejects_ambiguous_operators():
    config = SimpleNamespace()

    with pytest.raises(ValueError, match="exactly one operator"):
        source_filter_clause(
            config,
            "rental_fee",
            {"$gte": 10, "$lte": 1000},
        )


def test_fixed_candidate_runs_reuse_one_candidate_set():
    class FakeEngine:
        search_table = "search_ready"
        search_id_column = "id"

        def __init__(self):
            self.candidates = [candidate(1), candidate(2), candidate(3)]
            self.rank_calls = []

        def rank(
            self,
            query,
            candidates,
            query_plan=None,
            top_k=None,
            trace_id="-",
        ):
            self.rank_calls.append(
                [item["metadata"]["id"] for item in candidates]
            )
            ordered = (
                candidates
                if len(self.rank_calls) % 2
                else list(reversed(candidates))
            )
            return {
                "results": ordered[:top_k],
                "provider": "primary",
                "attempts": [
                    {"provider": "primary", "status": "success"}
                ],
                "degraded": False,
            }

        def search(self, query, limit=None, planned_result=None):
            ranked = self.rank(
                query,
                self.candidates,
                planned_result["query_plan"],
                top_k=limit,
            )
            return {
                "product_ids": [
                    item["metadata"]["id"]
                    for item in ranked["results"]
                ],
                "hybrid_tail_candidates": [],
                "related_product_ids": [],
            }

    engine = FakeEngine()
    planned = {
        "query_plan": {
            "semantic_query": "camera",
            "keyword_query": "camera",
            "execution_path": "semantic",
        },
        "resolved_filters": {"categorical": {}},
        "unresolved_filters": {},
    }

    fixed = run_with_fixed_candidates(
        engine,
        {"name": "camera", "query": "camera", "result_limit": 2},
        planned,
        runs=3,
    )

    assert engine.rank_calls == [[1, 2, 3]] * 3
    assert fixed["candidate_ids"] == ["1", "2", "3"]
    assert fixed["result_ids_by_run"] == [
        ["1", "2"],
        ["3", "2"],
        ["1", "2"],
    ]
    assert [run["provider"] for run in fixed["reranker_runs"]] == [
        "primary",
        "primary",
        "primary",
    ]


def test_strict_case_threshold_uses_median_rank():
    case = {
        "name": "wanted",
        "query": "wanted bike",
        "relevant_ids": ["relevant"],
        "min_reciprocal_rank": 0.2,
        "min_hit_rate_at_10": 1.0,
        "min_candidate_recall": 1.0,
    }
    result_ids = [str(index) for index in range(1, 10)] + ["relevant"]
    fixed = {
        "execution_path": "deterministic_filter",
        "candidate_ids": result_ids,
        "candidate_fingerprint": "fixed",
        "result_ids_by_run": [result_ids, result_ids, result_ids],
        "reranker_runs": [],
    }

    report = evaluate_fixed_case(
        SimpleNamespace(database=None),
        case,
        fixed,
    )

    assert report["median_reciprocal_rank"] == 0.1
    assert report["success"] is False
    assert "median_rr<0.200" in report["failures"]
