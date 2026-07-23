import argparse
import hashlib
import json
import time
from copy import deepcopy
from pathlib import Path
from statistics import mean, median

from bm25_index import PersistentBM25Index
from mysql_store import mysql_connection, quote_mysql_identifier
from postgres_store import (
    PostgresRuntimeConfig,
    postgres_connection,
    qualified_table,
    quote_postgres_identifier,
)
from retrieval import extract_product_ids
from search_engine import ProductSearchEngine
from settings import PROJECT_ROOT
from tenant_config import discover_tenant_profiles
from vector_store import get_tenant_vector_collection

DEFAULT_CASES_PATH = PROJECT_ROOT / "eval" / "retrieval_cases.json"
SNAPSHOT_VERSION = 1


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
            remaining = self.minimum_interval_seconds - (
                now - self.last_started
            )
            if remaining > 0:
                time.sleep(remaining)
        self.last_started = time.monotonic()


def reciprocal_rank(result_ids: list[str], relevant_ids: set[str]) -> float:
    for rank, product_id in enumerate(result_ids, start=1):
        if product_id in relevant_ids:
            return 1.0 / rank
    return 0.0


def precision_at_k(
    result_ids: list[str],
    relevant_ids: set[str],
    k: int,
) -> float:
    considered = result_ids[:k]
    if not considered:
        return 0.0
    return sum(product_id in relevant_ids for product_id in considered) / len(
        considered
    )


