import logging
import sys
import threading
import time
from collections import OrderedDict
from typing import Any

from api.service import ProductSearchService
from core.settings import (
    API_TENANT_ENGINE_CACHE_SIZE,
    SEARCH_ANALYTICS_DELIVERY_MODE,
    SEARCH_ANALYTICS_SPOOL_PATH,
)
from core.tenant_config import TenantProfile, TenantRegistry
from search.bm25 import PersistentBM25Index
from search.engine import ProductSearchEngine
from search.policy_registry import build_search_policy
from search.reranker import SharedReranker
from storage.index_generations import (
    profile_for_slot,
    promote_candidate,
    read_generation_state,
    resolve_generation,
    restore_active_slot,
)
from storage.search_analytics import MySQLSearchAnalyticsStore
from storage.search_analytics_spool import SQLiteSearchAnalyticsSpoolStore
from storage.usage import MonthlyUsageStore
from storage.vector import get_tenant_vector_collection
from tenants.compatibility import build_compatibility_adapter

LOGGER = logging.getLogger("uvicorn.error")
SERVICE_RETIRE_GRACE_SECONDS = 300.0


def _api_dependency(name: str, default):
    """Honor patches made through the long-standing ``api`` import path."""
    api_module = sys.modules.get("api")
    return getattr(api_module, name, default) if api_module is not None else default


