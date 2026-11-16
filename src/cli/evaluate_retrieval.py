import argparse
import json
import time
from pathlib import Path
from statistics import mean, median

from cli.retrieval_evaluation import (
    RerankerPacer,
    evaluate_fixed_case,
    fixed_candidate_fingerprint,
    reranker_record,
    result_ids_from_rank,
    run_with_fixed_candidates,
)
from cli.retrieval_ground_truth import (
    matching_ids_from_filter_groups,
    matching_ids_from_search_table,
    precision_at_k,
    quote_identifier,
    reciprocal_rank,
    relevant_ids_for_case,
    source_filter_clause,
)
from cli.retrieval_snapshots import (
    SNAPSHOT_VERSION,
    load_plan_snapshot,
    plan_snapshot_document,
    planned_result_for_case,
    planner_record,
    snapshot_planned_result,
)
from core.settings import PROJECT_ROOT
from core.tenant_config import discover_tenant_profiles
from search.bm25 import PersistentBM25Index
from search.engine import ProductSearchEngine
from search.policy_registry import build_search_policy
from storage.index_generations import resolve_generation
from storage.vector import get_tenant_vector_collection

__all__ = (
    "DEFAULT_CASES_PATH",
    "RerankerPacer",
    "SNAPSHOT_VERSION",
    "evaluate_fixed_case",
    "fixed_candidate_fingerprint",
    "load_case_suite",
    "load_plan_snapshot",
    "main",
    "matching_ids_from_filter_groups",
    "matching_ids_from_search_table",
    "plan_snapshot_document",
    "planned_result_for_case",
    "planner_record",
    "precision_at_k",
    "quote_identifier",
    "reciprocal_rank",
    "relevant_ids_for_case",
    "reranker_record",
    "result_ids_from_rank",
    "run_with_fixed_candidates",
    "snapshot_planned_result",
    "source_filter_clause",
    "time",
)

DEFAULT_CASES_PATH = PROJECT_ROOT / "eval" / "retrieval_cases.json"


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
            "minimum delay between hosted reranker calls (default: suite setting or 0)"
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
        profile = resolve_generation(profiles[args.company]).profile
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
        search_policy=build_search_policy(profile.search_policy),
        vector_post_filter_metadata=False,
        semantic_related_tail_enabled=(profile.retrieval.semantic_related_tail_enabled),
        semantic_related_tail_requires_explicit_category=(
            profile.retrieval.semantic_related_tail_requires_explicit_category
        ),
        reranker_relative_score_floor=(profile.retrieval.reranker_relative_score_floor),
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
            providers = [run["provider"] for run in report["reranker_runs"]]
            fallback_count = sum(run["fallbacks"] for run in report["reranker_runs"])
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
        mean(report["reciprocal_ranks"][run_index] for report in case_reports)
        for run_index in range(runs)
    ]
    median_mrr = float(median(run_mrrs)) if run_mrrs else 0.0
    minimum_median_mrr = float(suite_settings.get("minimum_median_mrr", 0.0))
    reranker_fallbacks = sum(
        run["fallbacks"] for report in case_reports for run in report["reranker_runs"]
    )
    planner_fallbacks = sum(report["fallbacks"] for report in planner_reports)
    max_reranker_fallbacks = int(suite_settings.get("max_reranker_fallbacks", 0))
    suite_failures = []
    if median_mrr < minimum_median_mrr:
        suite_failures.append(f"median_mrr<{minimum_median_mrr:.3f}")
    if reranker_fallbacks > max_reranker_fallbacks:
        suite_failures.append(f"reranker_fallbacks>{max_reranker_fallbacks}")
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
