from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from .config import CompanyAnalyticsConfig
from .domain import (
    process_part_a,
    process_part_b,
    process_part_c,
    process_part_d,
)
from .domain.search.records import build_query_records
from .metrics import (
    metric_counts,
    resolve_metric_profiles,
    select_metrics,
)
from .source import AnalyticsDataSource
from .schedule import REFRESH_SCHEDULE
from .store import AnalyticsSnapshotStore

LOGGER = logging.getLogger(__name__)


def _copy_data(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    # Pandas 3 uses copy-on-write. A shallow copy isolates columns added by
    # individual report modules without doubling every source table in memory.
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


def _dashboard_sections(
    reports: dict[str, dict[str, Any]],
    profile: dict[str, tuple[str, ...]],
) -> dict[str, dict[str, Any]]:
    return {
        module: select_metrics(reports[module], metric_names)
        for module, metric_names in profile.items()
        if metric_names
    }


def _dashboard_modules(
    sections: dict[str, dict[str, Any]],
) -> list[str]:
    modules = list(sections)
    insert_at = 1 if modules and modules[0] == "search_intelligence" else 0
    modules.insert(insert_at, "individual_queries")
    return modules


class AnalyticsRefreshService:
    def __init__(
        self,
        source: AnalyticsDataSource,
        store: AnalyticsSnapshotStore,
    ):
        self.source = source
        self.store = store

    def refresh(self, company: CompanyAnalyticsConfig) -> dict[str, Any]:
        run_id = self.store.begin_refresh(company.company_id)
        source_rows: dict[str, int] = {}
        try:
            data = self.source.load(company)
            source_rows = {name: int(len(frame)) for name, frame in data.items()}
            generated_at = datetime.now(UTC).isoformat()

            LOGGER.info(
                "Building search intelligence company=%s",
                company.company_id,
            )
            search_intelligence = process_part_a(_copy_data(data))
            LOGGER.info(
                "Building API performance company=%s",
                company.company_id,
            )
            api_performance = process_part_b(_copy_data(data))
            LOGGER.info(
                "Building deep analytics company=%s",
                company.company_id,
            )
            deep_analytics = process_part_c(_copy_data(data))
            LOGGER.info(
                "Building market intelligence company=%s",
                company.company_id,
            )
            market_intelligence = process_part_d(_copy_data(data))
            query_payload = build_query_records(_copy_data(data))

            reports = {
                "search_intelligence": search_intelligence,
                "api_performance": api_performance,
                "deep_analytics": deep_analytics,
                "market_intelligence": market_intelligence,
            }
            company_profile, internal_profile = resolve_metric_profiles(
                company.company_metric_profile,
                company.internal_metric_profile,
            )
            company_sections = _dashboard_sections(
                reports,
                company_profile,
            )
            internal_sections = _dashboard_sections(
                reports,
                internal_profile,
            )

            query_pairs = []
            for internal_record in query_payload["queries"]:
                internal_record["created_at"] = _normalize_created_at(
                    internal_record.get("created_at")
                )
                company_record = _company_query_record(internal_record)
                query_pairs.append((company_record, internal_record))

            metadata = {
                "schema_version": "2.0",
                "company_id": company.company_id,
                "generated_at": generated_at,
                "refresh_schedule": REFRESH_SCHEDULE,
                "source_rows": source_rows,
            }
            company_dashboard = {
                "metadata": {
                    **metadata,
                    "audience": "company",
                    "modules": _dashboard_modules(company_sections),
                    "metric_counts": metric_counts(company_profile),
                    "individual_query_count": len(query_pairs),
                },
                **company_sections,
            }
            internal_dashboard = {
                "metadata": {
                    **metadata,
                    "audience": "internal",
                    "modules": _dashboard_modules(internal_sections),
                    "metric_counts": metric_counts(internal_profile),
                    "individual_query_count": len(query_pairs),
                },
                **internal_sections,
            }
            watermarks = []
            for name in ("search_history", "api_usage", "ads", "users"):
                frame = data.get(name)
                if frame is None or frame.empty or "created_at" not in frame:
                    continue
                parsed = pd.to_datetime(
                    frame["created_at"],
                    utc=True,
                    errors="coerce",
                ).dropna()
                if not parsed.empty:
                    watermarks.append(parsed.max())
            source_watermark = max(watermarks).isoformat() if watermarks else None
            version = self.store.publish(
                run_id=run_id,
                company_id=company.company_id,
                generated_at=generated_at,
                source_watermark=source_watermark,
                source_rows=source_rows,
                company_dashboard=company_dashboard,
                internal_dashboard=internal_dashboard,
                query_records=query_pairs,
            )
        except Exception as exc:
            self.store.fail_refresh(
                run_id,
                error_type=type(exc).__name__,
                source_rows=source_rows,
            )
            LOGGER.exception(
                "Analytics refresh failed company=%s error_type=%s",
                company.company_id,
                type(exc).__name__,
            )
            raise
        return {
            "status": "complete",
            "company_id": company.company_id,
            "run_id": run_id,
            "snapshot_version": version,
            "generated_at": generated_at,
            "source_watermark": source_watermark,
            "source_rows": source_rows,
            "query_records": len(query_pairs),
        }
