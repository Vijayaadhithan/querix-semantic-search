import hashlib
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from search.bm25 import PersistentBM25Index
from storage.database import (
    create_database_pool,
    DatabaseRuntimeConfig,
    database_backend,
    database_source_name,
    fetch_product_types_by_ids,
    fetch_products_by_ids,
)
from providers.gemini import last_gemini_metrics
from providers.ollama import embed_text, last_ollama_embedding_metrics
from search.planner import (
    WANTED_AD_TYPE,
    build_query_filter_catalog,
    default_query_plan,
    deterministic_filter_query_plan,
    direct_semantic_query_plan,
    enrich_query_plan,
    extract_query_plan,
    query_analysis,
    query_filter_value_index,
    resolve_query_filters,
)
from search.retrieval import (
    bm25_search,
    extract_product_ids,
    filter_candidates_by_ad_type,
    merge_results,
    related_tail_product_ids,
    vector_search,
)
from search.engine_support import (
    QUERY_PLAN_CACHE_SCHEMA_VERSION,
    RESULT_CACHE_SCHEMA_VERSION,
    SearchEngineSupportMixin,
)
from search.helpers import active_filter_names
from search.policy import DEFAULT_SEARCH_POLICY, SearchPolicy
from search.ranking import SearchRankingMixin
from core.settings import (
    BM25_TOP_K,
    EMBED_MODEL,
    HYBRID_CANDIDATE_K,
    UNPRICED_RENTAL_FEE_CEILING,
    MYSQL_RESULT_ID_COLUMN,
    MYSQL_SEARCH_ID_COLUMN,
    MYSQL_TABLE,
    PRIMARY_RANKED_K,
    QUERY_DETERMINISTIC_FAST_PATH,
    QUERY_DIRECT_SEMANTIC_FAST_PATH,
    QUERY_EXTRACT_MODELS,
    QUERY_PLAN_CACHE_SIZE,
    QUERY_PLAN_CACHE_TTL_SECONDS,
    RELATED_TAIL_ENABLED,
    RESULT_CACHE_ENABLED,
    RESULT_CACHE_TTL_SECONDS,
    RERANK_CANDIDATE_K,
    RERANK_MAX_DOCUMENT_CHARS,
    RETRIEVAL_OVERFETCH_FACTOR,
    RERANK_PROVIDER_ORDER,
    RERANK_TOP_K,
    VECTOR_CANDIDATE_K,
    VECTOR_TOP_K,
    VOYAGE_RERANK_LITE_MODEL,
    VOYAGE_RERANK_MODEL,
)

LOGGER = logging.getLogger("uvicorn.error")


