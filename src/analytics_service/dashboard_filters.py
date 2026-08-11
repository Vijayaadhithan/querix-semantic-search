from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PERIOD_OPTIONS = ("24h", "7d", "30d", "90d", "all", "custom")
_PERIOD_DELTAS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}
_SUCCESS_STATUSES = {"success", "successful", "ok", "cache_hit"}


@dataclass(frozen=True, slots=True)
class DashboardFilters:
    period: str = "all"
    created_from: datetime | None = None
    created_to: datetime | None = None
    outcome: str | None = None
    category: str | None = None
    language: str | None = None
    city: str | None = None
    city_id: int | None = None
    ad_type: str | None = None
    execution_path: str | None = None
    provider: str | None = None
    operation: str | None = None


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
    if created_from is not None and created_to is not None:
        if created_from > created_to:
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


def _matches(
    record: dict[str, Any],
    *,
    filters: DashboardFilters,
    created_from: datetime | None,
    created_to: datetime | None,
    internal: bool,
) -> bool:
    created_at = _record_datetime(record)
    if created_from is not None and (created_at is None or created_at < created_from):
        return False
    if created_to is not None and (created_at is None or created_at > created_to):
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


def _sorted_values(values: Iterable[Any]) -> list[str]:
    normalized = {str(value).strip() for value in values if str(value or "").strip()}
    return sorted(normalized, key=str.casefold)


def _available_filters(
    records: list[dict[str, Any]], *, internal: bool
) -> dict[str, Any]:
    categories: set[str] = set()
    cities = []
    city_options = {}
    ad_types = []
    providers = []
    operations = []
    execution_paths = []
    for record in records:
        categories.update(_record_categories(record))
        applied = dict(record.get("filters") or {})
        cities.append(applied.get("city"))
        city_id = applied.get("city_id")
        city_label = str(applied.get("city") or "").strip()
        if city_id is not None:
            try:
                city_options[int(city_id)] = city_label or str(city_id)
            except (TypeError, ValueError):
                pass
        ad_types.append(applied.get("target_ad_type"))
        if internal:
            performance = dict(record.get("performance") or {})
            execution_paths.append(performance.get("execution_path"))
            for attempt in _record_attempts(record):
                providers.append(attempt.get("provider"))
                operations.append(attempt.get("operation"))
    available = {
        "periods": list(PERIOD_OPTIONS),
        "outcomes": _sorted_values(record.get("outcome") for record in records),
        "categories": _sorted_values(categories),
        "languages": _sorted_values(record.get("language") for record in records),
        "cities": _sorted_values(cities),
        "city_options": [
            {"id": city_id, "label": label}
            for city_id, label in sorted(
                city_options.items(),
                key=lambda item: item[1].casefold(),
            )
        ],
        "ad_types": _sorted_values(ad_types),
    }
    if internal:
        available.update(
            {
                "execution_paths": _sorted_values(execution_paths),
                "providers": _sorted_values(providers),
                "operations": _sorted_values(operations),
            }
        )
    return available


