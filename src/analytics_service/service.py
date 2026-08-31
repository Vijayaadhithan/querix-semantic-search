from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .adapters import build_analytics_adapter
from .config import CompanyAnalyticsConfig
from .metrics import (
    metric_counts,
    resolve_metric_profiles,
    select_metrics,
)
from .schedule import REFRESH_SCHEDULE
from .source import AnalyticsDataSource
from .source_schema import DatasetSpec
from .store import AnalyticsSnapshotStore

LOGGER = logging.getLogger(__name__)


def _normalize_source_timestamps(
    data: dict[str, pd.DataFrame],
    *,
    timezone_name: str,
    dataset_specs: Mapping[str, DatasetSpec],
) -> dict[str, pd.DataFrame]:
    """Convert naive source timestamps to naive UTC exactly once."""

    normalized: dict[str, pd.DataFrame] = {}
    tenant_timezone = ZoneInfo(timezone_name)
    for name, original in data.items():
        timestamp_columns = [
            column for column in original.columns if str(column).endswith("_at")
        ]
        if not timestamp_columns:
            normalized[name] = original
            continue
        frame = original.copy(deep=False)
        assumed_timezone = (
            UTC if dataset_specs[name].timestamps_are_utc else tenant_timezone
        )
        for column in timestamp_columns:
            parsed = pd.to_datetime(
                frame[column],
                errors="coerce",
                format="mixed",
            )
            if isinstance(parsed.dtype, pd.DatetimeTZDtype):
                aware = parsed.dt.tz_convert(UTC)
            else:
                aware = parsed.dt.tz_localize(
                    assumed_timezone,
                    ambiguous="NaT",
                    nonexistent="shift_forward",
                ).dt.tz_convert(UTC)
            # Existing report modules use naive datetime arithmetic. Keeping a
            # canonical naive-UTC representation avoids mixed-aware failures
            # while removing the source-session timezone ambiguity.
            frame[column] = aware.dt.tz_localize(None)
        normalized[name] = frame
    return normalized


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
            data = _normalize_source_timestamps(
                self.source.load(company),
                timezone_name=company.timezone,
                dataset_specs=company.dataset_specs,
            )
            source_rows = {name: int(len(frame)) for name, frame in data.items()}
            generated_at = datetime.now(UTC).isoformat()

            adapter = build_analytics_adapter(company.adapter, company)
            contract = adapter.analytics_contract
            company_profile, internal_profile = resolve_metric_profiles(
                company.company_metric_profile,
                company.internal_metric_profile,
                default_company=contract.default_company_metric_profile,
                default_internal=contract.default_internal_metric_profile,
            )
            selected_modules = frozenset(
                module
                for profile in (company_profile, internal_profile)
                for module, names in profile.items()
                if names
            )
            LOGGER.info(
                "Building tenant analytics company=%s adapter=%s modules=%s",
                company.company_id,
                company.adapter,
                ",".join(sorted(selected_modules)),
            )
            computation = adapter.build_computation(data, selected_modules)
            reports = computation.reports
            company_sections = _dashboard_sections(
                reports,
                company_profile,
            )
            internal_sections = _dashboard_sections(
                reports,
                internal_profile,
            )

            query_pairs = computation.query_pairs

            metadata = {
                "schema_version": "3.3",
                "company_id": company.company_id,
                "generated_at": generated_at,
                "source_timezone": company.timezone,
                "normalized_timezone": "UTC",
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
                    "metric_definitions": adapter.metric_definitions(
                        reports,
                        company_profile,
                        audience="company",
                        source_rows=source_rows,
                    ),
                },
                "business_overview": computation.company_overview,
                **company_sections,
            }
            internal_dashboard = {
                "metadata": {
                    **metadata,
                    "audience": "internal",
                    "modules": _dashboard_modules(internal_sections),
                    "metric_counts": metric_counts(internal_profile),
                    "individual_query_count": len(query_pairs),
                    "metric_definitions": adapter.metric_definitions(
                        reports,
                        internal_profile,
                        audience="internal",
                        source_rows=source_rows,
                    ),
                },
                **internal_sections,
            }
            watermarks = []
            for frame in data.values():
                if frame.empty or "created_at" not in frame:
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
