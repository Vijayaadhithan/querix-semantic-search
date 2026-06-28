import json

from mysql_store import (
    fetch_product_types_by_ids,
    fetch_products_by_ids,
    mysql_connection,
    mysql_source_name,
    quote_mysql_identifier,
    require_pymysql,
)
from query_planner import (
    build_query_filter_catalog,
    enrich_query_plan,
    extract_duration_filter,
    extract_price_constraints,
    extract_query_plan,
    infer_target_ad_type,
    normalize_filter_value,
    parse_query_plan,
    query_filter_value_index,
    resolve_query_filters,
)
from reranker import load_reranker, rerank
from retrieval import (
    bm25_search,
    extract_product_ids,
    filter_candidates_by_ad_type,
    load_collection,
    merge_results,
    metadata_matches_filters,
    vector_search,
)
from search_engine import ProductSearchEngine
from settings import (
    APP_NAME,
    MYSQL_DATABASE,
    MYSQL_RESULT_ID_COLUMN,
    MYSQL_RESULT_TABLE,
    MYSQL_SEARCH_ID_COLUMN,
    MYSQL_TABLE,
    QUERY_EXTRACT_MODEL,
    RERANK_MODEL,
)


def main():
    print("Opening Chroma collection...", flush=True)
    engine = ProductSearchEngine()
    bm25_count = engine.bm25_index.count()
    if not bm25_count:
        print(
            "No persistent BM25 product index found. Run: "
            "python src/ingest.py --mysql-bm25-only"
        )
        engine.close()
        return

    print(f"Opened BM25 index with {bm25_count} products.", flush=True)
    print(f"\n{APP_NAME} semantic product search ready. Type 'exit' to quit.\n")

    try:
        while True:
            question = input("Ask: ").strip()
            if question.lower() in ["exit", "quit"]:
                break
            if not question:
                continue

            print(
                f"Extracting search intent with {QUERY_EXTRACT_MODEL}...",
                end="",
                flush=True,
            )
            planned = engine.plan(question)
            query_plan = planned["query_plan"]
            print(f" done ({planned['seconds']:.2f}s).", flush=True)
            if query_plan["fallback_reason"]:
                print(
                    "Query extraction failed; using the original query for vector and "
                    f"BM25 search. Reason: {query_plan['fallback_reason']}"
                )

            unresolved_filters = planned["unresolved_filters"]
            if unresolved_filters:
                print(
                    "Ignoring filters that do not exactly match indexed values: "
                    f"{json.dumps(unresolved_filters, ensure_ascii=False)}"
                )

            visible_plan = {
                key: value
                for key, value in query_plan.items()
                if key != "fallback_reason"
            }
            print(
                "Query plan: "
                f"{json.dumps(visible_plan, ensure_ascii=False)}"
            )
            print("Searching...", end="", flush=True)
            retrieved = engine.retrieve(
                query_plan,
                planned["resolved_filters"],
            )
            merged = retrieved["candidates"]
            if not merged:
                target_label = (
                    "wanted ads"
                    if query_plan["target_ad_type"] == "wanted"
                    else "offer ads"
                )
                print(
                    f" done.\n\nNo matching {target_label} found after applying "
                    "the requested filters.\n"
                )
                print("-" * 80 + "\n")
                continue

            if engine.ranker is None:
                print(f" loading {RERANK_MODEL}...", end="", flush=True)
                load_seconds = engine.ensure_reranker()
                print(
                    f" loaded ({load_seconds:.2f}s)...",
                    end="",
                    flush=True,
                )
            ranked = engine.rank(question, merged, query_plan)
            reranked = ranked["results"]
            print(
                " done "
                f"(vector {retrieved['vector_seconds']:.2f}s, "
                f"BM25 {retrieved['bm25_seconds']:.3f}s, "
                f"rerank {ranked['seconds']:.2f}s).",
                flush=True,
            )

            product_ids = extract_product_ids(reranked)
            products = fetch_products_by_ids(product_ids)
            print(
                f"\nProducts from {MYSQL_DATABASE}.{MYSQL_RESULT_TABLE} "
                f"({len(products)} rows):\n"
            )
            print(json.dumps(products, ensure_ascii=False, indent=2, default=str))
            print("\n" + "-" * 80 + "\n")
    finally:
        engine.close()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        raise SystemExit(f"Error: {exc}") from exc