def _counter_chart(title: str, counter: Counter) -> dict[str, Any]:
    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return {
        "title": title,
        "chart_type": "bar",
        "labels": [name for name, _ in ordered],
        "values": [int(value) for _, value in ordered],
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(ceil(percentile * len(ordered)) - 1, 0)
    return round(float(ordered[min(rank, len(ordered) - 1)]), 1)


def _bucket_spec(
    dated_records: list[tuple[datetime, dict[str, Any]]],
    *,
    created_from: datetime | None,
    created_to: datetime | None,
) -> str:
    if created_from is not None and created_to is not None:
        span = created_to - created_from
    elif len(dated_records) >= 2:
        span = max(item[0] for item in dated_records) - min(
            item[0] for item in dated_records
        )
    else:
        span = timedelta(0)
    if span <= timedelta(days=2):
        return "hour"
    if span <= timedelta(days=120):
        return "day"
    return "week"


def _bucket_label(value: datetime, granularity: str) -> str:
    if granularity == "hour":
        return value.replace(minute=0, second=0, microsecond=0).isoformat()
    if granularity == "day":
        return value.date().isoformat()
    week_start = value.date() - timedelta(days=value.weekday())
    return week_start.isoformat()


def _activity_graph(
    dated_records: list[tuple[datetime, dict[str, Any]]],
    *,
    internal: bool,
    timezone: ZoneInfo,
    created_from: datetime | None,
    created_to: datetime | None,
) -> dict[str, Any]:
    granularity = _bucket_spec(
        dated_records,
        created_from=created_from,
        created_to=created_to,
    )
    buckets: dict[str, Counter] = defaultdict(Counter)
    for created_at, record in dated_records:
        label = _bucket_label(created_at.astimezone(timezone), granularity)
        buckets[label]["total"] += 1
        if internal:
            status = str(dict(record.get("api") or {}).get("status") or "")
            key = "success" if status.casefold() in _SUCCESS_STATUSES else "failure"
        else:
            outcome = str(record.get("outcome") or "unknown").casefold()
            key = (
                outcome
                if outcome in {"fulfilled", "zero_result", "failure"}
                else "other"
            )
        buckets[label][key] += 1
    labels = sorted(buckets)
    series_names = (
        (
            ("Requests", "total"),
            ("Successful", "success"),
            ("Failed", "failure"),
        )
        if internal
        else (
            ("Searches", "total"),
            ("Fulfilled", "fulfilled"),
            ("Zero result", "zero_result"),
            ("Failed", "failure"),
        )
    )
    return {
        "title": "API Activity Over Time" if internal else "Search Activity Over Time",
        "chart_type": "line",
        "granularity": granularity,
        "timezone": str(timezone),
        "labels": labels,
        "values": [int(buckets[label]["total"]) for label in labels],
        "series": [
            {
                "name": display,
                "values": [int(buckets[label][key]) for label in labels],
            }
            for display, key in series_names
        ],
    }


def _company_overview(records: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = Counter(str(record.get("outcome") or "unknown") for record in records)
    categories = Counter(
        category for record in records for category in _record_categories(record)
    )
    languages = Counter(str(record.get("language") or "Unknown") for record in records)
    total = len(records)
    fulfilled = outcomes.get("fulfilled", 0)
    result_counts = [
        int(dict(record.get("search") or {}).get("result_count") or 0)
        for record in records
    ]
    total_results = [
        int(dict(record.get("search") or {}).get("total_results") or 0)
        for record in records
    ]
    return {
        "summary": {
            "searches": total,
            "fulfilled": int(fulfilled),
            "zero_results": int(outcomes.get("zero_result", 0)),
            "failures": int(outcomes.get("failure", 0)),
            "fulfillment_rate": round(fulfilled / total * 100, 1) if total else 0,
            "average_returned_results": round(mean(result_counts), 1)
            if result_counts
            else 0,
            "average_total_results": round(mean(total_results), 1)
            if total_results
            else 0,
        },
        "breakdowns": {
            "outcomes": _counter_chart("Search Outcomes", outcomes),
            "categories": _counter_chart("Searches by Category", categories),
            "languages": _counter_chart("Search Language", languages),
        },
    }


def _operation_group(operation: str) -> str:
    normalized = operation.strip().casefold()
    if normalized in {"query_planning", "llm", "chat", "generation"}:
        return "llm"
    if normalized in {"reranking", "reranker", "rerank"}:
        return "reranker"
    if normalized == "embedding":
        return "embedding"
    return normalized or "other"


def _internal_overview(
    records: list[dict[str, Any]], filters: DashboardFilters
) -> dict[str, Any]:
    paths: Counter = Counter()
    providers: Counter = Counter()
    operations: Counter = Counter()
    statuses: Counter = Counter()
    durations: list[float] = []
    stage_values: dict[str, list[float]] = defaultdict(list)
    token_groups: dict[str, Counter] = defaultdict(Counter)
    plan_cache: Counter = Counter()
    result_cache: Counter = Counter()
    downstream_calls = 0

    provider_filter = _normalized_choice(filters.provider)
    operation_filter = _normalized_choice(filters.operation)
    for record in records:
        performance = dict(record.get("performance") or {})
        api = dict(record.get("api") or {})
        path = str(performance.get("execution_path") or "unknown")
        paths[path] += 1
        status = str(api.get("status") or "unknown")
        statuses[status] += 1
        duration = performance.get("total_server_duration_ms")
        if isinstance(duration, (int, float)):
            durations.append(float(duration))
        downstream_calls += int(performance.get("downstream_api_calls") or 0)
        cache = dict(performance.get("cache") or {})
        if cache.get("plan_hit") is not None:
            plan_cache[bool(cache["plan_hit"])] += 1
        if cache.get("result_hit") is not None:
            result_cache[bool(cache["result_hit"])] += 1
        for name, value in dict(performance.get("stages_ms") or {}).items():
            if isinstance(value, (int, float)):
                stage_values[str(name)].append(float(value))

        for attempt in _record_attempts(record):
            provider = str(attempt.get("provider") or "unknown")
            operation = str(attempt.get("operation") or "unknown")
            if provider_filter and provider.casefold() != provider_filter.casefold():
                continue
            if operation_filter and operation.casefold() != operation_filter.casefold():
                continue
            providers[provider] += 1
            operations[operation] += 1
            group = token_groups[_operation_group(operation)]
            group["attempts"] += 1
            group["api_calls"] += max(int(attempt.get("api_calls") or 0), 0)
            nonzero_tokens = False
            for name in (
                "input_tokens",
                "output_tokens",
                "thought_tokens",
                "total_tokens",
            ):
                value = max(int(attempt.get(name) or 0), 0)
                group[name] += value
                nonzero_tokens = nonzero_tokens or value > 0
            if nonzero_tokens:
                group["attempts_with_reported_tokens"] += 1

    total = len(records)
    successful = sum(
        count
        for status, count in statuses.items()
        if status.casefold() in _SUCCESS_STATUSES
    )
    token_payload = {
        group_name: {
            key: int(values.get(key, 0))
            for key in (
                "attempts",
                "api_calls",
                "attempts_with_reported_tokens",
                "input_tokens",
                "output_tokens",
                "thought_tokens",
                "total_tokens",
            )
        }
        for group_name, values in sorted(token_groups.items())
    }
    for required in ("llm", "reranker"):
        token_payload.setdefault(
            required,
            {
                "attempts": 0,
                "api_calls": 0,
                "attempts_with_reported_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "thought_tokens": 0,
                "total_tokens": 0,
            },
        )
    stage_latency = {
        name: {
            "count": len(values),
            "avg_ms": round(mean(values), 1),
            "p95_ms": _percentile(values, 0.95),
        }
        for name, values in sorted(stage_values.items())
        if values
    }
    return {
        "summary": {
            "requests": total,
            "successful": int(successful),
            "failed": int(total - successful),
            "success_rate": round(successful / total * 100, 1) if total else 0,
            "average_latency_ms": round(mean(durations), 1) if durations else 0,
            "p50_latency_ms": _percentile(durations, 0.50),
            "p95_latency_ms": _percentile(durations, 0.95),
            "p99_latency_ms": _percentile(durations, 0.99),
            "downstream_api_calls": downstream_calls,
            "plan_cache_hit_rate": (
                round(plan_cache[True] / sum(plan_cache.values()) * 100, 1)
                if plan_cache
                else None
            ),
            "result_cache_hit_rate": (
                round(result_cache[True] / sum(result_cache.values()) * 100, 1)
                if result_cache
                else None
            ),
        },
        "token_usage_by_operation": {
            "title": "Reported Token Usage: LLM vs Reranker",
            "chart_type": "comparison_table",
            "note": (
                "Token totals are provider-reported; attempts with zero tokens "
                "may represent zero usage or unavailable provider telemetry."
            ),
            "data": token_payload,
        },
        "breakdowns": {
            "execution_paths": _counter_chart("Execution Paths", paths),
            "providers": _counter_chart("Provider Attempts", providers),
            "operations": _counter_chart("Operations", operations),
            "statuses": _counter_chart("Request Statuses", statuses),
        },
        "stage_latency": {
            "title": "Measured Stage Latency",
            "chart_type": "comparison_table",
            "timing_semantics": "stages_may_overlap_do_not_sum",
            "data": stage_latency,
        },
    }


def build_dashboard_overview(
    records: list[dict[str, Any]],
    *,
    internal: bool,
    filters: DashboardFilters,
    timezone_name: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    timezone = ZoneInfo(validate_timezone(timezone_name))
    period, created_from, created_to = _resolve_window(
        filters,
        timezone=timezone,
        now=now,
    )
    filtered = [
        record
        for record in records
        if _matches(
            record,
            filters=filters,
            created_from=created_from,
            created_to=created_to,
            internal=internal,
        )
    ]
    dated_records = [
        (created_at, record)
        for record in filtered
        if (created_at := _record_datetime(record)) is not None
    ]
    overview = (
        _internal_overview(filtered, filters)
        if internal
        else _company_overview(filtered)
    )
    overview["main_graph"] = _activity_graph(
        dated_records,
        internal=internal,
        timezone=timezone,
        created_from=created_from,
        created_to=created_to,
    )
    return {
        "filtering": {
            "applied": {
                "period": period,
                "from": created_from.isoformat() if created_from else None,
                "to": created_to.isoformat() if created_to else None,
                "timezone": timezone_name,
                "outcome": _normalized_choice(filters.outcome),
                "category": _normalized_choice(filters.category),
                "language": _normalized_choice(filters.language),
                "city": _normalized_choice(filters.city),
                "city_id": filters.city_id,
                "ad_type": _normalized_choice(filters.ad_type),
                **(
                    {
                        "execution_path": _normalized_choice(filters.execution_path),
                        "provider": _normalized_choice(filters.provider),
                        "operation": _normalized_choice(filters.operation),
                    }
                    if internal
                    else {}
                ),
            },
            "available": _available_filters(records, internal=internal),
            "scope": {
                "filtered_overview": "search_and_api_activity_records",
                "snapshot_modules": (
                    "Current catalogue, user, supply, and market questions are "
                    "daily snapshot metrics and are not rewritten by activity filters."
                ),
                "city_semantics": (
                    "City is the resolved request filter captured by the search API; "
                    "older records without filter context are excluded when selected."
                ),
            },
            "matched_records": len(filtered),
            "total_records": len(records),
        },
        "filtered_overview": overview,
    }
