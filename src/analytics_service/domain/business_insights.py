"""Tenant-facing marketplace questions built from observed company data."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

import pandas as pd

from .scope import active_ads, active_users


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    return str(value).strip() != ""


def _demand_label(record: dict[str, Any]) -> str:
    filters = dict(record.get("filters") or {})
    for name in ("subcategory", "main_category"):
        value = str(filters.get(name) or "").strip()
        if value:
            return value
    for value in record.get("categories") or ():
        label = str(value or "").strip()
        if label and label != "Other / Uncategorized":
            return label
    return "Unclassified text"


def _demand_identity(record: dict[str, Any]) -> tuple[str, str, int | None, int | None]:
    filters = dict(record.get("filters") or {})
    subcategory_id = filters.get("subcategory_id")
    main_category_id = filters.get("main_category_id")
    label = _demand_label(record)
    try:
        if subcategory_id is not None:
            numeric_id = int(subcategory_id)
            return f"subcategory:{numeric_id}", label, None, numeric_id
    except (TypeError, ValueError):
        pass
    try:
        if main_category_id is not None:
            numeric_id = int(main_category_id)
            return f"category:{numeric_id}", label, numeric_id, None
    except (TypeError, ValueError):
        pass
    return f"label:{label.casefold()}", label, None, None


def _city_label(record: dict[str, Any]) -> str:
    filters = dict(record.get("filters") or {})
    city = str(filters.get("city") or "").strip()
    if city:
        return city
    city_id = filters.get("city_id")
    return f"City #{city_id}" if _is_present(city_id) else "No city selected"


def _demand_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # A category/location browse is an observed marketplace intent even when it
    # has no free-text query. Keep it in company demand totals.
    return list(records)


def build_company_business_insights(
    data: dict[str, pd.DataFrame],
    records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build Gainr-relevant demand and demand-versus-supply answers.

    Applied catalogue/location filters are authoritative. Text classification is
    used only when a historical request has no recorded structured category.
    """
    demand_records = _demand_records(records)
    identities = {
        key: (label, main_category_id, subcategory_id)
        for record in demand_records
        for key, label, main_category_id, subcategory_id in [_demand_identity(record)]
    }
    demand = Counter(_demand_identity(record)[0] for record in demand_records)
    ordered_demand = demand.most_common(20)

    gap_buckets: dict[str, Counter[str]] = defaultdict(Counter)
    unmet_buckets: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    ad_types: dict[str, Counter[str]] = defaultdict(Counter)
    for record in demand_records:
        category_key, category, _, _ = _demand_identity(record)
        city = _city_label(record)
        outcome = str(record.get("outcome") or "telemetry_missing")
        gap_buckets[category_key]["searches"] += 1
        gap_buckets[category_key][outcome] += 1
        unmet_buckets[(city, category)]["searches"] += 1
        unmet_buckets[(city, category)][outcome] += 1
        ad_type = str(
            dict(record.get("filters") or {}).get("target_ad_type") or "not recorded"
        )
        ad_types[ad_type]["searches"] += 1
        ad_types[ad_type][outcome] += 1

    unmet_rows = []
    for (city, category), counts in unmet_buckets.items():
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
                "zero_result_rate": round(zero / searches * 100, 1) if searches else 0,
                "failure_rate": round(failures / searches * 100, 1) if searches else 0,
            }
        )
    unmet_rows.sort(
        key=lambda item: (item["zero_results"], item["searches"]), reverse=True
    )

    ads = active_ads(data["ads"])
    categories = data["categories"]
    subcategories = data["sub_categories"]
    category_names = categories.set_index("id")["name"].to_dict()
    subcategory_names = subcategories.set_index("id")["name"].to_dict()
    subcategory_parents = subcategories.set_index("id")["categoryId"].to_dict()
    supply = Counter()
    for subcategory_id, count in ads.groupby("category_id").size().items():
        try:
            numeric_id = int(subcategory_id)
        except (TypeError, ValueError, OverflowError):
            continue
        subcategory = str(subcategory_names.get(numeric_id) or "").strip()
        category = str(
            category_names.get(subcategory_parents.get(numeric_id)) or ""
        ).strip()
        if subcategory:
            supply[subcategory] += int(count)
            supply[f"subcategory:{numeric_id}"] += int(count)
        if category:
            supply[category] += int(count)
            parent_id = subcategory_parents.get(numeric_id)
            if parent_id is not None:
                supply[f"category:{int(parent_id)}"] += int(count)

    gap_rows = []
    for category_key, counts in gap_buckets.items():
        category, main_category_id, subcategory_id = identities[category_key]
        searches = int(counts["searches"])
        zero = int(counts["zero_result"])
        available_supply = int(supply.get(category_key, supply.get(category, 0)))
        gap_rows.append(
            {
                "category_key": category_key,
                "category": category,
                "main_category_id": main_category_id,
                "subcategory_id": subcategory_id,
                "searches": searches,
                "available_listings": available_supply,
                "zero_results": zero,
                "zero_result_rate": round(zero / searches * 100, 1) if searches else 0,
                "searches_per_100_listings": round(searches / available_supply * 100, 1)
                if available_supply
                else None,
            }
        )
    gap_rows.sort(
        key=lambda item: (item["zero_result_rate"], item["searches"]), reverse=True
    )

    ad_type_rows = []
    for ad_type, counts in sorted(ad_types.items()):
        searches = int(counts["searches"])
        zero = int(counts["zero_result"])
        ad_type_rows.append(
            {
                "ad_type": ad_type,
                "searches": searches,
                "fulfilled": int(counts["fulfilled"]),
                "zero_results": zero,
                "failures": int(counts["failure"]),
                "fulfillment_rate": round(int(counts["fulfilled"]) / searches * 100, 1)
                if searches
                else 0,
            }
        )

    unclassified_queries = Counter(
        str(record.get("normalized_query") or record.get("query") or "").strip()
        or "Catalogue browse"
        for record in demand_records
        if _demand_label(record) == "Unclassified text"
    )
    classified_count = len(demand_records) - sum(unclassified_queries.values())

    return {
        "q94_catalog_demand": {
            "labels": [identities[key][0] for key, _ in ordered_demand],
            "values": [int(count) for _, count in ordered_demand],
            "title": "What are customers looking for?",
            "note": (
                "Uses the applied Gainr catalogue category when recorded; "
                "historical text-only requests use a labelled fallback."
            ),
            "chart_type": "bar",
        },
        "q95_unmet_demand_by_city_category": {
            "data": unmet_rows[:40],
            "title": "No-result searches by city and category",
            "note": (
                "No-result searches completed normally but found no eligible listing. "
                "Technical request failures are shown separately and are not counted "
                "as unmet marketplace demand. City is the filter selected by the "
                "client."
            ),
            "chart_type": "table",
        },
        "q96_demand_supply_gap": {
            "data": gap_rows[:40],
            "title": "Where is demand stronger than available supply?",
            "note": (
                "Available listings are non-deleted ads in active statuses 1 or 8 "
                "at snapshot time. Demand uses a rolling 90-day search window; "
                "structured catalogue IDs are used when captured and older text-only "
                "records fall back to labels."
            ),
            "demand_window_days": 90,
            "supply_scope": "active_inventory_at_snapshot_time",
            "chart_type": "table",
        },
        "q97_ad_type_demand": {
            "data": ad_type_rows,
            "title": "Listing type customers searched for",
            "note": (
                "Offer listings are products or services currently available. Wanted "
                "listings are requests from people looking for something."
            ),
            "chart_type": "table",
        },
        "q98_demand_classification_coverage": {
            "title": "Searches matched to a known category",
            "chart_type": "classification_coverage",
            "total": int(len(demand_records)),
            "classified": int(classified_count),
            "unclassified": int(sum(unclassified_queries.values())),
            "coverage_rate": round(classified_count / len(demand_records) * 100, 1)
            if demand_records
            else 0,
            "top_unclassified": [
                {"query": query, "searches": int(count)}
                for query, count in unclassified_queries.most_common(20)
            ],
            "note": (
                "A search is classified when a structured catalogue filter or another "
                "reliable category was recorded. Unclassified searches are valid "
                "search texts that need taxonomy, alias, or instrumentation review."
            ),
        },
    }


def build_company_overview(
    data: dict[str, pd.DataFrame],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    ads = data["ads"]
    users = data["users"]
    current_ads = active_ads(ads)
    current_users = active_users(users)
    demand_records = _demand_records(records)
    outcomes = Counter(str(record.get("outcome") or "") for record in demand_records)
    completed = outcomes["fulfilled"] + outcomes["zero_result"]
    return {
        "scope": "Latest completed company snapshot",
        "total_users": int(len(users)),
        "active_users": int(len(current_users)),
        "total_listings": int(len(ads)),
        "active_listings": int(len(current_ads)),
        "active_sellers": int(current_ads["user_id"].nunique()),
        "cities_with_active_supply": int(current_ads["city_id"].nunique()),
        "recorded_demand": int(len(demand_records)),
        "fulfilled_demand": int(outcomes["fulfilled"]),
        "zero_result_demand": int(outcomes["zero_result"]),
        "failed_requests": int(outcomes["failure"]),
        "fulfillment_rate": round(outcomes["fulfilled"] / completed * 100, 1)
        if completed
        else 0,
    }
