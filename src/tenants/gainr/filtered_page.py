"""Execute a bounded Gainr result page through catalogue or semantic retrieval."""

from __future__ import annotations

import time
from dataclasses import dataclass

from tenants.gainr.models import (
    GainrFilterResultRequest,
)


@dataclass(frozen=True)
class GainrSearchPage:
    rows: list[dict]
    total: int
    route: str
    usage: dict
    analytics_result: dict
    window_limited: bool
    engine_ms: float
    database_ms: float
    eligibility_ms: float
    hydration_ms: float
    usage_ms: float
    trace_id: str
    eligibility_source: str


class GainrPageExecutor:
    def __init__(
        self, profile, product_search_service, repository, indexed_filters, boosted_ids
    ):
        self.profile = profile
        self.product_search_service = product_search_service
        self.repository = repository
        self.indexed_filters = indexed_filters
        self.boosted_ids = boosted_ids

    def execute(
        self,
        request: GainrFilterResultRequest,
        planned: dict,
        effective: dict,
        allowed_ad_types: set[str],
        *,
        page_size: int,
        execution_path: str,
        database_only_filters: bool,
        speculative_embedding_future,
    ) -> GainrSearchPage:
        engine_ms = database_ms = eligibility_ms = hydration_ms = usage_ms = 0.0
        trace_id = "-"
        eligibility_source = "database"
        if execution_path == "deterministic_filter":
            if speculative_embedding_future is not None:
                speculative_embedding_future.cancel()
            database_started = time.perf_counter()
            repository_filters = self.indexed_filters(effective)
            rows, total = self.repository.search_catalog(
                repository_filters,
                request.filter,
                search_term=request.searchTerm,
                page=request.page,
                page_size=page_size,
                sort_order=planned["query_plan"].get("sort_order"),
                allowed_ad_types=allowed_ad_types,
                boosted_ids=self.boosted_ids(),
            )
            database_ms = (time.perf_counter() - database_started) * 1000
            route = "deterministic"
            usage_store = self.product_search_service.usage_store
            usage_started = time.perf_counter()
            if usage_store is not None:
                usage_store.record(
                    company_id=self.profile.company_id,
                    provider="internal",
                    model="deterministic_filter",
                    operation="search",
                    status="success",
                )
            usage_ms = (time.perf_counter() - usage_started) * 1000
            usage = {
                "tracked": usage_store is not None,
                "model_requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "breakdown": [],
            }
            analytics_result = {
                **planned,
                "result_cache_hit": False,
                "embedding_model_metrics": {},
                "reranker_attempts": [],
            }
            window_limited = False
        else:
            engine_started = time.perf_counter()
            result = self.product_search_service.run_engine_search(
                request.searchTerm,
                capacity_acquired=True,
                limit=self.product_search_service.max_results,
                ranking_window=(self.profile.compatibility.semantic_ranked_window),
                planned_result=planned,
                resolved_filters=effective,
                allowed_ad_types=allowed_ad_types,
                **(
                    {"speculative_embedding_future": (speculative_embedding_future)}
                    if speculative_embedding_future is not None
                    else {}
                ),
                # The compatibility repository checks current eligibility and
                # hydrates the requested 20-row page from the enriched
                # search-ready projection. Avoid the engine's earlier generic
                # MySQL hydration unless a price sort needs complete rows. This
                # removes one remote database round trip from normal semantic
                # searches without changing ranking or live-ad validation.
                hydrate_products=(
                    planned["query_plan"].get("sort_order")
                    in {"price_asc", "price_desc"}
                ),
            )
            engine_ms = (time.perf_counter() - engine_started) * 1000
            boosted_ids = self.boosted_ids()
            if boosted_ids:
                result["product_ids"] = sorted(
                    result.get("product_ids", []),
                    key=lambda product_id: str(product_id) not in boosted_ids,
                )
            trace_id = str(result.get("trace_id") or "-")
            if database_only_filters or not result.get("products"):
                eligibility_source = "ranked_page"
                hydration_started = time.perf_counter()
                rows, total = self.repository.hydrate_ranked_page(
                    result.get("product_ids", []),
                    effective,
                    request.filter,
                    allowed_ad_types,
                    page=request.page,
                    page_size=page_size,
                )
                hydration_ms = (time.perf_counter() - hydration_started) * 1000
            else:
                eligibility_source = "engine_rows"
                eligibility_started = time.perf_counter()
                id_column = self.repository.config.result_id_column
                current_rows = {
                    str(row[id_column]): row
                    for row in result.get("products", [])
                    if row.get(id_column) is not None
                }
                eligible_ids = []
                for product_id in result.get("product_ids", []):
                    row = current_rows.get(str(product_id))
                    if row is None:
                        continue
                    deleted_at = row.get("deleted_at")
                    if deleted_at is not None and str(deleted_at).strip():
                        continue
                    if (
                        allowed_ad_types is not None
                        and str(row.get("type")) not in allowed_ad_types
                    ):
                        continue
                    eligible_ids.append(product_id)
                eligibility_ms = (time.perf_counter() - eligibility_started) * 1000
                total = len(eligible_ids)
                start = (request.page - 1) * page_size
                hydration_started = time.perf_counter()
                rows = self.repository.hydrate_filtered(
                    eligible_ids[start : start + page_size],
                    effective,
                    request.filter,
                    allowed_ad_types,
                )
                hydration_ms = (time.perf_counter() - hydration_started) * 1000
            window_limited = (
                len(result.get("product_ids", []))
                >= self.product_search_service.max_results
            )
            route = execution_path
            usage_started = time.perf_counter()
            usage = self.product_search_service._record_usage(result)
            usage_ms = (time.perf_counter() - usage_started) * 1000
            analytics_result = {
                **result,
                "query_model_metrics": (
                    result.get("query_model_metrics")
                    or planned.get("query_model_metrics")
                    or {}
                ),
                "plan_cache_hit": bool(planned.get("plan_cache_hit")),
            }
        return GainrSearchPage(
            rows=rows,
            total=total,
            route=route,
            usage=usage,
            analytics_result=analytics_result,
            window_limited=window_limited,
            engine_ms=engine_ms,
            database_ms=database_ms,
            eligibility_ms=eligibility_ms,
            hydration_ms=hydration_ms,
            usage_ms=usage_ms,
            trace_id=trace_id,
            eligibility_source=eligibility_source,
        )
