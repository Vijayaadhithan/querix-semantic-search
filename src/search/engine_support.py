import hashlib
import json
import logging
import sys
import threading
import time
from concurrent.futures import Future
from copy import deepcopy

from core.settings import (
    HYBRID_CANDIDATE_K,
    PRIMARY_RANKED_K,
    QUERY_DETERMINISTIC_FAST_PATH,
    QUERY_EXTRACT_MODELS,
    QUERY_PLAN_CACHE_SIZE,
    QUERY_PLAN_CACHE_TTL_SECONDS,
    RERANK_CANDIDATE_K,
    RERANK_MAX_DOCUMENT_CHARS,
    RERANK_MODEL,
    RERANK_PROVIDER_ORDER,
    RESULT_CACHE_ENABLED,
    RESULT_CACHE_TTL_SECONDS,
    VOYAGE_RERANK_LITE_MODEL,
    VOYAGE_RERANK_MODEL,
)
from providers.gemini import last_gemini_metrics
from providers.ollama import embed_text, last_ollama_embedding_metrics
from search.helpers import active_filter_names
from search.planner import (
    default_query_plan,
    deterministic_filter_query_plan,
    direct_semantic_query_plan,
    enrich_query_plan,
    extract_query_plan,
    query_analysis,
    resolve_query_filters,
)
from storage.database import fetch_product_types_by_ids, fetch_products_by_ids

LOGGER = logging.getLogger("uvicorn.error")
RESULT_CACHE_SCHEMA_VERSION = "v27"
QUERY_PLAN_CACHE_SCHEMA_VERSION = "v18"
SPECULATIVE_EMBEDDING_DELAY_SECONDS = 0.025


def _engine_dependency(name: str, default):
    """Honor patches made through the public ``search.engine`` module."""
    engine_module = sys.modules.get("search.engine")
    return (
        getattr(engine_module, name, default) if engine_module is not None else default
    )


