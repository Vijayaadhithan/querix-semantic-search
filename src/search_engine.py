import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from bm25_index import PersistentBM25Index
from database_store import (
    create_database_pool,
    DatabaseRuntimeConfig,
    database_backend,
    database_source_name,
    fetch_product_types_by_ids,
    fetch_products_by_ids,
)
from gemini_client import last_gemini_metrics
from ollama_client import last_ollama_embedding_metrics
from query_planner import (
    WANTED_AD_TYPE,
    build_query_filter_catalog,
    default_query_plan,
    deterministic_filter_query_plan,
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
    merge_results,
    related_tail_product_ids,
    vector_search,
)
from settings import (
    BM25_TOP_K,
    EMBED_MODEL,
    HYBRID_CANDIDATE_K,
    JINA_RERANK_MODEL,
    UNPRICED_RENTAL_FEE_CEILING,
    MYSQL_RESULT_ID_COLUMN,
    MYSQL_SEARCH_ID_COLUMN,
    MYSQL_TABLE,
    PRIMARY_RANKED_K,
    QUERY_DETERMINISTIC_FAST_PATH,
    QUERY_EXTRACT_MODELS,
    QUERY_PLAN_CACHE_SIZE,
    QUERY_PLAN_CACHE_TTL_SECONDS,
    RELATED_TAIL_ENABLED,
    RESULT_CACHE_ENABLED,
    RESULT_CACHE_TTL_SECONDS,
    RERANK_CANDIDATE_K,
    RERANK_MAX_DOCUMENT_CHARS,
    RETRIEVAL_OVERFETCH_FACTOR,
    RERANK_MODEL,
    RERANK_PROVIDER_ORDER,
    RERANK_TOP_K,
    VECTOR_CANDIDATE_K,
    VECTOR_TOP_K,
    VOYAGE_RERANK_LITE_MODEL,
    VOYAGE_RERANK_MODEL,
)

LOGGER = logging.getLogger("uvicorn.error")
RESULT_CACHE_SCHEMA_VERSION = "v15"
QUERY_PLAN_CACHE_SCHEMA_VERSION = "v2"

GAINR_VEHICLE_INTENT_TERMS = {
    "automobile",
    "automobiles",
    "bike",
    "cab",
    "car",
    "driver",
    "suv",
    "taxi",
    "traveller",
    "transport",
    "travel",
    "vehicle",
    "van",
}
GAINR_VEHICLE_USE_TERMS = {
    "comfort",
    "comfortable",
    "daily",
    "day",
    "distance",
    "drive",
    "hire",
    "long",
    "monthly",
    "ride",
    "rent",
    "rental",
    "safe",
    "safety",
    "tour",
    "trip",
    "weekly",
}
GAINR_VEHICLE_SERVICE_TERMS = {
    "buffing",
    "cleaning",
    "consultant",
    "detailing",
    "detailer",
    "insurance",
    "mechanic",
    "modification",
    "modifier",
    "polish",
    "polisher",
    "repair",
    "service",
    "wash",
}
GAINR_USABLE_VEHICLE_TERMS = {
    "acting driver",
    "bike",
    "bus",
    "cab",
    "car",
    "chauffeur",
    "driver",
    "innova",
    "light motor vehicle",
    "mini truck",
    "pickup",
    "sedan",
    "suv",
    "taxi",
    "tempo traveller",
    "tourist vehicle",
    "traveller",
    "truck",
    "van",
}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


def _contains_phrase(text: str, phrases: set[str]) -> bool:
    normalized = " ".join(text.casefold().split())
    return any(
        re.search(
            r"(?<!\w)"
            + r"\s+".join(
                re.escape(token)
                for token in phrase.casefold().split()
            )
            + r"(?!\w)",
            normalized,
        )
        for phrase in phrases
        if phrase.strip()
    )


def _gainr_vehicle_travel_intent(query_plan: dict | None) -> bool:
    if not isinstance(query_plan, dict):
        return False
    query_text = " ".join(
        str(query_plan.get(key) or "")
        for key in ("semantic_query", "keyword_query")
    )
    query_tokens = _tokens(query_text)
    if query_tokens & GAINR_VEHICLE_SERVICE_TERMS:
        return False
    if not (query_tokens & GAINR_VEHICLE_INTENT_TERMS):
        return False
    return bool(query_tokens & GAINR_VEHICLE_USE_TERMS) or _contains_phrase(
        query_text,
        {"long distance", "outstation", "road trip"},
    )


