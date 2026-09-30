"""Reusable marketplace analytics computation selected by tenant contracts."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

import pandas as pd

from analytics_service.adapters_base import PassthroughCompanyAnalyticsAdapter
from analytics_service.contracts import AnalyticsComputation
from analytics_service.filters import DashboardFilters
from analytics_service.memory import log_refresh_memory
from analytics_service.source import SqlAnalyticsStage

from .dashboard_filters import (
    available_dashboard_filters,
    build_dashboard_overview,
)
from .domain import (
    build_company_business_insights,
    build_company_overview,
    process_part_a,
    process_part_b,
    process_part_c,
    process_part_d,
)
from .domain.scope import MarketplaceScope
from .domain.search.records import iter_query_records
from .metric_catalog import build_metric_definitions
from .query_spool import QueryRecordSpool


def _copy_data(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    return {name: frame.copy(deep=False) for name, frame in data.items()}


def _normalize_created_at(value: Any) -> str:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return str(value or "")
    return parsed.isoformat()


def _company_query_record(record: dict[str, Any]) -> dict[str, Any]:
    api = dict(record.get("api") or {})
    return {
        "search_id": record.get("search_id"),
        "request_id": record.get("request_id"),
        "query": record.get("query"),
        "normalized_query": record.get("normalized_query"),
        "request_kind": record.get("request_kind"),
        "created_at": record.get("created_at"),
        "word_count": record.get("word_count"),
        "categories": list(record.get("categories") or []),
        "brands": list(record.get("brands") or []),
        "locations": list(record.get("locations") or []),
        "language": record.get("language"),
        "rental_duration": record.get("rental_duration"),
        "flags": dict(record.get("flags") or {}),
        "outcome": record.get("outcome"),
        "filters": dict(record.get("filters") or {}),
        "search": {
            "status": api.get("status"),
            "result_count": api.get("result_count"),
            "total_results": api.get("total_results"),
        },
        **(
            {"ai_enrichment": record["ai_enrichment"]}
            if "ai_enrichment" in record
            else {}
        ),
    }


def _stream_query_pairs(
    records: Iterable[dict[str, Any]],
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Build audience payloads lazily from the disk-backed record spool."""

    for internal_record in records:
        yield _company_query_record(internal_record), internal_record


def _build_spool(data: dict[str, pd.DataFrame]) -> QueryRecordSpool:
    records = QueryRecordSpool()
    try:
        for internal_record in iter_query_records(data):
            internal_record["created_at"] = _normalize_created_at(
                internal_record.get("created_at")
            )
            records.append(internal_record)
        log_refresh_memory("query_spool_complete")
        return records
    except Exception:
        records.close()
        raise


class MarketplaceAnalyticsAdapter(PassthroughCompanyAnalyticsAdapter):
    scope: MarketplaceScope
    marketplace_name: str

    def build_computation(
        self,
        data: dict[str, pd.DataFrame],
        modules: frozenset[str],
    ) -> AnalyticsComputation:
        unsupported = modules - set(self.analytics_contract.available_metrics)
        if unsupported:
            raise ValueError(
                "Unsupported analytics computation modules: "
                + ", ".join(sorted(unsupported))
            )
        reports: dict[str, dict[str, Any]] = {}
        if "search_intelligence" in modules:
            reports["search_intelligence"] = process_part_a(_copy_data(data))
            log_refresh_memory("part_a_complete")
        if "api_performance" in modules:
            reports["api_performance"] = process_part_b(_copy_data(data))
            log_refresh_memory("part_b_complete")
        if "deep_analytics" in modules:
            reports["deep_analytics"] = process_part_c(
                _copy_data(data), scope=self.scope
            )
            log_refresh_memory("part_c_complete")
        if "market_intelligence" in modules:
            reports["market_intelligence"] = process_part_d(
                _copy_data(data), scope=self.scope
            )
            log_refresh_memory("part_d_complete")

        records = _build_spool(data)
        try:
            return self._finish_computation(data, modules, reports, records)
        except Exception:
            records.close()
            raise

    def _finish_computation(
        self,
        data: dict[str, pd.DataFrame],
        modules: frozenset[str],
        reports: dict[str, dict[str, Any]],
        records: QueryRecordSpool,
    ) -> AnalyticsComputation:
        if "search_intelligence" in modules:
            reports["search_intelligence"].update(
                build_company_business_insights(
                    data,
                    records,
                    scope=self.scope,
                    marketplace_name=self.marketplace_name,
                )
            )
        company_overview = build_company_overview(
            data,
            records,
            scope=self.scope,
        )
        return AnalyticsComputation(
            reports=reports,
            query_pairs=_stream_query_pairs(records),
            query_record_count=len(records),
            company_overview=company_overview,
            cleanup=records.close,
        )

    def build_staged_computation(
        self,
        stage: SqlAnalyticsStage,
        modules: frozenset[str],
        *,
        normalize: Any,
    ) -> AnalyticsComputation:
        """Load only the source group needed for each report phase."""

        unsupported = modules - set(self.analytics_contract.available_metrics)
        if unsupported:
            raise ValueError(
                "Unsupported analytics computation modules: "
                + ", ".join(sorted(unsupported))
            )
        history = stage.load(
            ("search_history", "api_usage", "location", "sub_categories"),
            normalize=normalize,
        )
        log_refresh_memory("history_loaded")
        reports: dict[str, dict[str, Any]] = {}
        if "search_intelligence" in modules:
            reports["search_intelligence"] = process_part_a(_copy_data(history))
            log_refresh_memory("part_a_complete")
        if "api_performance" in modules:
            reports["api_performance"] = process_part_b(_copy_data(history))
            log_refresh_memory("part_b_complete")
        records = _build_spool(history)
        del history

        try:
            catalogue = stage.load(
                tuple(
                    name
                    for name in stage.company.dataset_specs
                    if name not in {"search_history", "api_usage"}
                ),
                normalize=normalize,
            )
            log_refresh_memory("catalogue_loaded")
            if "deep_analytics" in modules:
                reports["deep_analytics"] = process_part_c(
                    _copy_data(catalogue), scope=self.scope
                )
                log_refresh_memory("part_c_complete")
            if "market_intelligence" in modules:
                reports["market_intelligence"] = process_part_d(
                    _copy_data(catalogue), scope=self.scope
                )
                log_refresh_memory("part_d_complete")
            return self._finish_computation(catalogue, modules, reports, records)
        except Exception:
            records.close()
            raise

    def metric_definitions(
        self,
        reports: dict[str, dict[str, Any]],
        profile: dict[str, tuple[str, ...]],
        *,
        audience: str,
        source_rows: dict[str, int],
    ) -> dict[str, dict[str, Any]]:
        return build_metric_definitions(
            reports,
            profile,
            audience=audience,
            source_rows=source_rows,
            marketplace_name=self.marketplace_name,
        )

    def dashboard_overview(
        self,
        records: list[dict[str, Any]],
        *,
        internal: bool,
        filters: DashboardFilters,
        timezone_name: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return build_dashboard_overview(
            records,
            internal=internal,
            filters=filters,
            timezone_name=timezone_name,
            now=now,
        )

    def available_dashboard_filters(
        self, records: Iterable[dict[str, Any]], *, internal: bool
    ) -> dict[str, Any]:
        return available_dashboard_filters(records, internal=internal)
