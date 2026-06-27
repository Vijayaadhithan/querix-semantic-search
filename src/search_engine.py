import time

from bm25_index import PersistentBM25Index
from mysql_store import fetch_products_by_ids, mysql_source_name
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
    extract_product_ids,
    filter_candidates_by_ad_type,
    load_collection,
    merge_results,
    vector_search,
)
from settings import (
    BM25_TOP_K,
    HYBRID_CANDIDATE_K,
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
            "seconds": time.perf_counter() - started,
        }

    def retrieve(self, query_plan: dict, resolved_filters: dict) -> dict:
        vector_started = time.perf_counter()
        vector_results = vector_search(
            query_plan["semantic_query"],
            self.collection,
            VECTOR_TOP_K,
            candidate_k=VECTOR_CANDIDATE_K,
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
            BM25_TOP_K,
        )
        bm25_seconds = time.perf_counter() - bm25_started

        candidates = merge_results(
            vector_results,
            bm25_results,
            query_plan.get("inferred_categories"),
        )[:HYBRID_CANDIDATE_K]
        candidates = filter_candidates_by_ad_type(
            candidates,
            query_plan["target_ad_type"],
        )
        return {
            "vector_results": vector_results,
            "bm25_results": bm25_results,
            "candidates": candidates,
            "vector_seconds": vector_seconds,
            "bm25_seconds": bm25_seconds,
        }

    def ensure_reranker(self) -> float:
        if self.ranker is not None:
            return 0.0
        started = time.perf_counter()
        self.ranker = load_reranker()
        return time.perf_counter() - started

    def rank(self, query: str, candidates: list[dict]) -> dict:
        load_seconds = self.ensure_reranker()
        started = time.perf_counter()
        results = rerank(
            query,
            candidates,
            self.ranker,
            RERANK_TOP_K,
        )
        return {
            "results": results,
            "load_seconds": load_seconds,
            "seconds": time.perf_counter() - started,
        }

    def search(self, query: str) -> dict:
        planned = self.plan(query)
        retrieved = self.retrieve(
            planned["query_plan"],
            planned["resolved_filters"],
        )
        candidates = retrieved["candidates"]
        ranked = (
            self.rank(query, candidates)
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