def _candidate_text(candidate: dict) -> str:
    metadata = candidate.get("metadata") or {}
    parts = [str(candidate.get("text") or "")]
    for key in (
        "content_title",
        "title",
        "main_category_name",
        "subcategory_name",
        "description",
    ):
        value = metadata.get(key)
        if value not in (None, ""):
            parts.append(str(value))
    return " ".join(parts)


def _apply_gainr_domain_intent_adjustments(
    query_plan: dict,
    candidates: list[dict],
    company_id: str | None = None,
) -> list[dict]:
    if company_id != "gainr":
        return candidates
    if not _gainr_vehicle_travel_intent(query_plan):
        return candidates

    adjusted = []
    for candidate in candidates:
        item = dict(candidate)
        text = _candidate_text(item).casefold()
        metadata = item.get("metadata") or {}
        score = float(item.get("fusion_score") or 0.0)

        is_automobile = (
            str(metadata.get("main_category_name") or "").casefold()
            == "automobiles"
        )
        is_usable_vehicle = _contains_phrase(
            text,
            GAINR_USABLE_VEHICLE_TERMS,
        )
        if is_automobile:
            score += 0.035
        if is_usable_vehicle:
            score += 0.025
        if _contains_phrase(text, GAINR_VEHICLE_SERVICE_TERMS):
            score -= 0.08
        if not is_automobile and not is_usable_vehicle:
            score -= 0.06

        item["fusion_score"] = score
        adjusted.append(item)

    return sorted(
        adjusted,
        key=lambda item: float(item.get("fusion_score") or 0.0),
        reverse=True,
    )


