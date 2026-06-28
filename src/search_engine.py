import time

from bm25_index import PersistentBM25Index
from mysql_store import fetch_products_by_ids, mysql_source_name
from ollama_client import last_ollama_metrics
from query_planner import (
    build_query_filter_catalog,
    enrich_query_plan,
    extract_query_plan,
    query_filter_value_index,
    resolve_query_filters,
)
from reranker import load_reranker, rerank
from retrieval import (
    bm25_search,
    category_fallback_search,
    extract_product_ids,
    filter_candidates_by_ad_type,
    load_collection,
    merge_results,
    vector_search,
)
from settings import (
    BM25_TOP_K,
    HYBRID_CANDIDATE_K,
    RETRIEVAL_OVERFETCH_FACTOR,
    RERANK_TOP_K,
    VECTOR_CANDIDATE_K,
    VECTOR_TOP_K,
)


class ProductSearchEngine:
    def __init__(
        self,
        collection=None,
        bm25_index=None,
        query_provider=None,
        embedding_provider=None,
        ranker=None,
    ):
        self.collection = collection or load_collection()
        self._owns_bm25_index = bm25_index is None
        self.bm25_index = bm25_index or PersistentBM25Index()
        self.query_provider = query_provider
        self.embedding_provider = embedding_provider
        self.ranker = ranker
        self.source_name = mysql_source_name()
        self.filter_value_index = query_filter_value_index(self.bm25_index)
        self.filter_catalog = build_query_filter_catalog(self.filter_value_index)

    def close(self) -> None:
        if self._owns_bm25_index:
            self.bm25_index.close()

    def plan(self, query: str) -> dict:
        started = time.perf_counter()
        query_plan = extract_query_plan(
            query,
            self.filter_catalog,
            query_provider=self.query_provider,
        )
        query_plan = enrich_query_plan(
            query,
            query_plan,
            self.filter_value_index,
        )
        resolved, unresolved = resolve_query_filters(
            query_plan["filters"],
            self.filter_value_index,
        )
        return {
            "query_plan": query_plan,
            "resolved_filters": resolved,
            "unresolved_filters": unresolved,
            "query_model_metrics": (
                last_ollama_metrics()["query_model"]
                if self.query_provider is None
                else {}
            ),
            "seconds": time.perf_counter() - started,
        }

    def retrieve(
        self,
        query_plan: dict,
        resolved_filters: dict,
        result_limit: int | None = None,
    ) -> dict:
        extended_window = (
            result_limit is not None and result_limit > RERANK_TOP_K
        )
        requested = result_limit or RERANK_TOP_K
        retrieval_depth = (
            max(
                VECTOR_TOP_K,
                BM25_TOP_K,
                requested * RETRIEVAL_OVERFETCH_FACTOR,
            )
            if extended_window
            else None
        )
        vector_top_k = retrieval_depth or VECTOR_TOP_K
        bm25_top_k = retrieval_depth or BM25_TOP_K
        hybrid_top_k = max(HYBRID_CANDIDATE_K, requested)

        vector_started = time.perf_counter()
        vector_results = vector_search(
            query_plan["semantic_query"],
            self.collection,
            vector_top_k,
            candidate_k=max(VECTOR_CANDIDATE_K, vector_top_k),
            source_name=self.source_name,
            resolved_filters=resolved_filters,
            embedding_provider=self.embedding_provider,
        )
        vector_seconds = time.perf_counter() - vector_started

        bm25_started = time.perf_counter()
        bm25_results = bm25_search(
            query_plan["keyword_query"],
            self.bm25_index,
            self.collection,
            resolved_filters,
            bm25_top_k,
        )
        bm25_seconds = time.perf_counter() - bm25_started

        category_started = time.perf_counter()
        category_results = []
        if extended_window:
            category_results = category_fallback_search(
                self.bm25_index,
                self.collection,
                resolved_filters,
                query_plan.get("inferred_categories"),
                retrieval_depth,
                exclude_ids={
                    item["id"]
                    for item in (*vector_results, *bm25_results)
                },
            )
        category_seconds = time.perf_counter() - category_started

        merged = merge_results(
            vector_results,
            bm25_results,
            query_plan.get("inferred_categories"),
            category_results=category_results,
        )
        # Apply ad intent before truncating the candidate window. Otherwise a
        # page can be short merely because unwanted ad types occupied the K
        # slots ahead of valid products.
        candidates = filter_candidates_by_ad_type(
            merged,
            query_plan["target_ad_type"],
        )[:hybrid_top_k]
        return {
            "vector_results": vector_results,
            "bm25_results": bm25_results,
            "category_results": category_results,
            "candidates": candidates,
            "vector_seconds": vector_seconds,
            "bm25_seconds": bm25_seconds,
            "category_seconds": category_seconds,
            "embedding_model_metrics": (
                last_ollama_metrics()["embedding_model"]
                if self.embedding_provider is None
                else {}
            ),
        }

    def ensure_reranker(self) -> float:
        if self.ranker is not None:
            return 0.0
        started = time.perf_counter()
        self.ranker = load_reranker()
        return time.perf_counter() - started

    def rank(
        self,
        query: str,
        candidates: list[dict],
        query_plan: dict | None = None,
        top_k: int | None = None,
    ) -> dict:
        load_seconds = self.ensure_reranker()
        ranking_query = query
        if query_plan is not None:
            context = []
            keyword_query = query_plan.get("keyword_query")
            if keyword_query and keyword_query.casefold() != query.casefold():
                context.append(f"Search concepts: {keyword_query}")
            inferred = query_plan.get("inferred_categories") or {}
            category_hints = [
                value
                for value in (
                    inferred.get("main_category"),
                    inferred.get("subcategory"),
                )
                if value
            ]
            if category_hints:
                context.append(
                    "Possible catalog categories: "
                    + ", ".join(dict.fromkeys(category_hints))
                )
            if context:
                ranking_query = query + "\n" + "\n".join(context)
        started = time.perf_counter()
        results = rerank(
            ranking_query,
            candidates,
            self.ranker,
            RERANK_TOP_K if top_k is None else top_k,
            diversity_top_k=RERANK_TOP_K,
        )
        return {
            "results": results,
            "load_seconds": load_seconds,
            "seconds": time.perf_counter() - started,
        }

    def search(self, query: str, limit: int | None = None) -> dict:
        if limit is not None and limit <= 0:
            raise ValueError("Search limit must be greater than zero.")
        planned = self.plan(query)
        retrieved = self.retrieve(
            planned["query_plan"],
            planned["resolved_filters"],
            result_limit=limit,
        )
        candidates = retrieved["candidates"]
        ranked = (
            self.rank(
                query,
                candidates,
                planned["query_plan"],
                top_k=limit,
            )
            if candidates
            else {"results": [], "load_seconds": 0.0, "seconds": 0.0}
        )
        product_ids = extract_product_ids(ranked["results"])
        products = fetch_products_by_ids(product_ids)
        return {
            **planned,
            **retrieved,
            "reranked": ranked["results"],
            "reranker_load_seconds": ranked["load_seconds"],
            "reranker_seconds": ranked["seconds"],
            "product_ids": product_ids,
            "products": products,
        }
