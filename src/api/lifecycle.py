"""Application startup, warm-up, and shutdown orchestration."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from api.service import ProductSearchService
from api.tenants import TenantServicePool
from core.rate_limit import TenantRateLimiter
from core.settings import (
    API_ADMIN_LOG_BUFFER_SIZE,
    API_AUTH_ENABLED,
    API_PRELOAD_EMBEDDING,
    API_PRELOAD_RERANKER,
    API_TENANT_CONFIG_DIR,
    API_TENANT_ENGINE_CACHE_SIZE,
    REDIS_ENABLED,
    REDIS_KEY_PREFIX,
    REDIS_URL,
    RERANK_MODEL,
    USAGE_DB_PATH,
    USAGE_TRACKING_ENABLED,
)
from core.tenant_config import TenantRegistry, load_tenant_registry
from observability.admin_logs import AdminLogBuffer
from providers.ollama import preload_ollama_embedding
from search.engine import ProductSearchEngine
from storage.redis import create_redis_cache
from storage.usage import MonthlyUsageStore
from tenants.registry import tenant_logger_names

LOGGER = logging.getLogger("uvicorn.error")
CAPTURED_LOGGER_NAMES = (
    "uvicorn.error",
    "uvicorn.access",
    *tenant_logger_names(),
)


@dataclass
class RuntimeResources:
    engine: ProductSearchEngine | None
    pool: TenantServicePool | None
    redis_cache: Any
    usage_store: MonthlyUsageStore | None
    owns_usage_store: bool

    def close(self) -> None:
        if self.pool is not None:
            self.pool.close()
        if self.engine is not None:
            self.engine.close()
        if self.redis_cache is not None:
            self.redis_cache.close()
        if self.owns_usage_store and self.usage_store is not None:
            self.usage_store.close()


def _configure_runtime(
    application: FastAPI,
    *,
    engine_factory: Callable[[], ProductSearchEngine],
    service: ProductSearchService | None,
    tenant_registry: TenantRegistry | None,
    tenant_engine_factory,
    compatibility_factory,
    rate_limiter: TenantRateLimiter | None,
    usage_store: MonthlyUsageStore | None,
) -> RuntimeResources:
    active_usage_store = usage_store
    owns_usage_store = False
    if active_usage_store is None and USAGE_TRACKING_ENABLED:
        active_usage_store = MonthlyUsageStore(USAGE_DB_PATH)
        owns_usage_store = True

    application.state.usage_store = active_usage_store
    application.state.check_ollama_readiness = service is None
    application.state.readiness_cache_lock = threading.Lock()
    tenant_mode = service is None and (tenant_registry is not None or API_AUTH_ENABLED)
    application.state.tenant_mode = tenant_mode

    engine = None
    pool = None
    redis_cache = None
    if tenant_mode:
        redis_cache = create_redis_cache(REDIS_ENABLED, REDIS_URL, REDIS_KEY_PREFIX)
        registry = tenant_registry or load_tenant_registry(
            API_TENANT_CONFIG_DIR,
            require_api_keys=True,
        )
        pool = TenantServicePool(
            registry,
            shared_cache=redis_cache,
            max_services=API_TENANT_ENGINE_CACHE_SIZE,
            engine_factory=tenant_engine_factory,
            compatibility_factory=compatibility_factory,
            usage_store=active_usage_store,
        )
        application.state.tenant_registry = registry
        application.state.tenant_service_pool = pool
        application.state.rate_limiter = rate_limiter or TenantRateLimiter(redis_cache)
        application.state.search_service = None
        application.state.pgvector_prewarm = pool.prewarm_pgvector_indexes()
    elif service is None:
        redis_cache = create_redis_cache(REDIS_ENABLED, REDIS_URL, REDIS_KEY_PREFIX)
        engine = engine_factory()
        engine.set_shared_plan_cache(redis_cache)
        application.state.search_service = ProductSearchService(
            engine,
            usage_store=active_usage_store,
        )
    else:
        application.state.search_service = service

    return RuntimeResources(
        engine=engine,
        pool=pool,
        redis_cache=redis_cache,
        usage_store=active_usage_store,
        owns_usage_store=owns_usage_store,
    )


def _preload_models(
    application: FastAPI,
    *,
    resources: RuntimeResources,
    service: ProductSearchService | None,
    preload_models: bool | None,
) -> tuple[bool, bool]:
    preload_reranker = (
        API_PRELOAD_RERANKER if preload_models is None else preload_models
    )
    preload_embedding = (
        API_PRELOAD_EMBEDDING if preload_models is None else preload_models
    )
    if preload_reranker:
        LOGGER.info("Initializing the configured reranker chain...")
        search_service = application.state.search_service
        load_ms = (
            resources.pool.preload_reranker()
            if resources.pool is not None
            else search_service.warmup()
        )
        ranker = (
            resources.pool.shared_reranker.ranker
            if resources.pool is not None
            else search_service.engine.ranker
        )
        LOGGER.info(
            "Reranker chain ready model_order=%s in %.0f ms.",
            getattr(ranker, "model_label", RERANK_MODEL),
            load_ms,
        )
    if preload_embedding and service is None:
        LOGGER.info("Preloading the Ollama embedding model...")
        embedding_warmup = preload_ollama_embedding()
        if resources.pool is not None:
            resources.pool.embedding_warmup = embedding_warmup
        else:
            application.state.search_service.embedding_warmup = embedding_warmup
        LOGGER.info(
            "Ollama embedding model ready in %.0f ms.",
            embedding_warmup["embedding_model"].get("total_ms", 0.0),
        )
    return preload_reranker, preload_embedding


def _warmup_summary(application: FastAPI, state_name: str) -> str:
    results = getattr(application.state, state_name, {})
    return (
        ",".join(
            f"{company_id}:{result.get('status', 'unknown')}"
            for company_id, result in sorted(results.items())
        )
        or "not_configured"
    )


def _attach_admin_log_buffer(
    application: FastAPI,
) -> tuple[AdminLogBuffer, list[logging.Logger]]:
    admin_log_buffer = AdminLogBuffer(API_ADMIN_LOG_BUFFER_SIZE)
    application.state.admin_log_buffer = admin_log_buffer
    captured_loggers = [logging.getLogger(name) for name in CAPTURED_LOGGER_NAMES]
    for captured_logger in captured_loggers:
        captured_logger.addHandler(admin_log_buffer)
    return admin_log_buffer, captured_loggers


def build_lifespan(
    *,
    engine_factory: Callable[[], ProductSearchEngine],
    service: ProductSearchService | None,
    tenant_registry: TenantRegistry | None,
    tenant_engine_factory,
    compatibility_factory,
    rate_limiter: TenantRateLimiter | None,
    preload_models: bool | None,
    usage_store: MonthlyUsageStore | None,
):
    """Return the FastAPI lifespan handler for the supplied runtime seams."""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        resources = _configure_runtime(
            application,
            engine_factory=engine_factory,
            service=service,
            tenant_registry=tenant_registry,
            tenant_engine_factory=tenant_engine_factory,
            compatibility_factory=compatibility_factory,
            rate_limiter=rate_limiter,
            usage_store=usage_store,
        )
        preload_reranker, preload_embedding = _preload_models(
            application,
            resources=resources,
            service=service,
            preload_models=preload_models,
        )
        if resources.pool is not None:
            application.state.planner_catalog_prewarm = (
                resources.pool.prewarm_planner_catalogs()
            )
        admin_log_buffer, captured_loggers = _attach_admin_log_buffer(application)
        LOGGER.info(
            "startup_warmup status=complete pgvector=%s reranker=%s "
            "embedding=%s planner_catalog=%s",
            _warmup_summary(application, "pgvector_prewarm"),
            "ready" if preload_reranker else "lazy",
            "ready" if preload_embedding and service is None else "lazy",
            _warmup_summary(application, "planner_catalog_prewarm"),
        )
        try:
            yield
        finally:
            for captured_logger in captured_loggers:
                captured_logger.removeHandler(admin_log_buffer)
            admin_log_buffer.close()
            resources.close()

    return lifespan
