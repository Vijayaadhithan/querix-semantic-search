from __future__ import annotations

import contextlib
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from analytics_service.filters import (
    PERIOD_OPTIONS,
    REQUEST_SCOPE_OPTIONS,
    DashboardFilters,
)
from verticals.marketplace.analytics.dashboard_charts import (
    _activity_graph as _activity_graph,
)
from verticals.marketplace.analytics.dashboard_charts import (
    _bucket_label as _bucket_label,
)
from verticals.marketplace.analytics.dashboard_charts import (
    _bucket_spec as _bucket_spec,
)
from verticals.marketplace.analytics.dashboard_records import (
    _PERIOD_DELTAS as _PERIOD_DELTAS,
)
from verticals.marketplace.analytics.dashboard_records import (
    _SUCCESS_STATUSES as _SUCCESS_STATUSES,
)
from verticals.marketplace.analytics.dashboard_records import (
    _aware_boundary as _aware_boundary,
)
from verticals.marketplace.analytics.dashboard_records import (
    _FilteredRecords as _FilteredRecords,
)
from verticals.marketplace.analytics.dashboard_records import (
    _matches as _matches,
)
from verticals.marketplace.analytics.dashboard_records import (
    _normalized_choice as _normalized_choice,
)
from verticals.marketplace.analytics.dashboard_records import (
    _record_attempts as _record_attempts,
)
from verticals.marketplace.analytics.dashboard_records import (
    _record_categories as _record_categories,
)
from verticals.marketplace.analytics.dashboard_records import (
    _record_datetime as _record_datetime,
)
from verticals.marketplace.analytics.dashboard_records import (
    _record_request_kind as _record_request_kind,
)
from verticals.marketplace.analytics.dashboard_records import (
    _resolve_window as _resolve_window,
)
from verticals.marketplace.analytics.dashboard_records import (
    _sorted_values as _sorted_values,
)
from verticals.marketplace.analytics.dashboard_records import (
    validate_timezone as validate_timezone,
)
from verticals.marketplace.analytics.dashboard_summaries import (
    _company_overview as _company_overview,
)
from verticals.marketplace.analytics.dashboard_summaries import (
    _counter_chart as _counter_chart,
)
from verticals.marketplace.analytics.dashboard_summaries import (
    _internal_overview as _internal_overview,
)
from verticals.marketplace.analytics.dashboard_summaries import (
    _operation_group as _operation_group,
)
from verticals.marketplace.analytics.dashboard_summaries import (
    _percentile as _percentile,
)


