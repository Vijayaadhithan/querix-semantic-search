import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from api.contracts import (
    HealthResponse,
    InvalidCursorError,
    PUBLIC_PRODUCT_FIELDS,
    PaginationResponse,
    SearchCapacityError,
    SearchRequest,
    SearchResponse,
    SearchSession,
    SearchSessionStore,
    decode_cursor,
    encode_cursor,
)
from search.engine import ProductSearchEngine
from core.settings import (
    API_MAX_RESULTS,
    API_SEARCH_SLOT_TIMEOUT_SECONDS,
    API_TENANT_MAX_CONCURRENT_SEARCHES,
    APP_NAME,
    EMBED_MODEL,
    RERANK_MODEL,
)
from storage.search_analytics import (
    SearchAnalyticsEvent,
    SearchAnalyticsStore,
    SearchApiUsageEvent,
)
from storage.usage import MonthlyUsageStore

LOGGER = logging.getLogger("uvicorn.error")


class ProductSearchService:
    def __init__(
        self,
        engine: ProductSearchEngine,
        sessions: SearchSessionStore | None = None,
        max_results: int = API_MAX_RESULTS,
        company_id: str | None = None,
        public_fields: tuple[str, ...] = PUBLIC_PRODUCT_FIELDS,
        field_mapping: dict[str, str] | None = None,
        usage_store: MonthlyUsageStore | None = None,
        analytics_store: SearchAnalyticsStore | None = None,
        max_concurrent_searches: int = API_TENANT_MAX_CONCURRENT_SEARCHES,
        search_slot_timeout_seconds: float = API_SEARCH_SLOT_TIMEOUT_SECONDS,
    ):
        if max_results <= 0:
            raise ValueError("Maximum results must be greater than zero.")
        if max_concurrent_searches <= 0:
            raise ValueError("Maximum concurrent searches must be greater than zero.")
        if search_slot_timeout_seconds <= 0:
            raise ValueError("Search slot timeout must be greater than zero.")
        self.engine = engine
        self.sessions = sessions or SearchSessionStore()
        self.max_results = max_results
        self.company_id = company_id
        self.public_fields = tuple(public_fields)
        self.field_mapping = dict(field_mapping or {})
        self.usage_store = usage_store
        self.analytics_store = analytics_store
        self.search_slot_timeout_seconds = search_slot_timeout_seconds
        self._engine_lock = threading.Lock()
        self._search_slots = threading.BoundedSemaphore(
            max_concurrent_searches
        )
        self.reranker_load_ms = 0.0
        self.embedding_warmup: dict[str, Any] = {}
        self._monitor_lock = threading.Lock()
        self._monitor_active = 0
        self._monitor_started = 0
        self._monitor_completed = 0
        self._monitor_failed = 0
        self._monitor_rejected = 0
        self._monitor_degraded = 0
        self._monitor_events: deque[dict[str, Any]] = deque(maxlen=100)
        self._coalesce_lock = threading.Lock()
        self._inflight_searches: dict[str, threading.Event] = {}

    def warmup(self) -> float:
        with self._engine_lock:
            load_seconds = self.engine.ensure_reranker()
        self.reranker_load_ms = load_seconds * 1000
        return self.reranker_load_ms

    def health(self) -> HealthResponse:
        with self._engine_lock:
            indexed_products = self.engine.bm25_index.count()
            cache_health = (
                self.engine.plan_cache_health()
                if hasattr(self.engine, "plan_cache_health")
                else {
                    "redis_enabled": False,
                    "redis_connected": False,
                    "query_plan_cache_backend": "memory",
                    "result_cache_enabled": False,
                    "result_cache_ttl_seconds": 0,
                }
            )
        return HealthResponse(
            status="ok",
            app=APP_NAME,
            indexed_products=indexed_products,
            max_result_window=self.max_results,
            session_ttl_seconds=self.sessions.ttl_seconds,
            reranker_model=getattr(
                self.engine.ranker,
                "model_label",
                RERANK_MODEL,
            ),
            reranker_loaded=self.engine.ranker is not None,
            reranker_load_ms=self.reranker_load_ms,
            embedding_warmup=self.embedding_warmup,
            company_id=self.company_id,
            **cache_health,
        )

    def readiness(self) -> dict[str, Any]:
        components: dict[str, dict[str, Any]] = {}
        try:
            indexed_products = self.engine.bm25_index.count()
            components["bm25"] = {
                "ok": indexed_products > 0,
                "indexed_products": indexed_products,
            }
        except Exception as exc:
            components["bm25"] = {
                "ok": False,
                "error_type": type(exc).__name__,
            }

        collection = getattr(self.engine, "collection", None)
        if collection is None or not hasattr(collection, "count"):
            components["pgvector"] = {"ok": True, "configured": False}
        else:
            try:
                vector_products = int(collection.count())
                components["pgvector"] = {
                    "ok": vector_products > 0,
                    "indexed_products": vector_products,
                }
            except Exception as exc:
                components["pgvector"] = {
                    "ok": False,
                    "error_type": type(exc).__name__,
                }

        database_pool = getattr(self.engine, "database_pool", None)
        if database_pool is None:
            components["database"] = {"ok": True, "configured": False}
        else:
            try:
                with database_pool.connection() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT 1")
                        cursor.fetchone()
                components["database"] = {"ok": True}
            except Exception as exc:
                components["database"] = {
                    "ok": False,
                    "error_type": type(exc).__name__,
                }
        return {
            "ok": all(component["ok"] for component in components.values()),
            "components": components,
        }

    def monitor_status(self) -> dict[str, Any]:
        with self._monitor_lock:
            events = list(self._monitor_events)[:20]
            return {
                "active": self._monitor_active,
                "started": self._monitor_started,
                "completed": self._monitor_completed,
                "failed": self._monitor_failed,
                "rejected": self._monitor_rejected,
                "degraded": self._monitor_degraded,
                "recent": events,
            }

    def monitor_events(
        self,
        *,
        limit: int = 20,
        event_status: str | None = None,
    ) -> dict[str, Any]:
        with self._monitor_lock:
            events = list(self._monitor_events)
            active = self._monitor_active
        if event_status is not None:
            events = [
                event
                for event in events
                if event.get("status") == event_status
            ]
        return {
            "active": active,
            "retained": len(events),
            "events": events[:limit],
        }

    @staticmethod
    def _monitor_timeline(
        result: dict[str, Any],
        duration_ms: float,
    ) -> list[dict[str, Any]]:
        query_plan = result.get("query_plan") or {}
        query_metrics = result.get("query_model_metrics") or {}
        embedding = result.get("embedding_model_metrics") or {}
        execution_path = query_plan.get("execution_path", "semantic")
        timeline: list[dict[str, Any]] = [
            {
                "step": "plan",
                "status": (
                    "cache_hit"
                    if result.get("plan_cache_hit")
                    else "complete"
                ),
                "duration_ms": round(
                    float(result.get("seconds", 0.0)) * 1000,
                    3,
                ),
                "execution_path": execution_path,
                "model": query_metrics.get("model") or "none",
                "resolved_filter_groups": len(
                    result.get("resolved_filters") or {}
                ),
                "unresolved_filters": len(
                    result.get("unresolved_filters") or {}
                ),
            }
        ]
        if result.get("result_cache_hit"):
            timeline.append(
                {
                    "step": "result_cache",
                    "status": "hit",
                    "duration_ms": round(
                        float(
                            result.get("result_cache_seconds", 0.0)
                        )
                        * 1000,
                        3,
                    ),
                    "products": len(result.get("products") or []),
                }
            )
        elif execution_path == "deterministic_filter":
            timeline.append(
                {
                    "step": "fast_filter",
                    "status": "complete",
                    "duration_ms": round(
                        float(
                            result.get("related_tail_seconds", 0.0)
                        )
                        * 1000,
                        3,
                    ),
                    "products": len(result.get("products") or []),
                }
            )
        else:
            vector_results = result.get("vector_results") or []
            bm25_results = result.get("bm25_results") or []
            candidates = result.get("candidates") or []
            timeline.extend(
                [
                    {
                        "step": "retrieve",
                        "status": (
                            "degraded"
                            if result.get("retrieval_degraded")
                            else "complete"
                        ),
                        "duration_ms": round(
                            float(
                                result.get(
                                    "retrieval_seconds",
                                    max(
                                        float(
                                            result.get(
                                                "vector_seconds",
                                                0.0,
                                            )
                                        ),
                                        float(
                                            result.get(
                                                "bm25_seconds",
                                                0.0,
                                            )
                                        ),
                                    ),
                                )
                            )
                            * 1000,
                            3,
                        ),
                        "vector_ms": round(
                            float(
                                result.get("vector_seconds", 0.0)
                            )
                            * 1000,
                            3,
                        ),
                        "bm25_ms": round(
                            float(
                                result.get("bm25_seconds", 0.0)
                            )
                            * 1000,
                            3,
                        ),
                        "embedding_total_ms": round(
                            float(embedding.get("total_ms", 0.0)),
                            3,
                        ),
                        "embedding_load_ms": round(
                            float(embedding.get("load_ms", 0.0)),
                            3,
                        ),
                        "parallel_ms": round(
                            float(
                                result.get(
                                    "parallel_retrieval_seconds",
                                    0.0,
                                )
                            )
                            * 1000,
                            3,
                        ),
                        "fusion_ms": round(
                            float(result.get("fusion_seconds", 0.0))
                            * 1000,
                            3,
                        ),
                        "type_lookup_ms": round(
                            float(
                                result.get("type_lookup_seconds", 0.0)
                            )
                            * 1000,
                            3,
                        ),
                        "vector_query_metrics": result.get(
                            "vector_query_metrics",
                            {},
                        ),
                        "vector_results": len(vector_results),
                        "bm25_results": len(bm25_results),
                        "candidates": len(candidates),
                        "degraded_stages": result.get(
                            "degraded_stages",
                            [],
                        ),
                        "error_type": result.get("retrieval_error_type"),
                    },
                    {
                        "step": "rerank",
                        "status": (
                            "degraded"
                            if result.get("reranker_degraded")
                            else "complete"
                            if candidates
                            else "skipped"
                        ),
                        "duration_ms": round(
                            float(
                                result.get("reranker_seconds", 0.0)
                            )
                            * 1000,
                            3,
                        ),
                        "provider": result.get(
                            "reranker_provider",
                            "none",
                        ),
                        "results": len(result.get("reranked") or []),
                        "error_type": result.get("reranker_error_type"),
                    },
                    {
                        "step": "related_tail",
                        "status": "complete",
                        "duration_ms": round(
                            float(
                                result.get(
                                    "related_tail_seconds",
                                    0.0,
                                )
                            )
                            * 1000,
                            3,
                        ),
                        "primary": len(
                            result.get("primary_product_ids") or []
                        ),
                        "related": len(
                            result.get("related_product_ids") or []
                        ),
                    },
                    {
                        "step": "database_map",
                        "status": "complete",
                        "products": len(result.get("products") or []),
                    },
                ]
            )
        timeline.append(
            {
                "step": "search",
                "status": "complete",
                "duration_ms": round(duration_ms, 3),
                "products": len(result.get("products") or []),
            }
        )
        return timeline

    def run_engine_search(self, query: str, **kwargs) -> dict[str, Any]:
        started = time.perf_counter()
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._monitor_lock:
            self._monitor_active += 1
            self._monitor_started += 1
        coalesce_key = self._coalesce_key(query, kwargs)
        owner = False
        inflight: threading.Event | None = None
        with self._coalesce_lock:
            inflight = self._inflight_searches.get(coalesce_key)
            if inflight is None:
                inflight = threading.Event()
                self._inflight_searches[coalesce_key] = inflight
                owner = True
        try:
            if not owner:
                wait_started = time.perf_counter()
                completed = inflight.wait(
                    timeout=max(10.0, self.search_slot_timeout_seconds)
                )
                if not completed:
                    with self._monitor_lock:
                        self._monitor_rejected += 1
                    raise SearchCapacityError(
                        "An identical search is still running; retry shortly."
                    )
                LOGGER.info(
                    "step=request_coalesce status=waited company=%s "
                    "query_chars=%d duration_ms=%.0f",
                    self.company_id or "legacy",
                    len(query),
                    (time.perf_counter() - wait_started) * 1000,
                )
            acquired = self._search_slots.acquire(
                timeout=self.search_slot_timeout_seconds
            )
            if not acquired:
                with self._monitor_lock:
                    self._monitor_rejected += 1
                raise SearchCapacityError(
                    "Search capacity is busy; retry shortly."
                )
            try:
                result = self.engine.search(query, **kwargs)
            finally:
                self._search_slots.release()
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            with self._monitor_lock:
                self._monitor_failed += 1
                self._monitor_events.appendleft(
                    {
                        "timestamp_utc": timestamp,
                        "status": "failed",
                        "query_chars": len(query),
                        "duration_ms": round(duration_ms, 3),
                        "error_type": type(exc).__name__,
                        "timeline": [
                            {
                                "step": "search",
                                "status": "failed",
                                "duration_ms": round(duration_ms, 3),
                                "error_type": type(exc).__name__,
                            }
                        ],
                    }
                )
            raise
        else:
            duration_ms = (time.perf_counter() - started) * 1000
            query_plan = result.get("query_plan") or {}
            embedding = result.get("embedding_model_metrics") or {}
            event = {
                "timestamp_utc": timestamp,
                "trace_id": result.get("trace_id"),
                "status": "success",
                "query_chars": len(query),
                "duration_ms": round(duration_ms, 3),
                "execution_path": query_plan.get(
                    "execution_path",
                    "semantic",
                ),
                "result_cache_hit": bool(result.get("result_cache_hit")),
                "products": len(result.get("products") or []),
                "reranker_provider": result.get(
                    "reranker_provider",
                    "none",
                ),
                "timings_ms": {
                    "planning": round(
                        float(result.get("seconds", 0.0)) * 1000,
                        3,
                    ),
                    "vector_search": round(
                        float(result.get("vector_seconds", 0.0)) * 1000,
                        3,
                    ),
                    "bm25_search": round(
                        float(result.get("bm25_seconds", 0.0)) * 1000,
                        3,
                    ),
                    "embedding_total": round(
                        float(embedding.get("total_ms", 0.0)),
                        3,
                    ),
                    "embedding_load": round(
                        float(embedding.get("load_ms", 0.0)),
                        3,
                    ),
                    "reranking": round(
                        float(result.get("reranker_seconds", 0.0)) * 1000,
                        3,
                    ),
                    "related_tail": round(
                        float(result.get("related_tail_seconds", 0.0))
                        * 1000,
                        3,
                    ),
                    "result_cache": round(
                        float(result.get("result_cache_seconds", 0.0))
                        * 1000,
                        3,
                    ),
                },
                "timeline": self._monitor_timeline(
                    result,
                    duration_ms,
                ),
            }
            result["_service_total_ms"] = duration_ms
            with self._monitor_lock:
                self._monitor_completed += 1
                if result.get("retrieval_degraded") or result.get(
                    "reranker_degraded"
                ):
                    self._monitor_degraded += 1
                self._monitor_events.appendleft(event)
            return result
        finally:
            if owner and inflight is not None:
                with self._coalesce_lock:
                    current = self._inflight_searches.get(coalesce_key)
                    if current is inflight:
                        del self._inflight_searches[coalesce_key]
                    inflight.set()
            with self._monitor_lock:
                self._monitor_active -= 1

    @staticmethod
    def _coalesce_key(query: str, kwargs: dict[str, Any]) -> str:
        return json.dumps(
            {
                "query": " ".join(query.split()).casefold(),
                "kwargs": kwargs,
            },
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )

    def record_external_search(
        self,
        query: str,
        *,
        execution_path: str,
        duration_ms: float,
        products: int,
        timeline: list[dict[str, Any]] | None = None,
    ) -> None:
        event = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "status": "success",
            "query_chars": len(query),
            "duration_ms": round(duration_ms, 3),
            "execution_path": execution_path,
            "result_cache_hit": False,
            "products": products,
            "reranker_provider": "none",
            "timings_ms": {
                "planning": 0.0,
                "vector_search": 0.0,
                "bm25_search": 0.0,
                "embedding_total": 0.0,
                "embedding_load": 0.0,
                "reranking": 0.0,
                "related_tail": 0.0,
                "result_cache": 0.0,
            },
            "timeline": timeline
            or [
                {
                    "step": "filter_result",
                    "status": "complete",
                    "duration_ms": round(duration_ms, 3),
                    "products": products,
                }
            ],
        }
        with self._monitor_lock:
            self._monitor_started += 1
            self._monitor_completed += 1
            self._monitor_events.appendleft(event)

    def search(self, request: SearchRequest) -> SearchResponse:
        if request.query is not None:
            session = self._start_search(request.query)
            offset = 0
            cached = bool(
                session.interpreted_query.get("result_cache_hit")
            )
        else:
            search_id, offset = decode_cursor(request.cursor or "")
            session = self.sessions.get(search_id)
            cached = True
        return self._page(session, offset, request.page_size, cached)

    def _start_search(self, query: str) -> SearchSession:
        result = self.run_engine_search(query, limit=self.max_results)
        total_ms = float(result.get("_service_total_ms", 0.0))
        query_plan = result["query_plan"]
        usage = self._record_usage(result)
        interpreted_query = {
            key: query_plan.get(key)
            for key in (
                "semantic_query",
                "keyword_query",
                "target_ad_type",
                "sort_order",
            )
        }
        interpreted_query.update(
            {
                "execution_path": query_plan.get(
                    "execution_path",
                    "semantic",
                ),
                "plan_cache_hit": bool(result.get("plan_cache_hit")),
                "result_cache_hit": bool(result.get("result_cache_hit")),
                "query_corrections": query_plan.get(
                    "query_corrections",
                    [],
                ),
                "reranker_provider": result.get(
                    "reranker_provider",
                    "none",
                ),
                "retrieval_degraded": bool(
                    result.get("retrieval_degraded")
                ),
                "degraded_stages": result.get("degraded_stages", []),
            }
        )
        timings_ms = {
            "planning": result.get("seconds", 0.0) * 1000,
            "vector_search": result.get("vector_seconds", 0.0) * 1000,
            "bm25_search": result.get("bm25_seconds", 0.0) * 1000,
            "related_tail": result.get("related_tail_seconds", 0.0) * 1000,
            "reranker_load": result.get("reranker_load_seconds", 0.0) * 1000,
            "reranking": result.get("reranker_seconds", 0.0) * 1000,
            "result_cache": result.get(
                "result_cache_seconds",
                0.0,
            )
            * 1000,
            "total": total_ms,
        }
        query_metrics = result.get("query_model_metrics") or {}
        embedding_metrics = result.get("embedding_model_metrics") or {}
        timings_ms.update(
            {
                "query_model_total": query_metrics.get("total_ms", 0.0),
                "query_model_load": query_metrics.get("load_ms", 0.0),
                "embedding_model_total": embedding_metrics.get("total_ms", 0.0),
                "embedding_model_load": embedding_metrics.get("load_ms", 0.0),
            }
        )
        items = [
            public_product(
                product,
                fields=self.public_fields,
                field_mapping=self.field_mapping,
            )
            for product in result["products"]
            if product_is_visible(product)
        ]
        self.record_search_analytics(
            query,
            result,
            duration_ms=total_ms,
            result_count=len(items),
            total_results=len(result.get("product_ids") or items),
            filters=result.get("resolved_filters") or {},
        )
        return self.sessions.create(
            query=query,
            items=items,
            interpreted_query=interpreted_query,
            applied_filters=result["resolved_filters"],
            unresolved_filters=result["unresolved_filters"],
            timings_ms=timings_ms,
            usage=usage,
            company_id=self.company_id,
        )

    def _record_usage(self, result: dict) -> dict[str, Any]:
        company_id = self.company_id or "legacy"
        events = []
        query_metrics = result.get("query_model_metrics") or {}
        query_attempts = query_metrics.get("attempts") or (
            [query_metrics] if query_metrics.get("model") else []
        )
        for attempt in query_attempts:
            model = str(attempt.get("model") or "")
            if not model:
                continue
            events.append(
                {
                    "provider": (
                        str(attempt.get("provider"))
                        if attempt.get("provider")
                        else (
                            "groq"
                            if model.startswith("groq:")
                            else "google"
                        )
                    ),
                    "model": model,
                    "operation": "query_planning",
                    "status": str(attempt.get("status") or "success"),
                    "input_tokens": int(
                        attempt.get("input_tokens", 0) or 0
                    ),
                    "output_tokens": int(
                        attempt.get("output_tokens", 0) or 0
                    ),
                    "total_tokens": int(
                        attempt.get("total_tokens", 0) or 0
                    ),
                }
            )
        for attempt in result.get("reranker_attempts") or []:
            provider_name = str(attempt.get("provider") or "")
            provider = (
                "voyage"
                if provider_name.startswith("voyage")
                else provider_name
            )
            usage = attempt.get("usage") or {}
            events.append(
                {
                    "provider": provider,
                    "model": str(attempt.get("model") or provider_name),
                    "operation": "reranking",
                    "status": str(attempt.get("status") or "success"),
                    "input_tokens": int(
                        usage.get("input_tokens", 0) or 0
                    ),
                    "output_tokens": int(
                        usage.get("output_tokens", 0) or 0
                    ),
                    "total_tokens": int(
                        usage.get("total_tokens", 0) or 0
                    ),
                }
            )
        execution_path = str(
            result.get("query_plan", {}).get("execution_path", "unknown")
        )
        if self.usage_store is not None:
            self.usage_store.record(
                company_id=company_id,
                provider="internal",
                model=execution_path,
                operation="search",
                status=(
                    "cache_hit"
                    if result.get("result_cache_hit")
                    else "success"
                ),
            )
            for event in events:
                self.usage_store.record(company_id=company_id, **event)
        return {
            "tracked": self.usage_store is not None,
            "model_requests": len(events),
            "input_tokens": sum(
                event["input_tokens"] for event in events
            ),
            "output_tokens": sum(
                event["output_tokens"] for event in events
            ),
            "total_tokens": sum(
                event["total_tokens"] for event in events
            ),
            "breakdown": events,
        }

    @staticmethod
    def _search_api_usage_events(
        result: dict[str, Any],
    ) -> tuple[SearchApiUsageEvent, ...]:
        events: list[SearchApiUsageEvent] = []
        query_metrics = result.get("query_model_metrics") or {}
        query_attempts = query_metrics.get("attempts") or (
            [query_metrics] if query_metrics.get("model") else []
        )
        for attempt in query_attempts:
            model = str(attempt.get("model") or "")
            if not model:
                continue
            reason = str(attempt.get("reason") or "")
            provider = str(attempt.get("provider") or "")
            if not provider:
                provider = "groq" if model.startswith("groq:") else "google"
            events.append(
                SearchApiUsageEvent(
                    provider=provider,
                    model=model,
                    operation="query_planning",
                    status=str(attempt.get("status") or "success"),
                    api_calls=0 if reason == "missing_api_key" else 1,
                    input_tokens=int(
                        attempt.get("input_tokens", 0) or 0
                    ),
                    output_tokens=int(
                        attempt.get("output_tokens", 0) or 0
                    ),
                    thought_tokens=int(
                        attempt.get("thought_tokens", 0) or 0
                    ),
                    total_tokens=int(
                        attempt.get("total_tokens", 0) or 0
                    ),
                    duration_ms=float(
                        attempt.get("total_ms", 0.0) or 0.0
                    ),
                    failure_reason=reason,
                )
            )

        embedding = result.get("embedding_model_metrics") or {}
        if embedding and not result.get("result_cache_hit"):
            events.append(
                SearchApiUsageEvent(
                    provider="ollama",
                    model=EMBED_MODEL,
                    operation="embedding",
                    status="success",
                    api_calls=1,
                    duration_ms=float(
                        embedding.get("total_ms", 0.0) or 0.0
                    ),
                )
            )

        for attempt in result.get("reranker_attempts") or []:
            reason = str(attempt.get("reason") or "")
            local_rejection = (
                "request budget exhausted" in reason
                or "provider cooldown active" in reason
            )
            usage = attempt.get("usage") or {}
            events.append(
                SearchApiUsageEvent(
                    provider=str(attempt.get("provider") or "unknown"),
                    model=str(attempt.get("model") or ""),
                    operation="reranking",
                    status=str(attempt.get("status") or "success"),
                    api_calls=0 if local_rejection else 1,
                    input_tokens=int(
                        usage.get("input_tokens", 0) or 0
                    ),
                    output_tokens=int(
                        usage.get("output_tokens", 0) or 0
                    ),
                    thought_tokens=int(
                        usage.get("thought_tokens", 0) or 0
                    ),
                    total_tokens=int(
                        usage.get("total_tokens", 0) or 0
                    ),
                    duration_ms=float(
                        attempt.get("duration_ms", 0.0) or 0.0
                    ),
                    failure_reason=reason,
                )
            )
        return tuple(events)

    def record_search_analytics(
        self,
        query: str,
        result: dict[str, Any],
        *,
        duration_ms: float,
        result_count: int,
        total_results: int,
        user_id: str | None = None,
        page_number: int = 1,
        filters: dict[str, Any] | None = None,
    ) -> bool:
        if self.analytics_store is None:
            return False
        query_plan = result.get("query_plan") or {}
        event = SearchAnalyticsEvent(
            company_id=self.company_id or "legacy",
            user_id=user_id,
            query_text=query,
            execution_path=str(
                query_plan.get("execution_path") or "unknown"
            ),
            route_reason=str(query_plan.get("route_reason") or ""),
            page_number=page_number,
            filters=filters or result.get("resolved_filters") or {},
            result_count=result_count,
            total_results=total_results,
            status="success",
            result_cache_hit=bool(result.get("result_cache_hit")),
            plan_cache_hit=bool(result.get("plan_cache_hit")),
            duration_ms=duration_ms,
            api_usage=self._search_api_usage_events(result),
        )
        return self.analytics_store.submit(event)

    def usage_summary(self, month_utc: str | None = None) -> dict[str, Any]:
        if self.usage_store is None:
            raise RuntimeError("Monthly usage tracking is disabled.")
        return self.usage_store.summary(
            self.company_id or "legacy",
            month_utc,
        )

    @staticmethod
    def _page(
        session: SearchSession,
        offset: int,
        page_size: int,
        cached: bool,
    ) -> SearchResponse:
        if offset > len(session.items):
            raise InvalidCursorError("The cursor offset is invalid.")
        end = min(offset + page_size, len(session.items))
        items = session.items[offset:end]
        has_more = end < len(session.items)
        next_cursor = (
            encode_cursor(session.search_id, end)
            if has_more
            else None
        )
        return SearchResponse(
            company_id=session.company_id,
            search_id=session.search_id,
            query=session.query,
            cached=cached,
            items=items,
            interpreted_query=session.interpreted_query,
            applied_filters=session.applied_filters,
            unresolved_filters=session.unresolved_filters,
            timings_ms=session.timings_ms,
            usage=session.usage,
            pagination=PaginationResponse(
                page_size=page_size,
                returned=len(items),
                offset=offset,
                total_results=len(session.items),
                has_more=has_more,
                next_cursor=next_cursor,
            ),
        )

    def close(self) -> None:
        if self.analytics_store is not None:
            self.analytics_store.close()
        close = getattr(self.engine, "close", None)
        if callable(close):
            close()


def product_is_visible(product: dict[str, Any]) -> bool:
    # ads.type is the canonical offer/wanted discriminator. Do not infer
    # visibility from ads.status: the source uses multiple status lifecycles,
    # and valid wanted rows commonly carry status=2.
    return product.get("deleted_at") is None


def public_product(
    product: dict[str, Any],
    *,
    fields: tuple[str, ...] = PUBLIC_PRODUCT_FIELDS,
    field_mapping: dict[str, str] | None = None,
) -> dict[str, Any]:
    mapping = field_mapping or {}
    output = {}
    for public_field in fields:
        source_field = mapping.get(public_field, public_field)
        if source_field in product:
            output[public_field] = product[source_field]
    return output
