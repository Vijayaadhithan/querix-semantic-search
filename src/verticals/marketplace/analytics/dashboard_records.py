"""Normalize dashboard records and apply audience-independent filters."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from analytics_service.filters import (
    PERIOD_OPTIONS,
    DashboardFilters,
)

_PERIOD_DELTAS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}

_SUCCESS_STATUSES = {"success", "successful", "ok", "cache_hit"}


def validate_timezone(timezone_name: str) -> str:
    normalized = str(timezone_name or "UTC").strip() or "UTC"
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown analytics timezone {timezone_name!r}") from exc
    return normalized


def _aware_boundary(value: datetime | None, timezone: ZoneInfo) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone)
    return value.astimezone(UTC)


def _record_datetime(record: dict[str, Any]) -> datetime | None:
    raw = str(record.get("created_at") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalized_choice(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _resolve_window(
    filters: DashboardFilters,
    *,
    timezone: ZoneInfo,
    now: datetime | None,
) -> tuple[str, datetime | None, datetime | None]:
    period = str(filters.period or "all").strip().casefold()
    if period not in PERIOD_OPTIONS:
        raise ValueError(
            "Dashboard period must be one of: " + ", ".join(PERIOD_OPTIONS)
        )
    created_from = _aware_boundary(filters.created_from, timezone)
    created_to = _aware_boundary(filters.created_to, timezone)
    if created_from is not None or created_to is not None:
        if period not in {"all", "custom"}:
            raise ValueError(
                "Custom from/to boundaries cannot be combined with a fixed period"
            )
        period = "custom"
    elif period == "custom":
        raise ValueError("Custom dashboard period requires from and/or to")
    elif period in _PERIOD_DELTAS:
        anchor = now or datetime.now(UTC)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=UTC)
        created_to = anchor.astimezone(UTC)
        created_from = created_to - _PERIOD_DELTAS[period]
    if (
        created_from is not None
        and created_to is not None
        and created_from > created_to
    ):
        raise ValueError("Dashboard from boundary must not be after to")
    return period, created_from, created_to


def _record_categories(record: dict[str, Any]) -> set[str]:
    categories = {
        str(value).strip()
        for value in record.get("categories") or []
        if str(value).strip()
    }
    applied = dict(record.get("filters") or {})
    for name in ("main_category", "subcategory"):
        value = str(applied.get(name) or "").strip()
        if value:
            categories.add(value)
    return categories


def _record_attempts(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        attempt for attempt in record.get("attempts") or [] if isinstance(attempt, dict)
    ]


def _record_request_kind(record: dict[str, Any]) -> str:
    request_kind = str(record.get("request_kind") or "").strip().casefold()
    if request_kind:
        return request_kind
    has_text = bool(str(record.get("normalized_query") or "").strip())
    return "text_search" if has_text else "catalogue_browse"


def _matches(
    record: dict[str, Any],
    *,
    filters: DashboardFilters,
    created_from: datetime | None,
    created_to: datetime | None,
    internal: bool,
    request_scope: str,
) -> bool:
    created_at = _record_datetime(record)
    if created_from is not None and (created_at is None or created_at < created_from):
        return False
    if created_to is not None and (created_at is None or created_at > created_to):
        return False

    request_kind = _record_request_kind(record)
    if request_scope == "text_search" and request_kind != "text_search":
        return False
    if request_scope == "browse" and request_kind not in {
        "filtered_browse",
        "catalogue_browse",
    }:
        return False

    for actual, expected in (
        (record.get("outcome"), filters.outcome),
        (record.get("language"), filters.language),
    ):
        normalized = _normalized_choice(expected)
        if normalized and str(actual or "").casefold() != normalized.casefold():
            return False

    category = _normalized_choice(filters.category)
    if category and category.casefold() not in {
        value.casefold() for value in _record_categories(record)
    }:
        return False

    applied = dict(record.get("filters") or {})
    for name, expected in (
        ("city", filters.city),
        ("target_ad_type", filters.ad_type),
    ):
        normalized = _normalized_choice(expected)
        if normalized and str(applied.get(name) or "").casefold() != (
            normalized.casefold()
        ):
            return False

    if filters.city_id is not None:
        try:
            record_city_id = int(applied.get("city_id"))
        except (TypeError, ValueError):
            return False
        if record_city_id != filters.city_id:
            return False

    if not internal:
        return True

    execution_path = _normalized_choice(filters.execution_path)
    if execution_path:
        performance = dict(record.get("performance") or {})
        if str(performance.get("execution_path") or "").casefold() != (
            execution_path.casefold()
        ):
            return False

    provider = _normalized_choice(filters.provider)
    operation = _normalized_choice(filters.operation)
    if provider or operation:
        matching_attempt = any(
            (
                not provider
                or str(attempt.get("provider") or "").casefold() == provider.casefold()
            )
            and (
                not operation
                or str(attempt.get("operation") or "").casefold()
                == operation.casefold()
            )
            for attempt in _record_attempts(record)
        )
        if not matching_attempt:
            return False
    return True


class _FilteredRecords:
    """Replay a filtered snapshot without retaining matching records."""

    def __init__(
        self,
        records: Iterable[dict[str, Any]],
        *,
        filters: DashboardFilters,
        created_from: datetime | None,
        created_to: datetime | None,
        internal: bool,
        request_scope: str,
    ) -> None:
        self.records = records
        self.filters = filters
        self.created_from = created_from
        self.created_to = created_to
        self.internal = internal
        self.request_scope = request_scope

    def __iter__(self):
        for record in self.records:
            if _matches(
                record,
                filters=self.filters,
                created_from=self.created_from,
                created_to=self.created_to,
                internal=self.internal,
                request_scope=self.request_scope,
            ):
                yield record


def _sorted_values(values: Iterable[Any]) -> list[str]:
    normalized = {str(value).strip() for value in values if str(value or "").strip()}
    return sorted(normalized, key=str.casefold)
