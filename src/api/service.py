import json
import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.contracts import (
    PUBLIC_PRODUCT_FIELDS,
    HealthResponse,
    InvalidCursorError,
    PaginationResponse,
    SearchCapacityError,
    SearchRequest,
    SearchResponse,
    SearchSession,
    SearchSessionStore,
    decode_cursor,
    encode_cursor,
)
from api.monitoring import monitor_timeline
from api.telemetry import (
    EXPLICIT_TIMING_FIELDS,
    TIMING_SECONDS_FIELDS,
    analytics_filter_context,
    search_api_usage_events,
    search_timings_ms,
)
from core.settings import (
    API_MAX_RESULTS,
    API_SEARCH_SLOT_TIMEOUT_SECONDS,
    API_TENANT_MAX_CONCURRENT_SEARCHES,
    APP_NAME,
    RERANK_MODEL,
)
from ingestion.state import read_ingestion_state
from search.engine import ProductSearchEngine
from storage.search_analytics import (
    SearchAnalyticsEvent,
    SearchAnalyticsStore,
    SearchApiUsageEvent,
)
from storage.usage import MonthlyUsageStore

LOGGER = logging.getLogger("uvicorn.error")


class ProductSearchService:
    _TIMING_SECONDS_FIELDS = TIMING_SECONDS_FIELDS
    _EXPLICIT_TIMING_FIELDS = EXPLICIT_TIMING_FIELDS

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
        ingestion_state_path: Path | str | None = None,
    ):
        if max_results <= 0:
            raise ValueError("Maximum results must be greater than zero.")
        if max_concurrent_searches <= 0:
            raise ValueError("Maximum concurrent searches must be greater than zero.")
        if search_slot_timeout_seconds <= 0:
            raise ValueError("Search slot timeout must be greater than zero.")
        self.engine = engine
        self.sessions = sessions if sessions is not None else SearchSessionStore()
        self.max_results = max_results
        self.company_id = company_id
        self.public_fields = tuple(public_fields)
        self.field_mapping = dict(field_mapping or {})
        self.usage_store = usage_store
        self.analytics_store = analytics_store
        self.search_slot_timeout_seconds = search_slot_timeout_seconds
        self.ingestion_state_path = (
            Path(ingestion_state_path) if ingestion_state_path is not None else None
        )
        self._engine_lock = threading.Lock()
        self._search_slots = threading.BoundedSemaphore(max_concurrent_searches)
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
        ingestion_state = read_ingestion_state(self.ingestion_state_path)
        components["ingestion"] = {
            "ok": ingestion_state is None,
            "status": (
                "ready"
                if ingestion_state is None
                else ingestion_state.get("status", "invalid")
            ),
        }
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
                pool_maintenance = database_pool.validate_idle_connections()
                with database_pool.connection() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT 1")
                        cursor.fetchone()
                components["database"] = {
                    "ok": True,
                    "idle_pool": pool_maintenance,
                }
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
            events = [event for event in events if event.get("status") == event_status]
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
        return monitor_timeline(result, duration_ms)

    @contextmanager
    def search_capacity_slot(self):
        """Bound the complete tenant request, including planner and hydration."""
        ingestion_state = read_ingestion_state(self.ingestion_state_path)
        if ingestion_state is not None:
            raise SearchCapacityError("Search index is being refreshed; retry shortly.")
        acquired = self._search_slots.acquire(timeout=self.search_slot_timeout_seconds)
        if not acquired:
            with self._monitor_lock:
                self._monitor_rejected += 1
            raise SearchCapacityError("Search capacity is busy; retry shortly.")
        try:
            yield
        finally:
            self._search_slots.release()

    def run_engine_search(
        self,
        query: str,
        *,
        capacity_acquired: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        ingestion_state = read_ingestion_state(self.ingestion_state_path)
        if ingestion_state is not None:
            raise SearchCapacityError("Search index is being refreshed; retry shortly.")
        started = time.perf_counter()
        timestamp = datetime.now(UTC).isoformat()
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
            if capacity_acquired:
                result = self.engine.search(query, **kwargs)
            else:
                with self.search_capacity_slot():
                    result = self.engine.search(query, **kwargs)
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
                        float(result.get("related_tail_seconds", 0.0)) * 1000,
                        3,
                    ),
                    "result_cache": round(
                        float(result.get("result_cache_seconds", 0.0)) * 1000,
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
                if result.get("retrieval_degraded") or result.get("reranker_degraded"):
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
            "timestamp_utc": datetime.now(UTC).isoformat(),
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
            cached = bool(session.interpreted_query.get("result_cache_hit"))
        else:
            search_id, offset = decode_cursor(request.cursor or "")
            session = self.sessions.get(search_id)
            if session.company_id != self.company_id:
                raise InvalidCursorError("The cursor is invalid for this company.")
            cached = True
        return self._page(session, offset, request.page_size, cached)

    def _start_search(self, query: str) -> SearchSession:
        request_started = time.perf_counter()
        result = self.run_engine_search(query, limit=self.max_results)
        engine_total_ms = float(result.get("_service_total_ms", 0.0))
        query_plan = result["query_plan"]
        usage_started = time.perf_counter()
        usage = self._record_usage(result)
        usage_recording_ms = (time.perf_counter() - usage_started) * 1000
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
                "retrieval_degraded": bool(result.get("retrieval_degraded")),
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
            "total": engine_total_ms,
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
        response_mapping_started = time.perf_counter()
        items = [
            public_product(
                product,
                fields=self.public_fields,
                field_mapping=self.field_mapping,
            )
            for product in result["products"]
            if product_is_visible(product)
        ]
        response_mapping_ms = (time.perf_counter() - response_mapping_started) * 1000
        session_storage_started = time.perf_counter()
        session = self.sessions.create(
            query=query,
            items=items,
            interpreted_query=interpreted_query,
            applied_filters=result["resolved_filters"],
            unresolved_filters=result["unresolved_filters"],
            timings_ms=timings_ms,
            usage=usage,
            company_id=self.company_id,
        )
        session_storage_ms = (time.perf_counter() - session_storage_started) * 1000
        total_server_ms = (time.perf_counter() - request_started) * 1000
        timings_ms["total"] = total_server_ms
        result["_analytics_timings_ms"] = {
            "engine_total_ms": engine_total_ms,
            "usage_recording_ms": usage_recording_ms,
            "response_mapping_ms": response_mapping_ms,
            "session_storage_ms": session_storage_ms,
        }
        self.record_search_analytics(
            query,
            result,
            duration_ms=total_server_ms,
            result_count=len(items),
            total_results=len(result.get("product_ids") or items),
        )
        return session

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
                        else ("groq" if model.startswith("groq:") else "google")
                    ),
                    "model": model,
                    "operation": "query_planning",
                    "status": str(attempt.get("status") or "success"),
                    "input_tokens": int(attempt.get("input_tokens", 0) or 0),
                    "output_tokens": int(attempt.get("output_tokens", 0) or 0),
                    "total_tokens": int(attempt.get("total_tokens", 0) or 0),
                }
            )
        for attempt in result.get("reranker_attempts") or []:
            provider_name = str(attempt.get("provider") or "")
            provider = "voyage" if provider_name.startswith("voyage") else provider_name
            usage = attempt.get("usage") or {}
            events.append(
                {
                    "provider": provider,
                    "model": str(attempt.get("model") or provider_name),
                    "operation": "reranking",
                    "status": str(attempt.get("status") or "success"),
                    "input_tokens": int(usage.get("input_tokens", 0) or 0),
                    "output_tokens": int(usage.get("output_tokens", 0) or 0),
                    "total_tokens": int(usage.get("total_tokens", 0) or 0),
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
                status=("cache_hit" if result.get("result_cache_hit") else "success"),
            )
            for event in events:
                self.usage_store.record(company_id=company_id, **event)
        return {
            "tracked": self.usage_store is not None,
            "model_requests": len(events),
            "input_tokens": sum(event["input_tokens"] for event in events),
            "output_tokens": sum(event["output_tokens"] for event in events),
            "total_tokens": sum(event["total_tokens"] for event in events),
            "breakdown": events,
        }

    @staticmethod
    def _search_api_usage_events(
        result: dict[str, Any],
    ) -> tuple[SearchApiUsageEvent, ...]:
        return search_api_usage_events(result)

    def record_search_analytics(
        self,
        query: str,
        result: dict[str, Any],
        *,
        duration_ms: float,
        result_count: int,
        total_results: int,
    ) -> bool:
        if self.analytics_store is None:
            return False
        query_plan = result.get("query_plan") or {}
        result.setdefault(
            "_analytics_candidate_counts",
            {
                "retrieved_candidates": len(result.get("product_ids") or ()),
                "eligible_candidates": max(int(total_results), 0),
                "hydrated_results": max(int(result_count), 0),
                "returned_results": max(int(result_count), 0),
            },
        )
        timings_ms = self._search_timings_ms(result, duration_ms)
        event = SearchAnalyticsEvent(
            company_id=self.company_id or "legacy",
            query_text=query,
            execution_path=str(query_plan.get("execution_path") or "unknown"),
            result_count=result_count,
            total_results=total_results,
            status="success",
            duration_ms=duration_ms,
            api_usage=self._search_api_usage_events(result),
            plan_cache_hit=bool(result.get("plan_cache_hit")),
            result_cache_hit=bool(result.get("result_cache_hit")),
            timings_ms=timings_ms,
            context=self._analytics_filter_context(result),
        )
        return self.analytics_store.submit(event)

    @staticmethod
    def _analytics_filter_context(result: dict[str, Any]) -> dict[str, Any]:
        return analytics_filter_context(result)

    @classmethod
    def _search_timings_ms(
        cls,
        result: dict[str, Any],
        total_server_ms: float,
    ) -> dict[str, float]:
        return search_timings_ms(
            result,
            total_server_ms,
            seconds_fields=cls._TIMING_SECONDS_FIELDS,
            explicit_fields=cls._EXPLICIT_TIMING_FIELDS,
        )

    def record_search_failure(
        self,
        query: str,
        *,
        duration_ms: float,
        error_type: str,
        execution_path: str = "unknown",
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Durably record an authenticated search-processing failure.

        Only the exception class is retained. Exception messages can contain
        provider responses or other sensitive operational context and must not
        enter tenant telemetry.
        """
        if self.analytics_store is None:
            return False
        normalized_error_type = (error_type or "SearchError")[:255]
        event = SearchAnalyticsEvent(
            company_id=self.company_id or "legacy",
            query_text=query,
            execution_path=execution_path,
            result_count=0,
            total_results=0,
            status="failure",
            duration_ms=max(float(duration_ms), 0.0),
            timings_ms={"total_server_ms": round(max(float(duration_ms), 0.0), 3)},
            api_usage=(
                SearchApiUsageEvent(
                    provider="internal",
                    model=execution_path,
                    operation="search",
                    status="failure",
                    api_calls=0,
                    duration_ms=max(float(duration_ms), 0.0),
                    failure_reason=normalized_error_type,
                ),
            ),
            context=dict(context or {}),
        )
        try:
            return self.analytics_store.submit(event)
        except Exception as exc:
            LOGGER.error(
                "search_analytics_failure_submit status=failed "
                "company=%s error_type=%s",
                self.company_id or "legacy",
                type(exc).__name__,
            )
            return False

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
        next_cursor = encode_cursor(session.search_id, end) if has_more else None
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
        compatibility_service = getattr(self, "compatibility_service", None)
        compatibility_close = getattr(compatibility_service, "close", None)
        if callable(compatibility_close):
            compatibility_close()
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