class TenantServicePool:
    """Lazily opens isolated tenant search engines with an LRU memory bound."""

    def __init__(
        self,
        registry: TenantRegistry,
        *,
        shared_cache=None,
        max_services: int = API_TENANT_ENGINE_CACHE_SIZE,
        engine_factory=None,
        compatibility_factory=None,
        usage_store: MonthlyUsageStore | None = None,
    ):
        if max_services <= 0:
            raise ValueError("max_services must be greater than zero")
        self.registry = registry
        self.shared_cache = shared_cache
        self.max_services = max_services
        self.engine_factory = engine_factory
        self.compatibility_factory = compatibility_factory
        self.usage_store = usage_store
        self.shared_reranker = SharedReranker()
        self.reranker_load_ms = 0.0
        self.embedding_warmup: dict[str, Any] = {}
        self._services: OrderedDict[str, ProductSearchService] = OrderedDict()
        self._retired_services: list[tuple[float, ProductSearchService]] = []
        self._lock = threading.Lock()
        self._reload_lock = threading.Lock()

    def preload_reranker(self) -> float:
        _ranker, seconds = self.shared_reranker.ensure()
        self.reranker_load_ms = max(self.reranker_load_ms, seconds * 1000)
        return seconds * 1000

    def prewarm_pgvector_indexes(self) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for company_id, profile in self.registry.profiles.items():
            if not profile.storage.pgvector_prewarm_on_startup:
                continue
            resolved = resolve_generation(profile)
            active_profile = resolved.profile
            started = time.perf_counter()
            try:
                collection = _api_dependency(
                    "get_tenant_vector_collection",
                    get_tenant_vector_collection,
                )(
                    active_profile,
                    create=False,
                )
                result = collection.prewarm_hnsw_index(
                    mode=active_profile.storage.pgvector_prewarm_mode
                )
            except Exception as exc:
                result = {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "duration_ms": (time.perf_counter() - started) * 1000,
                }
                LOGGER.warning(
                    "pgvector startup prewarm failed company=%s "
                    "error_type=%s duration_ms=%.0f; continuing startup",
                    company_id,
                    type(exc).__name__,
                    result["duration_ms"],
                )
            else:
                result = {"status": "complete", **result}
                LOGGER.info(
                    "pgvector startup prewarm complete company=%s table=%s "
                    "table_blocks=%d table_bytes=%d index=%s mode=%s "
                    "index_blocks=%d index_bytes=%d duration_ms=%.0f",
                    company_id,
                    result.get("table", active_profile.storage.pgvector_table),
                    result.get("table_blocks", 0),
                    result.get("table_bytes", 0),
                    result["index"],
                    result["mode"],
                    result["blocks"],
                    result["bytes"],
                    result["duration_ms"],
                )
            results[company_id] = result
        return results

    def prewarm_planner_catalogs(self) -> dict[str, dict[str, Any]]:
        """Build and retain planner catalogs before the first API request."""
        results: dict[str, dict[str, Any]] = {}
        profiles = [
            profile
            for profile in self.registry.profiles.values()
            if profile.storage.pgvector_prewarm_on_startup
        ][: self.max_services]
        for profile in profiles:
            started = time.perf_counter()
            try:
                service = self.get(profile.company_id)
                value_index = service.engine.filter_value_index
                pattern_count = sum(
                    len(getattr(values, "match_patterns", ()))
                    for values in value_index.values()
                )
            except Exception as exc:
                result = {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "duration_ms": (time.perf_counter() - started) * 1000,
                }
                LOGGER.warning(
                    "planner catalog startup prewarm failed company=%s "
                    "error_type=%s duration_ms=%.0f; continuing startup",
                    profile.company_id,
                    type(exc).__name__,
                    result["duration_ms"],
                )
            else:
                result = {
                    "status": "complete",
                    "patterns": pattern_count,
                    "duration_ms": (time.perf_counter() - started) * 1000,
                }
                LOGGER.info(
                    "planner catalog startup prewarm complete company=%s "
                    "patterns=%d duration_ms=%.0f",
                    profile.company_id,
                    pattern_count,
                    result["duration_ms"],
                )
            results[profile.company_id] = result
        return results

    def get(self, company_id: str) -> ProductSearchService:
        closable: list[ProductSearchService] = []
        with self._lock:
            closable = self._collect_retired_locked()
            existing = self._services.get(company_id)
            if existing is not None:
                self._services.move_to_end(company_id)
                service = existing
            else:
                resolved = resolve_generation(self.registry.get(company_id))
                service = self._build_service(
                    resolved.profile,
                    generation=resolved.generation,
                    generation_slot=resolved.slot,
                )
                self._services[company_id] = service
                while len(self._services) > self.max_services:
                    _evicted_id, evicted = self._services.popitem(last=False)
                    self._retire_locked(evicted)
        for item in closable:
            item.close()
        return service

    def _retire_locked(self, service: ProductSearchService) -> None:
        # ``get`` returns before the request increments the service's active
        # counter. Retiring through the same grace path used by generation
        # swaps prevents another tenant's cache miss from closing that service
        # in this hand-off window or while a long search is still running.
        self._retired_services.append(
            (time.monotonic() + SERVICE_RETIRE_GRACE_SECONDS, service)
        )

    def _collect_retired_locked(self) -> list[ProductSearchService]:
        now = time.monotonic()
        ready = []
        retained = []
        for not_before, service in self._retired_services:
            if not_before <= now and service.monitor_status()["active"] == 0:
                ready.append(service)
            else:
                retained.append((not_before, service))
        self._retired_services = retained
        return ready

    def reload_generation(self, company_id: str) -> dict[str, Any]:
        """Build the selected generation, then atomically route new requests."""
        started = time.perf_counter()
        with self._reload_lock:
            resolved = resolve_generation(self.registry.get(company_id))
            with self._lock:
                current = self._services.get(company_id)
                if (
                    current is not None
                    and getattr(current, "index_generation", None)
                    == resolved.generation
                    and getattr(current, "index_generation_slot", None) == resolved.slot
                ):
                    return {
                        "status": "unchanged",
                        "company_id": company_id,
                        "generation": resolved.generation,
                        "slot": resolved.slot,
                        "duration_ms": (time.perf_counter() - started) * 1000,
                    }

            candidate = self._build_service(
                resolved.profile,
                generation=resolved.generation,
                generation_slot=resolved.slot,
            )
            readiness = candidate.readiness()
            if not readiness["ok"]:
                candidate.close()
                raise RuntimeError("Candidate index generation is not ready.")

            with self._lock:
                previous = self._services.get(company_id)
                self._services[company_id] = candidate
                self._services.move_to_end(company_id)
                if previous is not None:
                    # A request can obtain the previous service immediately
                    # before the swap and increment its active counter just
                    # afterwards. A grace period avoids closing that lease.
                    self._retire_locked(previous)
            return {
                "status": "reloaded",
                "company_id": company_id,
                "generation": resolved.generation,
                "slot": resolved.slot,
                "previous_generation": (
                    getattr(previous, "index_generation", None)
                    if previous is not None
                    else None
                ),
                "duration_ms": (time.perf_counter() - started) * 1000,
            }

    def activate_candidate(
        self,
        company_id: str,
        *,
        slot: str,
        generation: str,
    ) -> dict[str, Any]:
        """Validate, persist, and hot-swap a ready inactive generation."""
        started = time.perf_counter()
        with self._reload_lock:
            base_profile = self.registry.get(company_id)
            state = read_generation_state(base_profile)
            if slot == state["active_slot"]:
                raise RuntimeError("Candidate slot is already active.")
            slot_state = state["slots"].get(slot, {})
            if slot_state.get("status") != "ready":
                raise RuntimeError("Candidate index generation is not ready.")
            if slot_state.get("generation") != generation:
                raise RuntimeError("Candidate generation does not match the manifest.")

            candidate = self._build_service(
                profile_for_slot(base_profile, slot),
                generation=generation,
                generation_slot=slot,
            )
            readiness = candidate.readiness()
            if not readiness["ok"]:
                candidate.close()
                raise RuntimeError("Candidate index generation is not ready.")

            with self._lock:
                previous = self._services.get(company_id)
                try:
                    promote_candidate(
                        base_profile,
                        slot=slot,
                        generation=generation,
                    )
                except Exception:
                    candidate.close()
                    raise
                self._services[company_id] = candidate
                self._services.move_to_end(company_id)
                if previous is not None:
                    self._retire_locked(previous)
            return {
                "status": "promoted",
                "company_id": company_id,
                "generation": generation,
                "slot": slot,
                "previous_generation": (
                    getattr(previous, "index_generation", None)
                    if previous is not None
                    else None
                ),
                "duration_ms": (time.perf_counter() - started) * 1000,
            }

    def rollback_generation(self, company_id: str) -> dict[str, Any]:
        """Hot-swap to the recorded previous slot without stopping the API."""
        started = time.perf_counter()
        with self._reload_lock:
            base_profile = self.registry.get(company_id)
            state = read_generation_state(base_profile)
            active_slot = str(state["active_slot"])
            previous_slot = state.get("previous_slot")
            if previous_slot not in {"a", "b"} or previous_slot == active_slot:
                raise RuntimeError("No previous index generation is available.")
            previous_state = state["slots"].get(previous_slot, {})
            previous_generation = previous_state.get("generation")
            if not previous_generation:
                raise RuntimeError("Previous index generation is not initialized.")

            replacement = self._build_service(
                profile_for_slot(base_profile, str(previous_slot)),
                generation=str(previous_generation),
                generation_slot=str(previous_slot),
            )
            readiness = replacement.readiness()
            if not readiness["ok"]:
                replacement.close()
                raise RuntimeError("Previous index generation is not ready.")

            with self._lock:
                current = self._services.get(company_id)
                try:
                    restore_active_slot(base_profile, str(previous_slot))
                except Exception:
                    replacement.close()
                    raise
                self._services[company_id] = replacement
                self._services.move_to_end(company_id)
                if current is not None:
                    self._retire_locked(current)
            return {
                "status": "rolled_back",
                "company_id": company_id,
                "generation": str(previous_generation),
                "slot": str(previous_slot),
                "previous_generation": (
                    getattr(current, "index_generation", None)
                    if current is not None
                    else None
                ),
                "duration_ms": (time.perf_counter() - started) * 1000,
            }

    def loaded_services(self) -> dict[str, ProductSearchService]:
        with self._lock:
            return dict(self._services)

    def _build_service(
        self,
        profile: TenantProfile,
        *,
        generation: str = "legacy-a",
        generation_slot: str = "a",
    ) -> ProductSearchService:
        if self.engine_factory is None:
            collection = _api_dependency(
                "get_tenant_vector_collection",
                get_tenant_vector_collection,
            )(
                profile,
                create=False,
            )
            bm25_index = PersistentBM25Index(profile.storage.bm25_path)
            engine = ProductSearchEngine(
                collection=collection,
                bm25_index=bm25_index,
                shared_plan_cache=self.shared_cache,
                company_id=profile.company_id,
                mysql_config=profile.database,
                shared_reranker=self.shared_reranker,
                close_bm25_index=True,
                planner_enabled=profile.planner_enabled,
                planner_prompt_context=profile.planner_prompt_context,
                planner_query_aliases=profile.planner_query_aliases,
                vector_post_filter_metadata=(
                    "adaptive"
                    if profile.retrieval.adaptive_vector_post_filter_metadata
                    else False
                ),
                semantic_related_tail_enabled=(
                    profile.retrieval.semantic_related_tail_enabled
                ),
                semantic_related_tail_requires_explicit_category=(
                    profile.retrieval.semantic_related_tail_requires_explicit_category
                ),
                reranker_relative_score_floor=(
                    profile.retrieval.reranker_relative_score_floor
                ),
                reranker_min_score_by_provider=(
                    profile.retrieval.reranker_min_score_by_provider
                ),
                search_policy=build_search_policy(profile.search_policy),
            )
        else:
            engine = self.engine_factory(
                profile,
                self.shared_cache,
                self.shared_reranker,
            )
        engine.index_generation = generation
        analytics_store = None
        if profile.analytics.enabled:
            if SEARCH_ANALYTICS_DELIVERY_MODE == "daily_spool":
                analytics_store = SQLiteSearchAnalyticsSpoolStore(
                    SEARCH_ANALYTICS_SPOOL_PATH,
                    company_id=profile.company_id,
                    queue_capacity=profile.analytics.queue_capacity,
                )
            else:
                analytics_store = MySQLSearchAnalyticsStore(
                    profile.database,
                    company_id=profile.company_id,
                    search_history_table=(profile.analytics.search_history_table),
                    api_usage_table=profile.analytics.api_usage_table,
                    queue_capacity=profile.analytics.queue_capacity,
                )
        service = ProductSearchService(
            engine,
            company_id=profile.company_id,
            public_fields=profile.payload.public_fields,
            field_mapping=profile.payload.field_mapping,
            usage_store=self.usage_store,
            analytics_store=analytics_store,
            ingestion_state_path=(
                profile.storage.bm25_path.parent / ".ingestion-state.json"
            ),
        )
        service.reranker_load_ms = self.reranker_load_ms
        service.embedding_warmup = self.embedding_warmup
        service.index_generation = generation
        service.index_generation_slot = generation_slot
        service.compatibility_service = None
        if profile.compatibility.adapter:
            service.compatibility_service = (
                self.compatibility_factory(
                    profile,
                    service,
                    self.shared_cache,
                )
                if self.compatibility_factory is not None
                else build_compatibility_adapter(
                    profile.compatibility.adapter,
                    profile,
                    service,
                    self.shared_cache,
                )
            )
        return service

    def close(self) -> None:
        with self._lock:
            services = list(self._services.values())
            services.extend(service for _, service in self._retired_services)
            self._services.clear()
            self._retired_services.clear()
        for service in services:
            service.close()