class ProductSearchEngine(SearchEngineSupportMixin, SearchRankingMixin):
    def __init__(
        self,
        collection=None,
        bm25_index=None,
        query_provider=None,
        embedding_provider=None,
        ranker=None,
        shared_plan_cache=None,
        company_id: str | None = None,
        mysql_config: DatabaseRuntimeConfig | None = None,
        shared_reranker=None,
        close_bm25_index: bool = False,
        planner_enabled: bool = True,
        direct_semantic_fast_path: bool = QUERY_DIRECT_SEMANTIC_FAST_PATH,
        planner_prompt_context: str = "",
        planner_query_aliases: dict[str, str] | None = None,
        vector_post_filter_metadata: bool | str = False,
        semantic_related_tail_enabled: bool = RELATED_TAIL_ENABLED,
        semantic_related_tail_requires_explicit_category: bool = False,
        reranker_relative_score_floor: float = 0.0,
        reranker_min_score_by_provider: dict[str, float] | None = None,
        search_policy: SearchPolicy = DEFAULT_SEARCH_POLICY,
    ):
        if collection is None:
            raise ValueError(
                "A tenant pgvector collection is required to build the search engine."
            )
        self.collection = collection
        self._owns_bm25_index = bm25_index is None or close_bm25_index
        self.bm25_index = bm25_index or PersistentBM25Index()
        self.query_provider = query_provider
        self.embedding_provider = embedding_provider
        self.ranker = ranker or getattr(shared_reranker, "ranker", None)
        self.shared_plan_cache = shared_plan_cache
        self.shared_reranker = shared_reranker
        self.company_id = company_id
        self.planner_enabled = planner_enabled
        self.direct_semantic_fast_path = direct_semantic_fast_path
        self.planner_prompt_context = planner_prompt_context
        self.planner_query_aliases = dict(planner_query_aliases or {})
        self.vector_post_filter_metadata = vector_post_filter_metadata
        self.semantic_related_tail_enabled = semantic_related_tail_enabled
        self.semantic_related_tail_requires_explicit_category = (
            semantic_related_tail_requires_explicit_category
        )
        self.reranker_relative_score_floor = (
            reranker_relative_score_floor
        )
        self.reranker_min_score_by_provider = {
            str(provider).casefold(): float(score)
            for provider, score in (
                reranker_min_score_by_provider or {}
            ).items()
        }
        self.search_policy = search_policy
        self.mysql_config = mysql_config
        self.database_pool = create_database_pool(mysql_config)
        self.source_type = database_backend(mysql_config)
        self.source_name = database_source_name(mysql_config)
        self.search_table = (
            mysql_config.search_table if mysql_config is not None else MYSQL_TABLE
        )
        self.search_id_column = (
            mysql_config.search_id_column
            if mysql_config is not None
            else MYSQL_SEARCH_ID_COLUMN
        )
        self.result_id_column = (
            mysql_config.result_id_column
            if mysql_config is not None
            else MYSQL_RESULT_ID_COLUMN
        )
        self.filter_value_index = query_filter_value_index(self.bm25_index)
        self.filter_catalog = build_query_filter_catalog(self.filter_value_index)
        self.planner_cache_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "aliases": self.planner_query_aliases,
                    "catalog": self.filter_catalog,
                    "filter_index": {
                        key: sorted(
                            value.items(),
                            key=lambda item: str(item[0]),
                        )
                        for key, value in self.filter_value_index.items()
                    },
                    "models": list(QUERY_EXTRACT_MODELS),
                    "planner_enabled": self.planner_enabled,
                    "direct_semantic_fast_path": (
                        self.direct_semantic_fast_path
                    ),
                    "prompt_context": self.planner_prompt_context,
                    "search_policy": self.search_policy.cache_key,
                    "schema": QUERY_PLAN_CACHE_SCHEMA_VERSION,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
        self._query_plan_cache: OrderedDict[str, tuple[float, dict]] = (
            OrderedDict()
        )
        self._plan_cache_lock = threading.RLock()
        self._embedding_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="query-embedding-prefetch",
        )

    def retrieve(
        self,
        query_plan: dict,
        resolved_filters: dict,
        candidate_limit: int | None = None,
        trace_id: str = "-",
        allowed_ad_types: set[str] | None = None,
        strict_candidate_limit: bool = False,
        query_embedding=None,
        embedding_prefetch_metrics: dict | None = None,
    ) -> dict:
        retrieval_started = time.perf_counter()
        expected_types = (
            {str(value) for value in allowed_ad_types}
            if allowed_ad_types is not None
            else {
                WANTED_AD_TYPE
                if query_plan.get("target_ad_type") == "wanted"
                else "1"
            }
        )
        include_unpriced = expected_types == {WANTED_AD_TYPE}
        requested = candidate_limit or RERANK_TOP_K
        # The hosted reranker payload can stay small without shrinking
        # retrieval recall. Tenant policies run over the deeper fused pool
        # before the bounded reranker window is selected.
        recall_window = max(HYBRID_CANDIDATE_K, requested)
        extended_window = (
            candidate_limit is not None and recall_window > RERANK_TOP_K
        )
        retrieval_depth = (
            max(
                VECTOR_TOP_K,
                BM25_TOP_K,
                recall_window * RETRIEVAL_OVERFETCH_FACTOR,
            )
            if extended_window
            else None
        )
        vector_top_k = retrieval_depth or VECTOR_TOP_K
        bm25_top_k = retrieval_depth or BM25_TOP_K
        hybrid_top_k = (
            requested
            if strict_candidate_limit
            else recall_window
        )
        vector_candidate_k = max(VECTOR_CANDIDATE_K, vector_top_k)
        LOGGER.debug(
            "[search:%s] step=retrieve status=start embedding_model=%s "
            "vector_k=%d bm25_k=%d hybrid_k=%d filters=%s",
            trace_id,
            EMBED_MODEL,
            vector_top_k,
            bm25_top_k,
            hybrid_top_k,
            ",".join(active_filter_names(resolved_filters)) or "none",
        )

        def run_vector() -> tuple[list[dict], float, dict]:
            started = time.perf_counter()
            vector_metrics = {}
            results = vector_search(
                query_plan["semantic_query"],
                self.collection,
                vector_top_k,
                candidate_k=vector_candidate_k,
                source_name=self.source_name,
                resolved_filters=resolved_filters,
                embedding_provider=self.embedding_provider,
                company_id=self.company_id,
                post_filter_metadata=self.vector_post_filter_metadata,
                include_unpriced=include_unpriced,
                metrics=vector_metrics,
                query_embedding=query_embedding,
            )
            if query_embedding is not None:
                vector_metrics["embedding_prefetch_ms"] = float(
                    (embedding_prefetch_metrics or {}).get(
                        "prefetch_total_ms",
                        0.0,
                    )
                )
                vector_metrics["embedding_prefetch_wait_ms"] = float(
                    (embedding_prefetch_metrics or {}).get(
                        "prefetch_wait_ms",
                        0.0,
                    )
                )
            return results, time.perf_counter() - started, vector_metrics

        def run_bm25() -> tuple[list[dict], float]:
            started = time.perf_counter()
            results = bm25_search(
                query_plan["keyword_query"],
                self.bm25_index,
                resolved_filters,
                bm25_top_k,
                include_unpriced=include_unpriced,
                source_name=self.source_name,
                company_id=self.company_id,
                source_type=self.source_type,
                search_table=self.search_table,
                search_id_column=self.search_id_column,
            )
            return results, time.perf_counter() - started

        with ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="hybrid-retrieval",
        ) as executor:
            vector_future = executor.submit(run_vector)
            bm25_future = executor.submit(run_bm25)
            retrieval_errors = []
            try:
                (
                    vector_results,
                    vector_seconds,
                    vector_metrics,
                ) = vector_future.result()
            except Exception as exc:
                vector_results, vector_seconds = [], 0.0
                vector_metrics = {}
                retrieval_errors.append(("vector", exc))
                LOGGER.exception(
                    "[search:%s] step=vector status=degraded error_type=%s",
                    trace_id,
                    type(exc).__name__,
                )

            try:
                bm25_results, bm25_seconds = bm25_future.result()
            except Exception as exc:
                bm25_results, bm25_seconds = [], 0.0
                retrieval_errors.append(("bm25", exc))
                LOGGER.exception(
                    "[search:%s] step=bm25 status=degraded error_type=%s",
                    trace_id,
                    type(exc).__name__,
                )

        parallel_retrieval_ms = (
            time.perf_counter() - retrieval_started
        ) * 1000

        if len(retrieval_errors) == 2:
            stages = ", ".join(stage for stage, _exc in retrieval_errors)
            raise RuntimeError(f"All retrieval paths failed: {stages}") from (
                retrieval_errors[0][1]
            )

        fusion_started = time.perf_counter()
        merged = merge_results(
            vector_results,
            bm25_results,
            query_plan.get("inferred_categories"),
        )
        merged = self.search_policy.adjust_candidates(
            query_plan,
            merged,
        )
        fusion_ms = (time.perf_counter() - fusion_started) * 1000
        # Apply ad intent before truncating the candidate window. Otherwise a
        # page can be short merely because unwanted ad types occupied the K
        # slots ahead of valid products.
        type_lookup_started = time.perf_counter()
        eligible_candidates = filter_candidates_by_ad_type(
            merged,
            query_plan["target_ad_type"],
            type_fetcher=self._fetch_product_types,
            search_table=self.search_table,
            search_id_column=self.search_id_column,
            allowed_ad_types=allowed_ad_types,
        )
        type_lookup_ms = (
            time.perf_counter() - type_lookup_started
        ) * 1000
        candidates = eligible_candidates[:hybrid_top_k]
        # Keep the rest of the fused pool for later pages without increasing
        # the hosted reranker payload. These candidates remain in reciprocal-
        # rank-fusion order and have passed the same hard filters and ad-type
        # validation as the reranked window.
        hybrid_tail_candidates = eligible_candidates[hybrid_top_k:]
        embedding_metrics = dict(embedding_prefetch_metrics or {})
        if (
            not embedding_metrics
            and self.embedding_provider is None
        ):
            embedding_metrics = last_ollama_embedding_metrics()
        retrieval_total_ms = (
            time.perf_counter() - retrieval_started
        ) * 1000
        LOGGER.info(
            "[search:%s] step=retrieve status=complete vector=%d bm25=%d "
            "merged=%d candidates=%d hybrid_tail=%d vector_ms=%.0f bm25_ms=%.0f "
            "vector_count_ms=%.0f vector_embed_ms=%.0f vector_db_ms=%.0f "
            "vector_filter_ms=%.0f vector_strategy=%s vector_eligible=%s "
            "vector_eligible_capped=%s "
            "vector_query_mode=%s vector_shadow_equal=%s "
            "vector_shadow_error=%s "
            "vector_shadow_ms=%.0f vector_optimized_ms=%.0f "
            "parallel_ms=%.0f fusion_ms=%.0f type_lookup_ms=%.0f "
            "retrieval_total_ms=%.0f "
            "embed_total_ms=%.0f embed_load_ms=%.0f "
            "embed_prefetch_reused=%s embed_prefetch_ms=%.0f",
            trace_id,
            len(vector_results),
            len(bm25_results),
            len(merged),
            len(candidates),
            len(hybrid_tail_candidates),
            vector_seconds * 1000,
            bm25_seconds * 1000,
            vector_metrics.get("count_ms", 0.0),
            vector_metrics.get("embedding_ms", 0.0),
            vector_metrics.get("database_ms", 0.0),
            vector_metrics.get("post_filter_ms", 0.0),
            vector_metrics.get("strategy", "unknown"),
            vector_metrics.get("eligible_rows", "unknown"),
            vector_metrics.get("eligible_rows_capped", False),
            vector_metrics.get("query_mode", "legacy"),
            vector_metrics.get("shadow_equal", "not_run"),
            vector_metrics.get("shadow_error", "none"),
            vector_metrics.get("shadow_ms", 0.0),
            vector_metrics.get("optimized_ms", 0.0),
            parallel_retrieval_ms,
            fusion_ms,
            type_lookup_ms,
            retrieval_total_ms,
            embedding_metrics.get("total_ms", 0.0),
            embedding_metrics.get("load_ms", 0.0),
            vector_metrics.get("embedding_prefetch_reused", False),
            vector_metrics.get("embedding_prefetch_ms", 0.0),
        )
        return {
            "vector_results": vector_results,
            "bm25_results": bm25_results,
            "candidates": candidates,
            "hybrid_tail_candidates": hybrid_tail_candidates,
            "vector_seconds": vector_seconds,
            "bm25_seconds": bm25_seconds,
            "retrieval_seconds": retrieval_total_ms / 1000,
            "parallel_retrieval_seconds": parallel_retrieval_ms / 1000,
            "fusion_seconds": fusion_ms / 1000,
            "type_lookup_seconds": type_lookup_ms / 1000,
            "vector_query_metrics": dict(vector_metrics),
            "embedding_model_metrics": embedding_metrics,
            "retrieval_degraded": bool(retrieval_errors),
            "retrieval_error_type": (
                type(retrieval_errors[0][1]).__name__
                if retrieval_errors
                else None
            ),
            "degraded_stages": [stage for stage, _exc in retrieval_errors],
        }


    def _semantic_related_tail_allowed(self, resolved_filters: dict) -> bool:
        if not self.semantic_related_tail_enabled:
            return False
        if not self.semantic_related_tail_requires_explicit_category:
            return True
        categorical = resolved_filters.get("categorical", {})
        return any(
            key in categorical
            for key in ("main_category_name", "subcategory_name")
        )

    def _filtered_search(
        self,
        planned: dict,
        limit: int | None,
        trace_id: str,
        search_started: float,
        allowed_ad_types: set[str] | None = None,
    ) -> dict:
        result_limit = limit if limit is not None else RERANK_TOP_K
        browse_started = time.perf_counter()
        product_ids = related_tail_product_ids(
            self.bm25_index,
            planned["resolved_filters"],
            planned["query_plan"].get("inferred_categories"),
            planned["query_plan"]["target_ad_type"],
            result_limit,
            type_fetcher=self._fetch_product_types,
            sort_order=planned["query_plan"].get("sort_order"),
            allowed_ad_types=allowed_ad_types,
        )
        browse_seconds = time.perf_counter() - browse_started
        LOGGER.info(
            "[search:%s] step=fast_filter status=complete filters=%s "
            "products=%d duration_ms=%.0f",
            trace_id,
            ",".join(
                active_filter_names(planned["resolved_filters"])
            ) or "none",
            len(product_ids),
            browse_seconds * 1000,
        )
        products = [
            {**product, "result_tier": "filtered"}
            for product in self._fetch_products(product_ids)
        ]
        LOGGER.info(
            "[search:%s] step=search status=complete path=deterministic_filter "
            "products=%d duration_ms=%.0f",
            trace_id,
            len(products),
            (time.perf_counter() - search_started) * 1000,
        )
        return {
            **planned,
            "vector_results": [],
            "bm25_results": [],
            "candidates": [],
            "hybrid_tail_candidates": [],
            "vector_seconds": 0.0,
            "bm25_seconds": 0.0,
            "embedding_model_metrics": {},
            "reranked": [],
            "reranker_load_seconds": 0.0,
            "reranker_seconds": 0.0,
            "reranker_provider": "none",
            "reranker_attempts": [],
            "related_tail_seconds": browse_seconds,
            "primary_product_ids": [],
            "hybrid_product_ids": [],
            "related_product_ids": product_ids,
            "product_ids": product_ids,
            "products": products,
        }

    def search(
        self,
        query: str,
        limit: int | None = None,
        *,
        planned_result: dict | None = None,
        resolved_filters: dict | None = None,
        allowed_ad_types: set[str] | None = None,
        ranking_window: int | None = None,
        hydrate_products: bool = True,
        speculative_embedding_future=None,
    ) -> dict:
        if limit is not None and limit <= 0:
            raise ValueError("Search limit must be greater than zero.")
        if ranking_window is not None and ranking_window <= 0:
            raise ValueError("Ranking window must be greater than zero.")
        configured_primary_limit = (
            min(PRIMARY_RANKED_K, ranking_window)
            if ranking_window is not None
            else PRIMARY_RANKED_K
        )
        primary_limit = (
            min(configured_primary_limit, limit)
            if limit is not None
            else RERANK_TOP_K
        )
        configured_candidate_limit = (
            min(RERANK_CANDIDATE_K, ranking_window)
            if ranking_window is not None
            else RERANK_CANDIDATE_K
        )
        rerank_candidate_limit = (
            max(primary_limit, configured_candidate_limit)
            if limit is not None
            else None
        )
        trace_id = uuid.uuid4().hex[:8]
        if self.company_id:
            trace_id = f"{self.company_id}:{trace_id}"
        started = time.perf_counter()
        LOGGER.debug(
            "[search:%s] step=search status=start query_chars=%d limit=%s",
            trace_id,
            len(query),
            limit if limit is not None else "default",
        )
        cached_result = self._cached_search_result(
            query,
            limit,
            trace_id,
            resolved_filters,
            allowed_ad_types,
            ranking_window,
            hydrate_products,
        )
        if cached_result is not None:
            cached_result["trace_id"] = trace_id
            LOGGER.info(
                "[search:%s] step=search status=complete "
                "path=result_cache products=%d duration_ms=%.0f",
                trace_id,
                len(cached_result["products"]),
                (time.perf_counter() - started) * 1000,
            )
            return cached_result
        if (
            speculative_embedding_future is None
            and planned_result is None
        ):
            speculative_embedding_future = (
                self.start_speculative_embedding(query)
            )
        planned = (
            deepcopy(planned_result)
            if planned_result is not None
            else self.plan(query, trace_id=trace_id)
        )
        if resolved_filters is not None:
            planned["resolved_filters"] = deepcopy(resolved_filters)
        if (
            planned["query_plan"].get("execution_path")
            == "deterministic_filter"
        ):
            if speculative_embedding_future is not None:
                speculative_embedding_future.cancel()
            result = self._filtered_search(
                planned,
                limit,
                trace_id,
                started,
                allowed_ad_types,
            )
            result["result_cache_hit"] = False
            result["result_cache_seconds"] = 0.0
            result["trace_id"] = trace_id
            self._cache_search_result(
                query,
                limit,
                result,
                resolved_filters,
                allowed_ad_types,
                ranking_window,
            )
            return result
        prefetched_embedding = None
        prefetch_metrics = {}
        semantic_query = planned["query_plan"]["semantic_query"]
        if (
            speculative_embedding_future is not None
            and semantic_query == query
        ):
            prefetch_wait_started = time.perf_counter()
            try:
                prefetched = speculative_embedding_future.result()
                if prefetched.get("query") == semantic_query:
                    prefetched_embedding = prefetched["embedding"]
                    prefetch_metrics = dict(
                        prefetched.get("metrics") or {}
                    )
                    prefetch_metrics["prefetch_total_ms"] = (
                        float(prefetched.get("seconds", 0.0)) * 1000
                    )
                    prefetch_metrics["prefetch_wait_ms"] = (
                        time.perf_counter() - prefetch_wait_started
                    ) * 1000
            except Exception as exc:
                LOGGER.warning(
                    "[search:%s] step=embedding_prefetch "
                    "status=degraded error_type=%s",
                    trace_id,
                    type(exc).__name__,
                )
        elif speculative_embedding_future is not None:
            speculative_embedding_future.cancel()
        retrieved = self.retrieve(
            planned["query_plan"],
            planned["resolved_filters"],
            candidate_limit=rerank_candidate_limit,
            trace_id=trace_id,
            allowed_ad_types=allowed_ad_types,
            strict_candidate_limit=ranking_window is not None,
            query_embedding=prefetched_embedding,
            embedding_prefetch_metrics=prefetch_metrics,
        )
        candidates = retrieved["candidates"]
        if candidates:
            try:
                ranked = self.rank(
                    query,
                    candidates,
                    planned["query_plan"],
                    top_k=primary_limit,
                    trace_id=trace_id,
                )
            except Exception as exc:
                LOGGER.exception(
                    "[search:%s] step=rerank status=failed "
                    "error_type=%s candidates=%d",
                    trace_id,
                    type(exc).__name__,
                    len(candidates),
                )
                raise
        else:
            LOGGER.info(
                "[search:%s] step=rerank status=skipped reason=no_candidates",
                trace_id,
            )
            ranked = {
                "results": [],
                "load_seconds": 0.0,
                "seconds": 0.0,
                "provider": "none",
                "attempts": [],
            }
        primary_product_ids = extract_product_ids(
            ranked["results"],
            search_table=self.search_table,
            search_id_column=self.search_id_column,
        )
        tail_limit = (
            max(limit - len(primary_product_ids), 0)
            if limit is not None
            else 0
        )
        tail_started = time.perf_counter()
        hybrid_product_ids = []
        related_product_ids = []
        tail_allowed = self._semantic_related_tail_allowed(
            planned["resolved_filters"]
        )
        if tail_allowed and tail_limit:
            primary_identities = {
                str(product_id)
                for product_id in primary_product_ids
            }
            hybrid_product_ids = [
                product_id
                for product_id in extract_product_ids(
                    retrieved.get("hybrid_tail_candidates", []),
                    search_table=self.search_table,
                    search_id_column=self.search_id_column,
                )
                if str(product_id) not in primary_identities
            ][:tail_limit]
        catalogue_tail_limit = max(
            tail_limit - len(hybrid_product_ids),
            0,
        )
        if tail_allowed and catalogue_tail_limit:
            excluded_candidates = (
                candidates
                + retrieved.get("hybrid_tail_candidates", [])
            )
            related_product_ids = related_tail_product_ids(
                self.bm25_index,
                planned["resolved_filters"],
                planned["query_plan"].get("inferred_categories"),
                planned["query_plan"]["target_ad_type"],
                catalogue_tail_limit,
                exclude_doc_ids={
                    result["id"]
                    for result in excluded_candidates
                },
                exclude_product_ids=set(
                    (*primary_product_ids, *hybrid_product_ids)
                ),
                type_fetcher=self._fetch_product_types,
                sort_order=planned["query_plan"].get("sort_order"),
                allowed_ad_types=allowed_ad_types,
            )
        related_tail_seconds = time.perf_counter() - tail_started
        product_ids = list(
            dict.fromkeys(
                (
                    *primary_product_ids,
                    *hybrid_product_ids,
                    *related_product_ids,
                )
            )
        )
        LOGGER.debug(
            "[search:%s] step=related_tail status=complete filters=%s "
            "primary=%d hybrid=%d related=%d duration_ms=%.0f",
            trace_id,
            ",".join(
                active_filter_names(planned["resolved_filters"])
            ) or "none",
            len(primary_product_ids),
            len(hybrid_product_ids),
            len(related_product_ids),
            related_tail_seconds * 1000,
        )
        LOGGER.debug(
            "[search:%s] step=database_map status=start product_ids=%d",
            trace_id,
            len(product_ids),
        )
        products = self._fetch_products(product_ids) if hydrate_products else []
        primary_identities = {
            str(product_id)
            for product_id in primary_product_ids
        }
        products = [
            {
                **product,
                "result_tier": (
                    "ranked"
                    if str(product.get(self.result_id_column))
                    in primary_identities
                    else "related"
                ),
            }
            for product in products
        ]
        sort_order = planned["query_plan"].get("sort_order")
        if sort_order in {"price_asc", "price_desc"} and hydrate_products:
            descending = sort_order == "price_desc"

            def price_key(product):
                try:
                    price = float(product.get("rental_fee"))
                except (TypeError, ValueError):
                    return (1, 0.0)
                if price <= UNPRICED_RENTAL_FEE_CEILING:
                    return (1, price)
                return (0, -price if descending else price)

            products.sort(key=price_key)
            product_ids = [
                product[self.result_id_column]
                for product in products
                if product.get(self.result_id_column) is not None
            ]
        LOGGER.debug(
            "[search:%s] step=database_map status=complete rows=%d",
            trace_id,
            len(products),
        )
        LOGGER.info(
            "[search:%s] step=search status=complete ranked_ids=%d "
            "products=%d hydration=%s duration_ms=%.0f",
            trace_id,
            len(product_ids),
            len(products),
            "complete" if hydrate_products else "deferred",
            (time.perf_counter() - started) * 1000,
        )
        result = {
            **planned,
            **retrieved,
            "trace_id": trace_id,
            "reranked": ranked["results"],
            "reranker_load_seconds": ranked["load_seconds"],
            "reranker_seconds": ranked["seconds"],
            "reranker_provider": ranked["provider"],
            "reranker_attempts": ranked["attempts"],
            "reranker_degraded": bool(ranked.get("degraded")),
            "reranker_error_type": ranked.get("error_type"),
            "related_tail_seconds": related_tail_seconds,
            "primary_product_ids": primary_product_ids,
            "hybrid_product_ids": hybrid_product_ids,
            "related_product_ids": related_product_ids,
            "product_ids": product_ids,
            "products": products,
            "result_cache_hit": False,
            "result_cache_seconds": 0.0,
        }
        self._cache_search_result(
            query,
            limit,
            result,
            resolved_filters,
            allowed_ad_types,
            ranking_window,
        )
        return result