def load_case_suite(path: Path) -> tuple[dict, list[dict]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return {}, payload
    if not isinstance(payload, dict) or not isinstance(
        payload.get("cases"),
        list,
    ):
        raise ValueError(
            "Retrieval cases must be a JSON list or an object containing cases."
        )
    settings = {key: value for key, value in payload.items() if key != "cases"}
    return settings, payload["cases"]


def quote_identifier(config, value: str) -> str:
    if isinstance(config, PostgresRuntimeConfig):
        return quote_postgres_identifier(value)
    return quote_mysql_identifier(value)


def matching_ids_from_search_table(
    config,
    result_ids: list[str],
    filters: dict,
) -> set[str]:
    if not result_ids:
        return set()
    table = (
        qualified_table(config, config.search_table)
        if isinstance(config, PostgresRuntimeConfig)
        else quote_mysql_identifier(config.search_table)
    )
    placeholders = ", ".join(["%s"] * len(result_ids))
    clauses = [
        f"{quote_identifier(config, config.search_id_column)} "
        f"IN ({placeholders})"
    ]
    params: list = list(result_ids)
    for column, expected in filters.items():
        clauses.append(f"{quote_identifier(config, column)} = %s")
        params.append(expected)
    context = (
        postgres_connection(config)
        if isinstance(config, PostgresRuntimeConfig)
        else mysql_connection(config=config)
    )
    with context as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {quote_identifier(config, config.search_id_column)}
                FROM {table}
                WHERE {' AND '.join(clauses)}
                """,
                params,
            )
            return {str(row[0]) for row in cursor.fetchall()}


def matching_ids_from_filter_groups(
    config,
    result_ids: list[str],
    filter_groups: list[dict],
) -> tuple[set[str], int | None]:
    for index, filters in enumerate(filter_groups):
        matching = matching_ids_from_search_table(
            config,
            result_ids,
            filters,
        )
        if matching:
            return matching, index
    return set(), None


def plan_snapshot_document(
    company_id: str,
    planner_fingerprint: str,
) -> dict:
    return {
        "version": SNAPSHOT_VERSION,
        "company": company_id,
        "planner_fingerprint": planner_fingerprint,
        "plans": {},
    }


def load_plan_snapshot(
    path: Path | None,
    *,
    company_id: str,
    planner_fingerprint: str,
    refresh: bool,
) -> dict:
    expected = plan_snapshot_document(company_id, planner_fingerprint)
    if path is None or refresh or not path.exists():
        return expected
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key, value in expected.items():
        if key == "plans":
            continue
        if payload.get(key) != value:
            raise ValueError(
                f"Plan snapshot {path} has stale {key!r}; "
                "rerun with --refresh-plans."
            )
    if not isinstance(payload.get("plans"), dict):
        raise ValueError(f"Plan snapshot {path} has no plans object.")
    return payload


def snapshot_planned_result(planned: dict) -> dict:
    return {
        "query_plan": deepcopy(planned["query_plan"]),
        "resolved_filters": deepcopy(planned["resolved_filters"]),
        "unresolved_filters": deepcopy(planned["unresolved_filters"]),
        "query_model_metrics": deepcopy(
            planned.get("query_model_metrics") or {}
        ),
        "seconds": float(planned.get("seconds", 0.0)),
        "plan_cache_hit": bool(planned.get("plan_cache_hit")),
    }


def planned_result_for_case(
    engine: ProductSearchEngine,
    case: dict,
    snapshot: dict,
) -> tuple[dict, str]:
    name = str(case["name"])
    query = str(case["query"])
    cached = snapshot["plans"].get(name)
    if cached is not None:
        if cached.get("query") != query:
            raise ValueError(
                f"Plan snapshot query changed for case {name!r}; "
                "rerun with --refresh-plans."
            )
        planned = deepcopy(cached["planned_result"])
        planned["seconds"] = 0.0
        planned["plan_cache_hit"] = True
        return planned, "snapshot"
    planned = engine.plan(query)
    snapshot["plans"][name] = {
        "query": query,
        "planned_result": snapshot_planned_result(planned),
    }
    return planned, "live"


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
        "fallbacks": sum(
            attempt.get("status") == "fallback"
            for attempt in attempts
        ),
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
    return [
        str(value)
        for value in dict.fromkeys((*primary_ids, *continuation_ids))
    ][:result_limit]


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
        "execution_path": "semantic",
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
        "reranker_runs": [
            reranker_record(ranked)
            for ranked in ranked_runs
        ],
    }


def relevant_ids_for_case(
    profile,
    case: dict,
    all_observed_ids: list[str],
) -> tuple[set[str], int | None]:
    if case.get("expected_empty"):
        return set(), None
    filter_groups = case.get("acceptable_filters")
    if not filter_groups and (
        case.get("expected_filters") or case.get("source_filters")
    ):
        filter_groups = [
            case.get("expected_filters") or case.get("source_filters")
        ]
    if filter_groups:
        return matching_ids_from_filter_groups(
            profile.database,
            all_observed_ids,
            filter_groups,
        )
    return {str(value) for value in case["relevant_ids"]}, None


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
                *(
                    value
                    for result_ids in result_runs
                    for value in result_ids
                ),
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
            1.0 if not result_ids else 0.0
            for result_ids in result_runs
        ]
        precision_3 = [0.0 for _result_ids in result_runs]
        hit_10 = [False for _result_ids in result_runs]
    else:
        reciprocal_ranks = [
            reciprocal_rank(result_ids, relevant_ids)
            for result_ids in result_runs
        ]
        precision_3 = [
            precision_at_k(result_ids, relevant_ids, 3)
            for result_ids in result_runs
        ]
        hit_10 = [
            bool(set(result_ids[:10]) & relevant_ids)
            for result_ids in result_runs
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
    forbidden_ids = {
        str(value) for value in case.get("forbidden_ids", [])
    }
    forbidden_by_run = [
        sorted(set(result_ids[:10]) & forbidden_ids)
        for result_ids in result_runs
    ]
    failures = []
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
    minimum_candidate_recall = float(
        case.get("min_candidate_recall", 0.0)
    )
    if candidate_recall < minimum_candidate_recall:
        failures.append(
            f"candidate_recall<{minimum_candidate_recall:.3f}"
        )
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


def planner_record(planned: dict, source: str) -> dict:
    metrics = deepcopy(planned.get("query_model_metrics") or {})
    return {
        "source": source,
        "execution_path": planned["query_plan"].get(
            "execution_path",
            "semantic",
        ),
        "model": metrics.get("model", "none"),
        "attempted_models": metrics.get("attempted_models", []),
        "attempts": metrics.get("attempts", []),
        "fallbacks": sum(
            attempt.get("status") == "fallback"
            for attempt in metrics.get("attempts", [])
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run fixed-plan, fixed-candidate, repeated-reranker retrieval gates."
        )
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_PATH,
        help=f"retrieval case JSON file (default: {DEFAULT_CASES_PATH})",
    )
    parser.add_argument("--limit", type=int, help="run only the first N cases")
    parser.add_argument(
        "--company",
        required=True,
        help="tenant profile slug; uses that company's isolated indexes and DB",
    )
    parser.add_argument(
        "--runs",
        type=int,
        help="reranker runs per fixed candidate set (default: suite setting or 3)",
    )
    parser.add_argument(
        "--reranker-delay-seconds",
        type=float,
        help=(
            "minimum delay between hosted reranker calls "
            "(default: suite setting or 0)"
        ),
    )
    parser.add_argument(
        "--plan-snapshot",
        type=Path,
        help="optional reusable query-plan snapshot JSON",
    )
    parser.add_argument(
        "--refresh-plans",
        action="store_true",
        help="ignore and replace the supplied plan snapshot",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="optional JSON report destination",
    )
    args = parser.parse_args()

    suite_settings, cases = load_case_suite(args.cases)
    if args.limit is not None:
        cases = cases[: args.limit]
    runs = int(args.runs or suite_settings.get("reranker_runs", 3))
    if runs <= 0:
        raise SystemExit("--runs must be greater than zero.")
    reranker_delay_seconds = float(
        args.reranker_delay_seconds
        if args.reranker_delay_seconds is not None
        else suite_settings.get("reranker_delay_seconds", 0.0)
    )
    if reranker_delay_seconds < 0:
        raise SystemExit("--reranker-delay-seconds must not be negative.")
    reranker_pacer = RerankerPacer(reranker_delay_seconds)

    profiles = discover_tenant_profiles()
    try:
        profile = profiles[args.company]
    except KeyError as exc:
        available = ", ".join(sorted(profiles)) or "none"
        raise SystemExit(
            f"Unknown company {args.company!r}; available: {available}"
        ) from exc
    index = PersistentBM25Index(profile.storage.bm25_path)
    engine = ProductSearchEngine(
        collection=get_tenant_vector_collection(profile),
        bm25_index=index,
        company_id=profile.company_id,
        mysql_config=profile.database,
        close_bm25_index=True,
        planner_enabled=profile.planner_enabled,
        planner_prompt_context=profile.planner_prompt_context,
        planner_query_aliases=profile.planner_query_aliases,
        vector_post_filter_metadata=False,
        semantic_related_tail_enabled=(
            profile.retrieval.semantic_related_tail_enabled
        ),
        semantic_related_tail_requires_explicit_category=(
            profile.retrieval
            .semantic_related_tail_requires_explicit_category
        ),
        reranker_relative_score_floor=(
            profile.retrieval.reranker_relative_score_floor
        ),
        reranker_min_score_by_provider=(
            profile.retrieval.reranker_min_score_by_provider
        ),
    )
    snapshot = load_plan_snapshot(
        args.plan_snapshot,
        company_id=profile.company_id,
        planner_fingerprint=engine.planner_cache_fingerprint,
        refresh=args.refresh_plans,
    )
    case_reports = []
    planner_reports = []
    try:
        for case in cases:
            planned, plan_source = planned_result_for_case(
                engine,
                case,
                snapshot,
            )
            planner_reports.append(planner_record(planned, plan_source))
            fixed = run_with_fixed_candidates(
                engine,
                case,
                planned,
                runs,
                reranker_pacer,
            )
            report = evaluate_fixed_case(profile, case, fixed)
            case_reports.append(report)
            providers = [
                run["provider"]
                for run in report["reranker_runs"]
            ]
            fallback_count = sum(
                run["fallbacks"]
                for run in report["reranker_runs"]
            )
            print(
                f"{'PASS' if report['success'] else 'FAIL'} "
                f"{case['name']}: path={report['execution_path']} "
                f"plan={plan_source} candidates={report['candidate_count']} "
                f"candidate_hash={report['candidate_fingerprint']} "
                f"candidate_recall={report['candidate_recall']:.3f} "
                f"RR={report['reciprocal_ranks']} "
                f"median_RR={report['median_reciprocal_rank']:.3f} "
                f"median_P@3={report['median_precision_at_3']:.3f} "
                f"hit_rate@10={report['hit_rate_at_10']:.3f} "
                f"providers={providers} fallbacks={fallback_count} "
                f"failures={report['failures'] or 'none'}"
            )
    finally:
        engine.close()

    if args.plan_snapshot is not None:
        args.plan_snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.plan_snapshot.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    run_mrrs = [
        mean(
            report["reciprocal_ranks"][run_index]
            for report in case_reports
        )
        for run_index in range(runs)
    ]
    median_mrr = float(median(run_mrrs)) if run_mrrs else 0.0
    minimum_median_mrr = float(
        suite_settings.get("minimum_median_mrr", 0.0)
    )
    reranker_fallbacks = sum(
        run["fallbacks"]
        for report in case_reports
        for run in report["reranker_runs"]
    )
    planner_fallbacks = sum(
        report["fallbacks"]
        for report in planner_reports
    )
    max_reranker_fallbacks = int(
        suite_settings.get("max_reranker_fallbacks", 0)
    )
    suite_failures = []
    if median_mrr < minimum_median_mrr:
        suite_failures.append(
            f"median_mrr<{minimum_median_mrr:.3f}"
        )
    if reranker_fallbacks > max_reranker_fallbacks:
        suite_failures.append(
            f"reranker_fallbacks>{max_reranker_fallbacks}"
        )
    passed = sum(report["success"] for report in case_reports)
    success = passed == len(case_reports) and not suite_failures
    summary = {
        "success": success,
        "company": profile.company_id,
        "case_file": str(args.cases),
        "runs": runs,
        "reranker_delay_seconds": reranker_delay_seconds,
        "passed_cases": passed,
        "total_cases": len(case_reports),
        "run_mrrs": run_mrrs,
        "median_mrr": median_mrr,
        "minimum_median_mrr": minimum_median_mrr,
        "planner_fallbacks": planner_fallbacks,
        "reranker_fallbacks": reranker_fallbacks,
        "suite_failures": suite_failures,
        "planner_runs": planner_reports,
        "cases": case_reports,
    }
    print(
        f"\n{passed}/{len(case_reports)} cases passed; "
        f"run_MRR={[round(value, 3) for value in run_mrrs]} "
        f"median_MRR={median_mrr:.3f} "
        f"required={minimum_median_mrr:.3f} "
        f"planner_fallbacks={planner_fallbacks} "
        f"reranker_fallbacks={reranker_fallbacks} "
        f"suite_failures={suite_failures or 'none'}"
    )
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