class SearchEngineSupportMixin:
    def close(self) -> None:
        self._embedding_executor.shutdown(
            wait=False,
            cancel_futures=True,
        )
        if self.database_pool is not None:
            self.database_pool.close()
        if self._owns_bm25_index:
            self.bm25_index.close()

    def _query_cache_key(self, query: str) -> str:
        normalized = " ".join(query.casefold().split())
        return f"{self.planner_cache_fingerprint}:{normalized}"

    def set_shared_plan_cache(self, cache) -> None:
        self.shared_plan_cache = cache

    def start_speculative_embedding(self, query: str):
        """Start the original embedding unless a fast path cancels it first."""
        if not self.planner_enabled or not query.strip():
            return None

        def run():
            started = time.perf_counter()
            embedding = (
                self.embedding_provider.embed_text(query)
                if self.embedding_provider is not None
                else _engine_dependency("embed_text", embed_text)(query)
            )
            metrics = (
                {}
                if self.embedding_provider is not None
                else _engine_dependency(
                    "last_ollama_embedding_metrics",
                    last_ollama_embedding_metrics,
                )()
            )
            return {
                "query": query,
                "embedding": embedding,
                "metrics": metrics,
                "seconds": time.perf_counter() - started,
            }

        # Deterministic and direct-semantic planning normally resolves in less
        # than 25 ms.  Give those paths a short cancellation window so a
        # deterministic request does not consume local embedding capacity it
        # will never use.  Full semantic planning still overlaps almost all of
        # the embedding work with the hosted planner call.
        result_future = Future()

        def transfer_result(inner_future):
            try:
                result_future.set_result(inner_future.result())
            except BaseException as exc:
                result_future.set_exception(exc)

        def launch():
            if not result_future.set_running_or_notify_cancel():
                return
            try:
                inner_future = self._embedding_executor.submit(run)
            except BaseException as exc:
                result_future.set_exception(exc)
                return
            inner_future.add_done_callback(transfer_result)

        timer = threading.Timer(
            SPECULATIVE_EMBEDDING_DELAY_SECONDS,
            launch,
        )
        timer.daemon = True
        timer.start()
        return result_future

    def _cache_namespace(self, name: str) -> str:
        return f"{self.company_id}:{name}" if self.company_id else name

    def _fetch_products(self, product_ids) -> list[dict]:
        if self.mysql_config is None:
            return _engine_dependency(
                "fetch_products_by_ids",
                fetch_products_by_ids,
            )(product_ids)
        if self.database_pool is not None:
            with self.database_pool.connection() as connection:
                return _engine_dependency(
                    "fetch_products_by_ids",
                    fetch_products_by_ids,
                )(
                    product_ids,
                    connection=connection,
                    config=self.mysql_config,
                )
        return _engine_dependency(
            "fetch_products_by_ids",
            fetch_products_by_ids,
        )(product_ids, config=self.mysql_config)

    def _fetch_product_types(self, product_ids) -> dict[str, str]:
        if self.mysql_config is None:
            return _engine_dependency(
                "fetch_product_types_by_ids",
                fetch_product_types_by_ids,
            )(product_ids)
        if self.database_pool is not None:
            with self.database_pool.connection() as connection:
                return _engine_dependency(
                    "fetch_product_types_by_ids",
                    fetch_product_types_by_ids,
                )(
                    product_ids,
                    connection=connection,
                    config=self.mysql_config,
                )
        return _engine_dependency(
            "fetch_product_types_by_ids",
            fetch_product_types_by_ids,
        )(
            product_ids,
            config=self.mysql_config,
        )

    def plan_cache_health(self) -> dict:
        if self.shared_plan_cache is None:
            return {
                "redis_enabled": False,
                "redis_connected": False,
                "query_plan_cache_backend": "memory",
                "result_cache_enabled": False,
                "result_cache_ttl_seconds": RESULT_CACHE_TTL_SECONDS,
            }
        return {
            "redis_enabled": True,
            "redis_connected": bool(
                getattr(self.shared_plan_cache, "connected", False)
            ),
            "query_plan_cache_backend": (
                "redis+memory"
                if getattr(self.shared_plan_cache, "connected", False)
                else "memory_fallback"
            ),
            "result_cache_enabled": RESULT_CACHE_ENABLED,
            "result_cache_ttl_seconds": RESULT_CACHE_TTL_SECONDS,
        }

    def _result_cache_key(
        self,
        query: str,
        limit: int | None,
        resolved_filters: dict | None = None,
        allowed_ad_types: set[str] | None = None,
        ranking_window: int | None = None,
    ) -> str:
        version_parts = (
            RESULT_CACHE_SCHEMA_VERSION,
            self.company_id or "legacy",
            str(self.bm25_index.revision()),
            str(self.bm25_index.count()),
            str(limit),
            str(ranking_window),
            RERANK_MODEL,
            ",".join(RERANK_PROVIDER_ORDER),
            VOYAGE_RERANK_MODEL,
            VOYAGE_RERANK_LITE_MODEL,
            str(RERANK_MAX_DOCUMENT_CHARS),
            str(HYBRID_CANDIDATE_K),
            str(RERANK_CANDIDATE_K),
            str(PRIMARY_RANKED_K),
            json.dumps(
                resolved_filters,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            if resolved_filters is not None
            else "",
            ",".join(sorted(allowed_ad_types or set())),
            self._query_cache_key(query),
        )
        return hashlib.sha256("\0".join(version_parts).encode()).hexdigest()

    def _cached_search_result(
        self,
        query: str,
        limit: int | None,
        trace_id: str,
        resolved_filters: dict | None = None,
        allowed_ad_types: set[str] | None = None,
        ranking_window: int | None = None,
        hydrate_products: bool = True,
    ) -> dict | None:
        if not RESULT_CACHE_ENABLED or self.shared_plan_cache is None:
            return None
        started = time.perf_counter()
        cache_key = self._result_cache_key(
            query,
            limit,
            resolved_filters,
            allowed_ad_types,
            ranking_window,
        )
        cached = self.shared_plan_cache.get_json(
            self._cache_namespace("search_result"),
            cache_key,
        )
        if cached is None:
            LOGGER.debug(
                "[search:%s] step=result_cache status=miss duration_ms=%.0f",
                trace_id,
                (time.perf_counter() - started) * 1000,
            )
            return None
        required = {
            "query_plan",
            "resolved_filters",
            "unresolved_filters",
            "product_ids",
            "primary_product_ids",
            "related_product_ids",
        }
        if not required.issubset(cached) or not isinstance(
            cached["product_ids"],
            list,
        ):
            LOGGER.warning(
                "[search:%s] step=result_cache status=invalid",
                trace_id,
            )
            return None

        database_started = time.perf_counter()
        products = (
            self._fetch_products(cached["product_ids"]) if hydrate_products else []
        )
        primary_identities = {
            str(product_id) for product_id in cached["primary_product_ids"]
        }
        deterministic = (
            cached["query_plan"].get("execution_path") == "deterministic_filter"
        )
        products = [
            {
                **product,
                "result_tier": (
                    "filtered"
                    if deterministic
                    else (
                        "ranked"
                        if str(product.get(self.result_id_column)) in primary_identities
                        else "related"
                    )
                ),
            }
            for product in products
        ]
        elapsed = time.perf_counter() - started
        LOGGER.debug(
            "[search:%s] step=result_cache status=hit ids=%d rows=%d "
            "database_ms=%.0f duration_ms=%.0f",
            trace_id,
            len(cached["product_ids"]),
            len(products),
            (time.perf_counter() - database_started) * 1000,
            elapsed * 1000,
        )
        return {
            "query_plan": cached["query_plan"],
            "resolved_filters": cached["resolved_filters"],
            "unresolved_filters": cached["unresolved_filters"],
            "query_model_metrics": {},
            "seconds": 0.0,
            "plan_cache_hit": True,
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
            "reranker_provider": cached.get("reranker_provider", "cache"),
            "reranker_attempts": [],
            "related_tail_seconds": 0.0,
            "primary_product_ids": cached["primary_product_ids"],
            "hybrid_product_ids": cached.get("hybrid_product_ids", []),
            "related_product_ids": cached["related_product_ids"],
            "product_ids": cached["product_ids"],
            "products": products,
            "result_cache_hit": True,
            "result_cache_seconds": elapsed,
        }

    def _cache_search_result(
        self,
        query: str,
        limit: int | None,
        result: dict,
        resolved_filters: dict | None = None,
        allowed_ad_types: set[str] | None = None,
        ranking_window: int | None = None,
    ) -> None:
        if (
            not RESULT_CACHE_ENABLED
            or self.shared_plan_cache is None
            or result["query_plan"].get("fallback_reason")
            or result.get("reranker_degraded")
            or result.get("retrieval_degraded")
        ):
            return
        payload = {
            "query_plan": result["query_plan"],
            "resolved_filters": result["resolved_filters"],
            "unresolved_filters": result["unresolved_filters"],
            "product_ids": [
                str(product_id) for product_id in result.get("product_ids", [])
            ],
            "primary_product_ids": [
                str(product_id) for product_id in result.get("primary_product_ids", [])
            ],
            "hybrid_product_ids": [
                str(product_id) for product_id in result.get("hybrid_product_ids", [])
            ],
            "related_product_ids": [
                str(product_id) for product_id in result.get("related_product_ids", [])
            ],
            "reranker_provider": result.get("reranker_provider", "none"),
        }
        self.shared_plan_cache.set_json(
            self._cache_namespace("search_result"),
            self._result_cache_key(
                query,
                limit,
                resolved_filters,
                allowed_ad_types,
                ranking_window,
            ),
            payload,
            RESULT_CACHE_TTL_SECONDS,
        )

    def _cached_plan(self, query: str) -> dict | None:
        if QUERY_PLAN_CACHE_SIZE <= 0 or QUERY_PLAN_CACHE_TTL_SECONDS <= 0:
            return None
        key = self._query_cache_key(query)
        with self._plan_cache_lock:
            cached = self._query_plan_cache.get(key)
            if cached is not None:
                expires_at, result = cached
                if expires_at > time.monotonic():
                    self._query_plan_cache.move_to_end(key)
                    return deepcopy(result)
                del self._query_plan_cache[key]
        if self.shared_plan_cache is None:
            return None
        shared_key = hashlib.sha256(
            f"{QUERY_PLAN_CACHE_SCHEMA_VERSION}\0{key}".encode()
        ).hexdigest()
        result = self.shared_plan_cache.get_json(
            self._cache_namespace("query_plan"),
            shared_key,
        )
        if result is None:
            return None
        self._cache_memory_plan(key, result)
        return deepcopy(result)

    def _cache_memory_plan(self, key: str, result: dict) -> None:
        with self._plan_cache_lock:
            self._query_plan_cache[key] = (
                time.monotonic() + QUERY_PLAN_CACHE_TTL_SECONDS,
                deepcopy(result),
            )
            self._query_plan_cache.move_to_end(key)
            while len(self._query_plan_cache) > QUERY_PLAN_CACHE_SIZE:
                self._query_plan_cache.popitem(last=False)

    def _cache_plan(self, query: str, result: dict) -> None:
        if QUERY_PLAN_CACHE_SIZE <= 0 or QUERY_PLAN_CACHE_TTL_SECONDS <= 0:
            return
        key = self._query_cache_key(query)
        self._cache_memory_plan(key, result)
        if self.shared_plan_cache is not None:
            shared_key = hashlib.sha256(
                f"{QUERY_PLAN_CACHE_SCHEMA_VERSION}\0{key}".encode()
            ).hexdigest()
            self.shared_plan_cache.set_json(
                self._cache_namespace("query_plan"),
                shared_key,
                result,
                QUERY_PLAN_CACHE_TTL_SECONDS,
            )

    def plan(self, query: str, trace_id: str = "-") -> dict:
        started = time.perf_counter()
        LOGGER.debug(
            "[search:%s] step=plan status=start query_chars=%d models=%s",
            trace_id,
            len(query),
            " -> ".join(QUERY_EXTRACT_MODELS),
        )
        cached = self._cached_plan(query)
        if cached is not None:
            elapsed = time.perf_counter() - started
            cached.update(
                {
                    "query_model_metrics": {},
                    "seconds": elapsed,
                    "plan_cache_hit": True,
                }
            )
            LOGGER.info(
                "[search:%s] step=plan status=cache_hit path=%s "
                "route_reason=%s "
                "duration_ms=%.0f",
                trace_id,
                cached["query_plan"].get("execution_path", "semantic"),
                cached["query_plan"].get("route_reason", "cached"),
                elapsed * 1000,
            )
            return cached
        analysis_cache = {}
        query_plan = (
            _engine_dependency(
                "deterministic_filter_query_plan",
                deterministic_filter_query_plan,
            )(
                query,
                self.filter_value_index,
                None,
                analysis_cache,
                self.search_policy,
            )
            if QUERY_DETERMINISTIC_FAST_PATH
            else None
        )
        direct_rejection_reason = "deterministic_match"
        if query_plan is None and self.direct_semantic_fast_path:
            query_plan, direct_rejection_reason = _engine_dependency(
                "direct_semantic_query_plan",
                direct_semantic_query_plan,
            )(
                query,
                self.filter_value_index,
                self.planner_query_aliases,
                analysis_cache,
                self.search_policy,
            )
        if query_plan is None:
            query_plan = (
                _engine_dependency(
                    "extract_query_plan",
                    extract_query_plan,
                )(
                    query,
                    self.filter_catalog,
                    query_provider=self.query_provider,
                    prompt_context=self.planner_prompt_context,
                    query_aliases=self.planner_query_aliases,
                )
                if self.planner_enabled
                else _engine_dependency(
                    "default_query_plan",
                    default_query_plan,
                )(query)
            )
            query_plan = _engine_dependency(
                "enrich_query_plan",
                enrich_query_plan,
            )(
                query,
                query_plan,
                self.filter_value_index,
                self.planner_query_aliases,
                analysis=_engine_dependency(
                    "query_analysis",
                    query_analysis,
                )(
                    query,
                    self.filter_value_index,
                    self.planner_query_aliases,
                    analysis_cache,
                ),
                search_policy=self.search_policy,
            )
            query_plan["execution_path"] = "semantic"
            query_plan["route_reason"] = (
                "llm_required:" + direct_rejection_reason
                if self.direct_semantic_fast_path
                else "llm_required:direct_path_disabled"
            )
        resolved, unresolved = _engine_dependency(
            "resolve_query_filters",
            resolve_query_filters,
        )(
            query_plan["filters"],
            self.filter_value_index,
        )
        query_metrics = (
            _engine_dependency(
                "last_gemini_metrics",
                last_gemini_metrics,
            )()
            if self.query_provider is None
            and self.planner_enabled
            and query_plan["execution_path"] == "semantic"
            else {}
        )
        elapsed = time.perf_counter() - started
        log_method = (
            LOGGER.warning if query_plan.get("fallback_reason") else LOGGER.info
        )
        model_label = query_metrics.get("model")
        attempted_label = ",".join(query_metrics.get("attempted_models", []))
        if query_plan["execution_path"] in {
            "deterministic_filter",
            "direct_semantic",
        }:
            model_label = "none"
            attempted_label = "none"
        elif not model_label:
            model_label = (
                type(self.query_provider).__name__
                if self.planner_enabled
                else "disabled"
            )
            attempted_label = attempted_label or "custom"
        log_method(
            "[search:%s] step=plan status=%s path=%s model=%s attempted=%s "
            "filters=%s unresolved=%d route_reason=%s reason=%s "
            "duration_ms=%.0f",
            trace_id,
            ("provider_fallback" if query_plan.get("fallback_reason") else "complete"),
            query_plan["execution_path"],
            model_label,
            attempted_label,
            ",".join(active_filter_names(query_plan["filters"])) or "none",
            len(unresolved),
            query_plan.get("route_reason") or "none",
            query_plan.get("fallback_reason") or "none",
            elapsed * 1000,
        )
        result = {
            "query_plan": query_plan,
            "resolved_filters": resolved,
            "unresolved_filters": unresolved,
            "query_model_metrics": query_metrics,
            "seconds": elapsed,
            "plan_cache_hit": False,
        }
        if not query_plan.get("fallback_reason"):
            self._cache_plan(
                query,
                {
                    "query_plan": query_plan,
                    "resolved_filters": resolved,
                    "unresolved_filters": unresolved,
                },
            )
        return result