def _gainr_rerank_context(
    query_plan: dict | None,
    company_id: str | None = None,
) -> str | None:
    if company_id != "gainr":
        return None
    if not _gainr_vehicle_travel_intent(query_plan):
        return None
    return (
        "Gainr domain intent: the user wants a usable vehicle or driver for "
        "travel/transport. Prefer car, cab, driver, van, bus, traveller, bike, "
        "truck, or other vehicle rental listings. Treat comfort and safety as "
        "desired vehicle qualities; generic safety officers, trainers, "
        "auditors, and safety services are irrelevant. Demote services about "
        "vehicles such as detailing, polishing, modification, insurance, "
        "cleaning, repair, or consulting unless the user explicitly asks for "
        "those services."
    )


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
        shared_plan_cache=None,
        company_id: str | None = None,
        mysql_config: DatabaseRuntimeConfig | None = None,
        shared_reranker=None,
        close_bm25_index: bool = False,
        planner_enabled: bool = True,
        planner_prompt_context: str = "",
        vector_post_filter_metadata: bool = False,
        semantic_related_tail_enabled: bool = RELATED_TAIL_ENABLED,
        semantic_related_tail_requires_explicit_category: bool = False,
        reranker_relative_score_floor: float = 0.0,
        reranker_min_score_by_provider: dict[str, float] | None = None,
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
        self.planner_prompt_context = planner_prompt_context
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
        self._query_plan_cache: OrderedDict[str, tuple[float, dict]] = (
            OrderedDict()
        )
        self._plan_cache_lock = threading.RLock()

    def close(self) -> None:
        if self.database_pool is not None:
            self.database_pool.close()
        if self._owns_bm25_index:
            self.bm25_index.close()

    @staticmethod
    def _query_cache_key(query: str) -> str:
        return " ".join(query.casefold().split())

    def set_shared_plan_cache(self, cache) -> None:
        self.shared_plan_cache = cache

    def _cache_namespace(self, name: str) -> str:
        return f"{self.company_id}:{name}" if self.company_id else name

    def _fetch_products(self, product_ids) -> list[dict]:
        if self.mysql_config is None:
            return fetch_products_by_ids(product_ids)
        if self.database_pool is not None:
            with self.database_pool.connection() as connection:
                return fetch_products_by_ids(
                    product_ids,
                    connection=connection,
                    config=self.mysql_config,
                )
        return fetch_products_by_ids(product_ids, config=self.mysql_config)

    def _fetch_product_types(self, product_ids) -> dict[str, str]:
        if self.mysql_config is None:
            return fetch_product_types_by_ids(product_ids)
        if self.database_pool is not None:
            with self.database_pool.connection() as connection:
                return fetch_product_types_by_ids(
                    product_ids,
                    connection=connection,
                    config=self.mysql_config,
                )
        return fetch_product_types_by_ids(
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
            JINA_RERANK_MODEL,
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
            LOGGER.info(
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
        products = self._fetch_products(cached["product_ids"])
        primary_identities = {
            str(product_id)
            for product_id in cached["primary_product_ids"]
        }
        deterministic = (
            cached["query_plan"].get("execution_path")
            == "deterministic_filter"
        )
        products = [
            {
                **product,
                "result_tier": (
                    "filtered"
                    if deterministic
                    else (
                        "ranked"
                        if str(product.get(self.result_id_column))
                        in primary_identities
                        else "related"
                    )
                ),
            }
            for product in products
        ]
        elapsed = time.perf_counter() - started
        LOGGER.info(
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
                str(product_id)
                for product_id in result.get("product_ids", [])
            ],
            "primary_product_ids": [
                str(product_id)
                for product_id in result.get("primary_product_ids", [])
            ],
            "hybrid_product_ids": [
                str(product_id)
                for product_id in result.get("hybrid_product_ids", [])
            ],
            "related_product_ids": [
                str(product_id)
                for product_id in result.get("related_product_ids", [])
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
            f"{QUERY_PLAN_CACHE_SCHEMA_VERSION}\0{key}".encode("utf-8")
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
                f"{QUERY_PLAN_CACHE_SCHEMA_VERSION}\0{key}".encode("utf-8")
            ).hexdigest()
            self.shared_plan_cache.set_json(
                self._cache_namespace("query_plan"),
                shared_key,
                result,
                QUERY_PLAN_CACHE_TTL_SECONDS,
            )

    def plan(self, query: str, trace_id: str = "-") -> dict:
        started = time.perf_counter()
        LOGGER.info(
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
                "duration_ms=%.0f",
                trace_id,
                cached["query_plan"].get("execution_path", "semantic"),
                elapsed * 1000,
            )
            return cached
        query_plan = (
            deterministic_filter_query_plan(
                query,
                self.filter_value_index,
            )
            if QUERY_DETERMINISTIC_FAST_PATH
            else None
        )
        if query_plan is None:
            query_plan = (
                extract_query_plan(
                    query,
                    self.filter_catalog,
                    query_provider=self.query_provider,
                    prompt_context=self.planner_prompt_context,
                )
                if self.planner_enabled
                else default_query_plan(query)
            )
            query_plan = enrich_query_plan(
                query,
                query_plan,
                self.filter_value_index,
            )
            query_plan["execution_path"] = "semantic"
        resolved, unresolved = resolve_query_filters(
            query_plan["filters"],
            self.filter_value_index,
        )
        query_metrics = (
            last_gemini_metrics()
            if self.query_provider is None
            and self.planner_enabled
            and query_plan["execution_path"] == "semantic"
            else {}
        )
        elapsed = time.perf_counter() - started
        log_method = (
            LOGGER.warning
            if query_plan.get("fallback_reason")
            else LOGGER.info
        )
        model_label = query_metrics.get("model")
        attempted_label = ",".join(
            query_metrics.get("attempted_models", [])
        )
        if query_plan["execution_path"] == "deterministic_filter":
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
            "filters=%s unresolved=%d reason=%s duration_ms=%.0f",
            trace_id,
            (
                "provider_fallback"
                if query_plan.get("fallback_reason")
                else "complete"
            ),
            query_plan["execution_path"],
            model_label,
            attempted_label,
            ",".join(active_filter_names(query_plan["filters"])) or "none",
            len(unresolved),
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

    def retrieve(
        self,
        query_plan: dict,
        resolved_filters: dict,
        candidate_limit: int | None = None,
        trace_id: str = "-",
        allowed_ad_types: set[str] | None = None,
        strict_candidate_limit: bool = False,
    ) -> dict:
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
        # retrieval recall. Gainr's intent adjustments need a deeper fused
        # pool so descriptive terms such as "safety" do not let keyword-heavy
        # service ads crowd usable vehicles out before reranking.
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

        def run_vector() -> tuple[list[dict], float]:
            started = time.perf_counter()
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
            )
            return results, time.perf_counter() - started

        def run_bm25() -> tuple[list[dict], float]:
            started = time.perf_counter()
            results = bm25_search(
                query_plan["keyword_query"],
                self.bm25_index,
                self.collection,
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
                vector_results, vector_seconds = vector_future.result()
            except Exception as exc:
                vector_results, vector_seconds = [], 0.0
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

        if len(retrieval_errors) == 2:
            stages = ", ".join(stage for stage, _exc in retrieval_errors)
            raise RuntimeError(f"All retrieval paths failed: {stages}") from (
                retrieval_errors[0][1]
            )

        merged = merge_results(
            vector_results,
            bm25_results,
            query_plan.get("inferred_categories"),
        )
        merged = _apply_gainr_domain_intent_adjustments(
            query_plan,
            merged,
            self.company_id,
        )
        # Apply ad intent before truncating the candidate window. Otherwise a
        # page can be short merely because unwanted ad types occupied the K
        # slots ahead of valid products.
        eligible_candidates = filter_candidates_by_ad_type(
            merged,
            query_plan["target_ad_type"],
            type_fetcher=self._fetch_product_types,
            search_table=self.search_table,
            search_id_column=self.search_id_column,
            allowed_ad_types=allowed_ad_types,
        )
        candidates = eligible_candidates[:hybrid_top_k]
        # Keep the rest of the fused pool for later pages without increasing
        # the hosted reranker payload. These candidates remain in reciprocal-
        # rank-fusion order and have passed the same hard filters and ad-type
        # validation as the reranked window.
        hybrid_tail_candidates = eligible_candidates[hybrid_top_k:]
        embedding_metrics = (
            last_ollama_embedding_metrics()
            if self.embedding_provider is None
            else {}
        )
        LOGGER.info(
            "[search:%s] step=retrieve status=complete vector=%d bm25=%d "
            "merged=%d candidates=%d hybrid_tail=%d vector_ms=%.0f bm25_ms=%.0f "
            "embed_total_ms=%.0f embed_load_ms=%.0f",
            trace_id,
            len(vector_results),
            len(bm25_results),
            len(merged),
            len(candidates),
            len(hybrid_tail_candidates),
            vector_seconds * 1000,
            bm25_seconds * 1000,
            embedding_metrics.get("total_ms", 0.0),
            embedding_metrics.get("load_ms", 0.0),
        )
        return {
            "vector_results": vector_results,
            "bm25_results": bm25_results,
            "candidates": candidates,
            "hybrid_tail_candidates": hybrid_tail_candidates,
            "vector_seconds": vector_seconds,
            "bm25_seconds": bm25_seconds,
            "embedding_model_metrics": embedding_metrics,
            "retrieval_degraded": bool(retrieval_errors),
            "retrieval_error_type": (
                type(retrieval_errors[0][1]).__name__
                if retrieval_errors
                else None
            ),
            "degraded_stages": [stage for stage, _exc in retrieval_errors],
        }

    def ensure_reranker(self) -> float:
        if self.ranker is not None:
            return 0.0
        if self.shared_reranker is not None:
            self.ranker, load_seconds = self.shared_reranker.ensure()
            return load_seconds
        started = time.perf_counter()
        self.ranker = load_reranker()
        return time.perf_counter() - started

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

    @staticmethod
    def _fusion_fallback_results(
        candidates: list[dict],
        top_k: int,
    ) -> list[dict]:
        results = []
        for position, candidate in enumerate(candidates[:top_k], start=1):
            try:
                score = float(candidate.get("fusion_score"))
            except (TypeError, ValueError):
                score = 1.0 / position
            results.append(
                {
                    "id": candidate["id"],
                    "text": candidate["text"],
                    "metadata": candidate["metadata"],
                    "score": score,
                }
            )
        return results

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
            domain_context = _gainr_rerank_context(
                query_plan,
                self.company_id,
            )
            if domain_context:
                context.append(domain_context)
            if context:
                ranking_query = query + "\n" + "\n".join(context)
        started = time.perf_counter()
        LOGGER.info(
            "[search:%s] step=rerank status=start model=%s candidates=%d top_k=%d",
            trace_id,
            getattr(self.ranker, "model_label", RERANK_MODEL),
            len(candidates),
            RERANK_TOP_K if top_k is None else top_k,
        )
        effective_top_k = RERANK_TOP_K if top_k is None else top_k

        def run_rerank():
            return rerank(
                ranking_query,
                candidates,
                self.ranker,
                effective_top_k,
                diversity_top_k=effective_top_k,
            )

        def fallback_rank(exc: Exception) -> dict:
            elapsed = time.perf_counter() - started
            attempts = list(getattr(self.ranker, "last_attempts", []))
            results = self._fusion_fallback_results(
                candidates,
                effective_top_k,
            )
            LOGGER.warning(
                "[search:%s] step=rerank status=degraded "
                "provider=fusion_fallback error_type=%s "
                "candidates=%d results=%d load_ms=%.0f duration_ms=%.0f",
                trace_id,
                type(exc).__name__,
                len(candidates),
                len(results),
                load_seconds * 1000,
                elapsed * 1000,
            )
            return {
                "results": results,
                "load_seconds": load_seconds,
                "seconds": elapsed,
                "provider": "fusion_fallback",
                "attempts": attempts,
                "degraded": True,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

        if self.shared_reranker is not None:
            with self.shared_reranker.inference_guard():
                try:
                    results = run_rerank()
                except Exception as exc:
                    return fallback_rank(exc)
        else:
            try:
                results = run_rerank()
            except Exception as exc:
                return fallback_rank(exc)
        elapsed = time.perf_counter() - started
        provider = getattr(self.ranker, "last_provider", "local")
        attempts = list(getattr(self.ranker, "last_attempts", []))
        unfiltered_count = len(results)
        cutoff = None
        if results:
            top_score = float(results[0]["score"])
            cutoffs = []
            provider_floor = self.reranker_min_score_by_provider.get(
                str(provider).casefold()
            )
            if provider_floor is not None:
                cutoffs.append(provider_floor)
            if (
                self.reranker_relative_score_floor > 0
                and top_score > 0
            ):
                cutoffs.append(
                    top_score * self.reranker_relative_score_floor
                )
            if cutoffs:
                cutoff = max(cutoffs)
                results = [
                    result
                    for result in results
                    if float(result["score"]) >= cutoff
                ]
        LOGGER.info(
            "[search:%s] step=rerank status=complete provider=%s results=%d "
            "pruned=%d cutoff=%s load_ms=%.0f duration_ms=%.0f",
            trace_id,
            provider,
            len(results),
            unfiltered_count - len(results),
            f"{cutoff:.6f}" if cutoff is not None else "none",
            load_seconds * 1000,
            elapsed * 1000,
        )
        return {
            "results": results,
            "load_seconds": load_seconds,
            "seconds": elapsed,
            "provider": provider,
            "attempts": attempts,
            "degraded": False,
        }

    def _filtered_search(
        self,
        planned: dict,
        limit: int | None,
        trace_id: str,
        search_started: float,
        allowed_ad_types: set[str] | None = None,
        ranking_window: int | None = None,
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
        LOGGER.info(
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
        retrieved = self.retrieve(
            planned["query_plan"],
            planned["resolved_filters"],
            candidate_limit=rerank_candidate_limit,
            trace_id=trace_id,
            allowed_ad_types=allowed_ad_types,
            strict_candidate_limit=ranking_window is not None,
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
        LOGGER.info(
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
        LOGGER.info(
            "[search:%s] step=database_map status=start product_ids=%d",
            trace_id,
            len(product_ids),
        )
        products = self._fetch_products(product_ids)
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
        if sort_order in {"price_asc", "price_desc"}:
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
        LOGGER.info(
            "[search:%s] step=database_map status=complete rows=%d",
            trace_id,
            len(products),
        )
        LOGGER.info(
            "[search:%s] step=search status=complete products=%d duration_ms=%.0f",
            trace_id,
            len(products),
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
