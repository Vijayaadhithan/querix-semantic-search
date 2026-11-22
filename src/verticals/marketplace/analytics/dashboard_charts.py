"""Build timezone-aware activity charts from filtered analytics records."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from verticals.marketplace.analytics.dashboard_records import (
    _SUCCESS_STATUSES,
    _record_datetime,
    _record_request_kind,
)


def _bucket_spec(
    records: Iterable[dict[str, Any]],
    *,
    created_from: datetime | None,
    created_to: datetime | None,
) -> str:
    if created_from is not None and created_to is not None:
        span = created_to - created_from
    else:
        earliest = None
        latest = None
        for record in records:
            created_at = _record_datetime(record)
            if created_at is None:
                continue
            earliest = created_at if earliest is None else min(earliest, created_at)
            latest = created_at if latest is None else max(latest, created_at)
        span = latest - earliest if earliest is not None else timedelta(0)
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
    records: Iterable[dict[str, Any]],
    *,
    internal: bool,
    timezone: ZoneInfo,
    created_from: datetime | None,
    created_to: datetime | None,
) -> dict[str, Any]:
    granularity = _bucket_spec(
        records,
        created_from=created_from,
        created_to=created_to,
    )
    buckets: dict[str, Counter] = defaultdict(Counter)
    for record in records:
        created_at = _record_datetime(record)
        if created_at is None:
            continue
        label = _bucket_label(created_at.astimezone(timezone), granularity)
        buckets[label]["total"] += 1
        if internal:
            status = str(dict(record.get("api") or {}).get("status") or "")
            key = "success" if status.casefold() in _SUCCESS_STATUSES else "failure"
            buckets[label][key] += 1
            if record.get("outcome") == "failure":
                buckets[label]["failed_outcome"] += 1
        else:
            outcome = str(record.get("outcome") or "unknown").casefold()
            if _record_request_kind(record) == "text_search":
                buckets[label]["text_search"] += 1
                key = (
                    outcome
                    if outcome in {"fulfilled", "zero_result", "failure"}
                    else "other"
                )
                buckets[label][key] += 1
            else:
                buckets[label]["browse"] += 1
                if outcome == "failure":
                    buckets[label]["failure"] += 1
    labels = sorted(buckets, key=datetime.fromisoformat)
    intervals = []
    for label in labels:
        start = datetime.fromisoformat(label)
        if granularity == "hour":
            start = start.astimezone(UTC)
            stop = start + timedelta(hours=1)
        else:
            start = start.replace(tzinfo=timezone)
            stop = start + timedelta(days=7 if granularity == "week" else 1)
        start = start.astimezone(UTC)
        end = stop.astimezone(UTC) - timedelta(microseconds=1)
        intervals.append(
            {
                "from": max(start, created_from or start).isoformat(),
                "to": min(end, created_to or end).isoformat(),
            }
        )
    series_names = (
        (
            ("Requests", "total"),
            ("Successful", "success"),
            ("Failed", "failure"),
            ("Failed outcomes", "failed_outcome"),
        )
        if internal
        else (
            ("All search activity", "total"),
            ("Text searches", "text_search"),
            ("Fulfilled", "fulfilled"),
            ("Zero result", "zero_result"),
            ("Catalogue/filter browse", "browse"),
            ("Failed", "failure"),
        )
    )
    return {
        "title": "API Activity Over Time" if internal else "Search Activity Over Time",
        "chart_type": "line",
        "granularity": granularity,
        "timezone": str(timezone),
        "labels": labels,
        "intervals": intervals,
        "values": [int(buckets[label]["total"]) for label in labels],
        "series": [
            {
                "name": display,
                "values": [int(buckets[label][key]) for label in labels],
            }
            for display, key in series_names
        ],
    }
