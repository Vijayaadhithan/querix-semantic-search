import argparse
import json
from pathlib import Path

from bm25_index import PersistentBM25Index
from mysql_store import mysql_connection, quote_mysql_identifier
from postgres_store import (
    PostgresRuntimeConfig,
    postgres_connection,
    qualified_table,
    quote_postgres_identifier,
)
from search_engine import ProductSearchEngine
from settings import PROJECT_ROOT
from tenant_config import discover_tenant_profiles
from vector_store import get_tenant_vector_collection

DEFAULT_CASES_PATH = PROJECT_ROOT / "eval" / "retrieval_cases.json"


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


def filter_reciprocal_rank(products: list[dict], filters: dict) -> tuple[float, list[str]]:
    matches = []
    for rank, product in enumerate(products, start=1):
        if all(product.get(key) == expected for key, expected in filters.items()):
            product_id = product.get("id")
            if product_id is not None:
                matches.append(str(product_id))
            if len(matches) == 1:
                reciprocal = 1.0 / rank
    return (reciprocal if matches else 0.0), matches


def quote_identifier(config, value: str) -> str:
    if isinstance(config, PostgresRuntimeConfig):
        return quote_postgres_identifier(value)
    return quote_mysql_identifier(value)


def matching_ids_from_search_table(config, result_ids: list[str], filters: dict) -> set[str]:
    if not result_ids:
        return set()
    table = (
        qualified_table(config, config.search_table)
        if isinstance(config, PostgresRuntimeConfig)
        else quote_mysql_identifier(config.search_table)
    )
    placeholders = ", ".join(["%s"] * len(result_ids))
    clauses = [f"{quote_identifier(config, config.search_id_column)} IN ({placeholders})"]
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
        matching = matching_ids_from_search_table(config, result_ids, filters)
        if matching:
            return matching, index
    return set(), None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run labeled end-to-end semantic retrieval cases."
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
    args = parser.parse_args()

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.limit is not None:
        cases = cases[: args.limit]

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
    passed = 0
    reciprocal_ranks = []
    try:
        for case in cases:
            result_limit = int(case.get("result_limit", 20))
            result = engine.search(case["query"], limit=result_limit)
            result_ids = [str(value) for value in result["product_ids"]]
            matching: set[str] = set()
            if case.get("expected_empty"):
                success = not result_ids
                rr = 1.0 if success else 0.0
            elif (
                case.get("acceptable_filters")
                or case.get("expected_filters")
                or case.get("source_filters")
            ):
                filter_groups = case.get("acceptable_filters")
                if not filter_groups:
                    filter_groups = [
                        case.get("expected_filters") or case.get("source_filters")
                    ]
                if profile is not None:
                    matching, matched_group = matching_ids_from_filter_groups(
                        profile.database,
                        result_ids,
                        filter_groups,
                    )
                    rr = reciprocal_rank(result_ids, matching)
                    matching_ids = [value for value in result_ids if value in matching]
                else:
                    matched_group = None
                    expected_filters = filter_groups[0]
                    rr, matching_ids = filter_reciprocal_rank(
                        result.get("products", []),
                        expected_filters,
                    )
                success = rr > 0
            else:
                relevant_ids = {str(value) for value in case["relevant_ids"]}
                matching = relevant_ids
                rr = reciprocal_rank(result_ids, relevant_ids)
                success = rr > 0
            precision_3 = precision_at_k(result_ids, matching, 3)
            hit_10 = bool(set(result_ids[:10]) & matching)
            minimum_rr = case.get("min_reciprocal_rank")
            if minimum_rr is not None:
                success = success and rr >= float(minimum_rr)
            minimum_precision_3 = case.get("min_precision_at_3")
            if minimum_precision_3 is not None:
                success = success and precision_3 >= float(
                    minimum_precision_3
                )
            minimum_results = case.get("min_result_count")
            if minimum_results is not None:
                success = success and len(result_ids) >= int(minimum_results)
            forbidden_ids = {
                str(value) for value in case.get("forbidden_ids", [])
            }
            forbidden_top_10 = sorted(set(result_ids[:10]) & forbidden_ids)
            success = success and not forbidden_top_10
            reciprocal_ranks.append(rr)
            passed += int(success)
            label = "PASS" if success else "FAIL"
            if (
                case.get("acceptable_filters")
                or case.get("expected_filters")
                or case.get("source_filters")
            ):
                print(
                    f"{label} {case['name']}: {result_ids} "
                    f"matched={matching_ids if success else []} "
                    f"group={matched_group if matched_group is not None else 'none'} "
                    f"RR={rr:.3f} P@3={precision_3:.3f} "
                    f"hit@10={hit_10} count={len(result_ids)} "
                    f"forbidden@10={forbidden_top_10}"
                )
            else:
                print(
                    f"{label} {case['name']}: {result_ids} "
                    f"RR={rr:.3f} P@3={precision_3:.3f} "
                    f"hit@10={hit_10} count={len(result_ids)} "
                    f"forbidden@10={forbidden_top_10}"
                )
    finally:
        engine.close()

    mean_reciprocal_rank = (
        sum(reciprocal_ranks) / len(reciprocal_ranks)
        if reciprocal_ranks
        else 0.0
    )
    print(
        f"\n{passed}/{len(cases)} retrieval cases passed; "
        f"MRR={mean_reciprocal_rank:.3f}"
    )
    raise SystemExit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    main()
