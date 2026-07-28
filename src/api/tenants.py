import logging
import sys
import threading
import time
from collections import OrderedDict
from typing import Any

from api.service import ProductSearchService
from search.bm25 import PersistentBM25Index
from tenants.gainr.compatibility import GainrCompatibilityService
from search.reranker import SharedReranker
from search.engine import ProductSearchEngine
from core.settings import API_TENANT_ENGINE_CACHE_SIZE
from core.tenant_config import TenantProfile, TenantRegistry
from search.policy_registry import build_search_policy
from storage.usage import MonthlyUsageStore
from storage.vector import get_tenant_vector_collection

LOGGER = logging.getLogger("uvicorn.error")


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
        self.compatibility_factory = (
            compatibility_factory or GainrCompatibilityService
        )
        self.usage_store = usage_store
        self.shared_reranker = SharedReranker()
        self.reranker_load_ms = 0.0
        self.embedding_warmup: dict[str, Any] = {}
        self._services: OrderedDict[str, ProductSearchService] = OrderedDict()
        self._lock = threading.Lock()

    def preload_reranker(self) -> float:
        _ranker, seconds = self.shared_reranker.ensure()
        self.reranker_load_ms = max(self.reranker_load_ms, seconds * 1000)
        return seconds * 1000

    def prewarm_pgvector_indexes(self) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for company_id, profile in self.registry.profiles.items():
            if not profile.storage.pgvector_prewarm_on_startup:
                continue
            started = time.perf_counter()
            try:
                collection = _api_dependency(
                    "get_tenant_vector_collection",
                    get_tenant_vector_collection,
                )(
                    profile,
                    create=False,
                )
                result = collection.prewarm_hnsw_index(
                    mode=profile.storage.pgvector_prewarm_mode
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
                    result.get("table", profile.storage.pgvector_table),
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
        with self._lock:
            existing = self._services.get(company_id)
            if existing is not None:
                self._services.move_to_end(company_id)
                return existing
            profile = self.registry.get(company_id)
            service = self._build_service(profile)
            self._services[company_id] = service
            while len(self._services) > self.max_services:
                _evicted_id, evicted = self._services.popitem(last=False)
                evicted.close()
            return service

    def loaded_services(self) -> dict[str, ProductSearchService]:
        with self._lock:
            return dict(self._services)

    def _build_service(self, profile: TenantProfile) -> ProductSearchService:
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
                    profile.retrieval
                    .semantic_related_tail_requires_explicit_category
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
        service = ProductSearchService(
            engine,
            company_id=profile.company_id,
            public_fields=profile.payload.public_fields,
            field_mapping=profile.payload.field_mapping,
            usage_store=self.usage_store,
        )
        service.reranker_load_ms = self.reranker_load_ms
        service.embedding_warmup = self.embedding_warmup
        service.compatibility_service = None
        if profile.compatibility.adapter == "gainr_legacy":
            service.compatibility_service = self.compatibility_factory(
                profile,
                service,
                self.shared_cache,
            )
        return service

    def close(self) -> None:
        with self._lock:
            services = list(self._services.values())
            self._services.clear()
        for service in services:
            service.close()