def _filtered_company_insights(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    phrases = Counter()
    demand = Counter()
    unmet = defaultdict(Counter)
    ad_types = defaultdict(Counter)
    for record in records:
        applied = dict(record.get("filters") or {})
        category = str(
            applied.get("subcategory")
            or applied.get("main_category")
            or next(
                (
                    value
                    for value in record.get("categories") or ()
                    if value != "Other / Uncategorized"
                ),
                "Unclassified text",
            )
        )
        city = str(
            applied.get("city")
            or (
                f"City #{applied['city_id']}"
                if applied.get("city_id") is not None
                else "No city selected"
            )
        )
        outcome = str(record.get("outcome") or "telemetry_missing")
        ad_type = str(applied.get("target_ad_type") or "not recorded")
        if outcome == "zero_result" and _record_request_kind(record) == "text_search":
            phrase = str(record.get("normalized_query") or "").strip()
            if phrase:
                phrases[phrase] += 1
        demand[category] += 1
        unmet[(city, category)][outcome] += 1
        unmet[(city, category)]["searches"] += 1
        ad_types[ad_type][outcome] += 1
        ad_types[ad_type]["searches"] += 1
    unmet_rows = []
    for (city, category), counts in unmet.items():
        searches = int(counts["searches"])
        zero = int(counts["zero_result"])
        failures = int(counts["failure"])
        if not zero and not failures:
            continue
        unmet_rows.append(
            {
                "city": city,
                "category": category,
                "searches": searches,
                "zero_results": zero,
                "failures": failures,
                "zero_result_rate": round(zero / searches * 100, 1),
                "failure_rate": round(failures / searches * 100, 1),
            }
        )
    unmet_rows.sort(
        key=lambda row: (
            -row["zero_results"],
            -row["searches"],
            row["city"].casefold(),
            row["category"].casefold(),
        )
    )
    ordered_demand = sorted(
        demand.items(), key=lambda item: (-item[1], item[0].casefold())
    )[:20]
    return {
        "filtered_no_result_phrases": {
            "title": "Top searches with no results",
            "chart_type": "table",
            "data": [
                {"query": phrase, "count": count}
                for phrase, count in sorted(
                    phrases.items(), key=lambda item: (-item[1], item[0])
                )[:10]
            ],
        },
        "filtered_catalog_demand": {
            "title": "What did customers look for in this filtered scope?",
            "chart_type": "bar",
            "labels": [label for label, _ in ordered_demand],
            "values": [int(value) for _, value in ordered_demand],
        },
        "filtered_unmet_demand": {
            "title": "No-result searches in this selection",
            "chart_type": "table",
            "data": unmet_rows[:40],
            "note": (
                "No-result searches completed normally but found no eligible listing. "
                "Technical request failures are reported separately."
            ),
        },
        "filtered_ad_type_demand": {
            "title": "Listing type customers searched for",
            "chart_type": "table",
            "data": [
                {
                    "ad_type": ad_type,
                    "searches": int(counts["searches"]),
                    "fulfilled": int(counts["fulfilled"]),
                    "zero_results": int(counts["zero_result"]),
                    "failures": int(counts["failure"]),
                }
                for ad_type, counts in sorted(ad_types.items())
            ],
            "note": (
                "Offer listings are available products or services. Wanted listings "
                "are requests from people looking for something."
            ),
        },
    }


def available_dashboard_filters(
    records: Iterable[dict[str, Any]], *, internal: bool
) -> dict[str, Any]:
    categories: set[str] = set()
    cities: set[str] = set()
    city_options = {}
    ad_types: set[str] = set()
    providers: set[str] = set()
    operations: set[str] = set()
    execution_paths: set[str] = set()
    outcomes: set[str] = set()
    languages: set[str] = set()
    for record in records:
        categories.update(_record_categories(record))
        outcomes.add(str(record.get("outcome") or ""))
        languages.add(str(record.get("language") or ""))
        applied = dict(record.get("filters") or {})
        cities.add(str(applied.get("city") or ""))
        city_id = applied.get("city_id")
        city_label = str(applied.get("city") or "").strip()
        if city_id is not None:
            with contextlib.suppress(TypeError, ValueError):
                city_options[int(city_id)] = city_label or str(city_id)
        ad_types.add(str(applied.get("target_ad_type") or ""))
        if internal:
            performance = dict(record.get("performance") or {})
            execution_paths.add(str(performance.get("execution_path") or ""))
            for attempt in _record_attempts(record):
                providers.add(str(attempt.get("provider") or ""))
                operations.add(str(attempt.get("operation") or ""))
    available = {
        "periods": list(PERIOD_OPTIONS),
        "request_scopes": list(REQUEST_SCOPE_OPTIONS),
        "outcomes": _sorted_values(outcomes),
        "categories": _sorted_values(categories),
        "languages": _sorted_values(languages),
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


def build_dashboard_overview(
    records: Iterable[dict[str, Any]],
    *,
    internal: bool,
    filters: DashboardFilters,
    timezone_name: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    timezone = ZoneInfo(validate_timezone(timezone_name))
    request_scope = str(filters.request_scope or "all").strip().casefold()
    if request_scope not in REQUEST_SCOPE_OPTIONS:
        raise ValueError(
            "Dashboard request scope must be one of: "
            + ", ".join(REQUEST_SCOPE_OPTIONS)
        )
    period, created_from, created_to = _resolve_window(
        filters,
        timezone=timezone,
        now=now,
    )
    filtered = _FilteredRecords(
        records,
        filters=filters,
        created_from=created_from,
        created_to=created_to,
        internal=internal,
        request_scope=request_scope,
    )
    overview = (
        _internal_overview(filtered, filters)
        if internal
        else _company_overview(filtered)
    )
    overview["main_graph"] = _activity_graph(
        filtered,
        internal=internal,
        timezone=timezone,
        created_from=created_from,
        created_to=created_to,
    )
    matched_records = overview["summary"]["requests" if internal else "all_requests"]
    available = getattr(records, "available_filters", None)
    if available is None:
        available = available_dashboard_filters(records, internal=internal)
    return {
        "filtering": {
            "applied": {
                "period": period,
                "request_scope": request_scope,
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
            "available": available,
            "scope": {
                "filtered_overview": (
                    "All search activity includes text searches and structured "
                    "catalogue/filter browsing. Select a narrower search scope to "
                    "compare them consistently."
                ),
                "snapshot_modules": (
                    "Current catalogue, user, supply, and market questions are "
                    "snapshot metrics and are not rewritten by activity filters."
                ),
                "city_semantics": (
                    "City is the resolved request filter captured by the search API; "
                    "older records without filter context are excluded when selected."
                ),
            },
            "matched_records": matched_records,
            "total_records": len(records),
        },
        "filtered_overview": overview,
        **(
            {"filtered_insights": _filtered_company_insights(filtered)}
            if not internal
            else {}
        ),
    }
