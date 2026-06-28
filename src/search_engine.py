import logging
import time
import uuid

from bm25_index import PersistentBM25Index
from gemini_client import last_gemini_metrics
from mysql_store import fetch_products_by_ids, mysql_source_name
from ollama_client import last_ollama_embedding_metrics
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
    related_tail_product_ids,
    vector_search,
)
from settings import (
    BM25_TOP_K,
    EMBED_MODEL,
    HYBRID_CANDIDATE_K,
    MYSQL_RESULT_ID_COLUMN,
    PRIMARY_RANKED_K,
    QUERY_EXTRACT_MODELS,
    RELATED_TAIL_ENABLED,
    RERANK_CANDIDATE_K,
    RETRIEVAL_OVERFETCH_FACTOR,
    RERANK_MODEL,
    RERANK_TOP_K,
    VECTOR_CANDIDATE_K,
    VECTOR_TOP_K,
)

LOGGER = logging.getLogger("uvicorn.error")


def active_filter_names(filters: dict) -> list[str]:
    names = []
    for key, value in filters.items():
        if isinstance(value, dict):
            names.extend(
                f"{key}.{child_key}"
                for child_key, child_value in value.items()
                if child_value not in (None, "", [], {})
            )
        elif value not in (None, "", [], {}):
            names.append(key)
    return names


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

    def plan(self, query: str, trace_id: str = "-") -> dict:
        started = time.perf_counter()
        LOGGER.info(
            "[search:%s] step=plan status=start query_chars=%d models=%s",
            trace_id,
            len(query),
            " -> ".join(QUERY_EXTRACT_MODELS),
        )
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
        query_metrics = (
            last_gemini_metrics()
            if self.query_provider is None
            else {}
        )
        elapsed = time.perf_counter() - started
        log_method = (
            LOGGER.warning
            if query_plan.get("fallback_reason")
            else LOGGER.info
        )
        log_method(
            "[search:%s] step=plan status=%s model=%s attempted=%s "
            "filters=%s unresolved=%d duration_ms=%.0f",
            trace_id,
            (
                "deterministic_fallback"
                if query_plan.get("fallback_reason")
                else "complete"
            ),
            query_metrics.get("model", type(self.query_provider).__name__),
            ",".join(query_metrics.get("attempted_models", [])) or "custom",
            ",".join(active_filter_names(query_plan["filters"])) or "none",
            len(unresolved),
            elapsed * 1000,
        )
        return {
            "query_plan": query_plan,
            "resolved_filters": resolved,
            "unresolved_filters": unresolved,
            "query_model_metrics": query_metrics,
            "seconds": elapsed,
        }

    def retrieve(
        self,
        query_plan: dict,
        resolved_filters: dict,
        candidate_limit: int | None = None,
        trace_id: str = "-",
    ) -> dict:
        extended_window = (
            candidate_limit is not None and candidate_limit > RERANK_TOP_K
        )
        requested = candidate_limit or RERANK_TOP_K
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
        LOGGER.info(
            "[search:%s] step=retrieve status=start embedding_model=%s "
            "vector_k=%d bm25_k=%d hybrid_k=%d filters=%s",
            trace_id,
            EMBED_MODEL,
            vector_top_k,
            bm25_top_k,
            hybrid_top_k,
            ",".join(active_filter_names(resolved_filters)) or "none",
        )

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

        merged = merge_results(
            vector_results,
            bm25_results,
            query_plan.get("inferred_categories"),
        )
        # Apply ad intent before truncating the candidate window. Otherwise a
        # page can be short merely because unwanted ad types occupied the K
        # slots ahead of valid products.
        candidates = filter_candidates_by_ad_type(
            merged,
            query_plan["target_ad_type"],
        )[:hybrid_top_k]
        LOGGER.info(
            "[search:%s] step=retrieve status=complete vector=%d bm25=%d "
            "merged=%d candidates=%d vector_ms=%.0f bm25_ms=%.0f",
            trace_id,
            len(vector_results),
            len(bm25_results),
            len(merged),
            len(candidates),
            vector_seconds * 1000,
            bm25_seconds * 1000,
        )
        return {
            "vector_results": vector_results,
            "bm25_results": bm25_results,
            "candidates": candidates,
            "vector_seconds": vector_seconds,
            "bm25_seconds": bm25_seconds,
            "embedding_model_metrics": (
                last_ollama_embedding_metrics()
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
        trace_id: str = "-",
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
        LOGGER.info(
            "[search:%s] step=rerank status=start model=%s candidates=%d top_k=%d",
            trace_id,
            RERANK_MODEL,
            len(candidates),
            RERANK_TOP_K if top_k is None else top_k,
        )
        results = rerank(
            ranking_query,
            candidates,
            self.ranker,
            RERANK_TOP_K if top_k is None else top_k,
            diversity_top_k=RERANK_TOP_K,
        )
        elapsed = time.perf_counter() - started
        LOGGER.info(
            "[search:%s] step=rerank status=complete results=%d "
            "load_ms=%.0f duration_ms=%.0f",
            trace_id,
            len(results),
            load_seconds * 1000,
            elapsed * 1000,
        )
        return {
            "results": results,
            "load_seconds": load_seconds,
            "seconds": elapsed,
        }

    def search(self, query: str, limit: int | None = None) -> dict:
        if limit is not None and limit <= 0:
            raise ValueError("Search limit must be greater than zero.")
        primary_limit = (
            min(PRIMARY_RANKED_K, limit)
            if limit is not None
            else RERANK_TOP_K
        )
        rerank_candidate_limit = (
            max(primary_limit, RERANK_CANDIDATE_K)
            if limit is not None
            else None
        )
        trace_id = uuid.uuid4().hex[:8]
        started = time.perf_counter()
        LOGGER.info(
            "[search:%s] step=search status=start query_chars=%d limit=%s",
            trace_id,
            len(query),
            limit if limit is not None else "default",
        )
        planned = self.plan(query, trace_id=trace_id)
        retrieved = self.retrieve(
            planned["query_plan"],
            planned["resolved_filters"],
            candidate_limit=rerank_candidate_limit,
            trace_id=trace_id,
        )
        candidates = retrieved["candidates"]
        if candidates:
            ranked = self.rank(
                query,
                candidates,
                planned["query_plan"],
                top_k=primary_limit,
                trace_id=trace_id,
            )
        else:
            LOGGER.info(
                "[search:%s] step=rerank status=skipped reason=no_candidates",
                trace_id,
            )
            ranked = {"results": [], "load_seconds": 0.0, "seconds": 0.0}
        primary_product_ids = extract_product_ids(ranked["results"])
        tail_limit = (
            max(limit - len(primary_product_ids), 0)
            if limit is not None
            else 0
        )
        tail_started = time.perf_counter()
        related_product_ids = []
        if RELATED_TAIL_ENABLED and tail_limit:
            related_product_ids = related_tail_product_ids(
                self.bm25_index,
                planned["resolved_filters"],
                planned["query_plan"].get("inferred_categories"),
                planned["query_plan"]["target_ad_type"],
                tail_limit,
                exclude_doc_ids={
                    result["id"]
                    for result in ranked["results"]
                },
                exclude_product_ids=set(primary_product_ids),
            )
        related_tail_seconds = time.perf_counter() - tail_started
        product_ids = list(
            dict.fromkeys((*primary_product_ids, *related_product_ids))
        )
        LOGGER.info(
            "[search:%s] step=related_tail status=complete filters=%s "
            "primary=%d related=%d duration_ms=%.0f",
            trace_id,
            ",".join(
                active_filter_names(planned["resolved_filters"])
            ) or "none",
            len(primary_product_ids),
            len(related_product_ids),
            related_tail_seconds * 1000,
        )
        LOGGER.info(
            "[search:%s] step=mysql_map status=start product_ids=%d",
            trace_id,
            len(product_ids),
        )
        products = fetch_products_by_ids(product_ids)
        primary_identities = {
            str(product_id)
            for product_id in primary_product_ids
        }
        products = [
            {
                **product,
                "result_tier": (
                    "ranked"
                    if str(product.get(MYSQL_RESULT_ID_COLUMN))
                    in primary_identities
                    else "related"
                ),
            }
            for product in products
        ]
        LOGGER.info(
            "[search:%s] step=mysql_map status=complete rows=%d",
            trace_id,
            len(products),
        )
        LOGGER.info(
            "[search:%s] step=search status=complete products=%d duration_ms=%.0f",
            trace_id,
            len(products),
            (time.perf_counter() - started) * 1000,
        )
        return {
            **planned,
            **retrieved,
            "reranked": ranked["results"],
            "reranker_load_seconds": ranked["load_seconds"],
            "reranker_seconds": ranked["seconds"],
            "related_tail_seconds": related_tail_seconds,
            "primary_product_ids": primary_product_ids,
            "related_product_ids": related_product_ids,
            "product_ids": product_ids,
            "products": products,
        }
