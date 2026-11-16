"""Build parameterized Gainr predicates shared by count and card queries."""

import json
from typing import Any

from storage.mysql import (
    quote_mysql_identifier,
)
from tenants.gainr.models import (
    GainrSearchFilter,
)

_BOOST_ADS_TABLE = quote_mysql_identifier("boost_ads")


def append_condition(
    conditions: list[str],
    params: list[Any],
    expression: str,
    value,
) -> None:
    if isinstance(value, (list, tuple, set)):
        values = list(dict.fromkeys(value))
        if not values:
            return
        placeholders = ", ".join("%s" for _ in values)
        conditions.append(f"{expression} IN ({placeholders})")
        params.extend(values)
        return
    conditions.append(f"{expression} = %s")
    params.append(value)


def build_where_clause(
    resolved_filters: dict,
    request_filter: GainrSearchFilter,
    *,
    serves_cards_from_search_ready: bool,
    result_table: str,
    search_active_qualified_condition: str,
    compatibility,
    product_ids: list[Any] | None = None,
    fallback_term: str = "",
    allowed_ad_types: set[str] | None = None,
) -> tuple[str, list[Any]]:
    conditions = []
    card_alias = "sr" if serves_cards_from_search_ready else "a"
    if serves_cards_from_search_ready:
        # Search-ready is an ETL projection and can briefly remain active
        # after the authoritative ad has been disabled or deleted. Every
        # client-facing card query must recheck the live ad row.
        conditions.append(
            "EXISTS ("
            f"SELECT 1 FROM {result_table} AS live_ad "
            "WHERE live_ad.id = sr.id "
            "AND live_ad.status = 1 "
            "AND (live_ad.deleted_at IS NULL "
            "OR TRIM(live_ad.deleted_at) = '')"
            ")"
        )
    else:
        conditions.extend(
            (
                "a.status = 1",
                "(a.deleted_at IS NULL OR TRIM(a.deleted_at) = '')",
            )
        )
    if search_active_qualified_condition:
        conditions.insert(0, search_active_qualified_condition)
    params: list[Any] = []
    column_map = {
        "main_category_name": "sr.main_category_name",
        "subcategory_name": "sr.subcategory_name",
        "state_name": "sr.state_name",
        "city_name": "sr.city_name",
        "locality_name": "sr.locality_name",
        "rental_duration": "sr.rental_duration",
        "main_category_id": "sr.main_category_id",
        "subcategory_id": "sr.subcategory_id",
        "state_id": "sr.state_id",
        "city_id": "sr.city_id",
        "locality_id": "sr.locality_id",
        "user_gender": "sr.user_gender",
    }
    for key, value in resolved_filters.get("categorical", {}).items():
        expression = column_map.get(key)
        if expression is not None:
            append_condition(
                conditions,
                params,
                expression,
                value,
            )
    category_type = request_filter.category_type
    if category_type not in ("", None):
        if category_type in (1, 2):
            append_condition(
                conditions,
                params,
                "sr.category_type",
                category_type,
            )
        elif category_type == 3:
            # The search-ready projection has no boost count. Match the
            # authoritative, date-bounded boost rows used for promotion.
            conditions.append(
                "sr.id IN ("
                f"SELECT boost.ads_id FROM {_BOOST_ADS_TABLE} AS boost "
                "WHERE boost.deleted_at IS NULL "
                "AND boost.start_date <= CURDATE() "
                "AND boost.end_date >= CURDATE()"
                ")"
            )
    if request_filter.attribute_value:
        attribute_conditions = []
        for value in request_filter.attribute_value:
            attribute_conditions.append("JSON_CONTAINS(sr.ads_attributes_json, %s)")
            params.append(json.dumps({"value": value}, ensure_ascii=False))
        conditions.append(f"({' OR '.join(attribute_conditions)})")
    minimum = resolved_filters.get("min_rental_fee")
    maximum = resolved_filters.get("max_rental_fee")
    if minimum is not None or maximum is not None:
        priced_conditions = ["sr.rental_fee > 1"]
        priced_params = []
        if minimum is not None:
            priced_conditions.append("sr.rental_fee >= %s")
            priced_params.append(minimum)
        if maximum is not None:
            priced_conditions.append("sr.rental_fee <= %s")
            priced_params.append(maximum)
        priced_clause = " AND ".join(priced_conditions)
        if allowed_ad_types is not None and "2" in allowed_ad_types:
            conditions.append(
                f"(({card_alias}.type = %s AND "
                "(sr.rental_fee IS NULL OR sr.rental_fee <= 1)) "
                f"OR ({priced_clause}))"
            )
            params.append("2")
        else:
            conditions.append(f"({priced_clause})")
        params.extend(priced_params)
    if allowed_ad_types is not None:
        append_condition(
            conditions,
            params,
            f"{card_alias}.type",
            sorted(allowed_ad_types),
        )
    if request_filter.fee:
        negotiable_values = []
        for value in request_filter.fee:
            if value == compatibility.fixed_fee_id:
                negotiable_values.append(0)
            elif value == compatibility.negotiable_fee_id:
                negotiable_values.append(1)
        if negotiable_values:
            append_condition(
                conditions,
                params,
                f"{card_alias}.is_rent_negotiable",
                negotiable_values,
            )
    if product_ids is not None:
        if not product_ids:
            conditions.append("1 = 0")
        else:
            append_condition(
                conditions,
                params,
                "sr.id",
                product_ids,
            )
    elif fallback_term and not resolved_filters.get("categorical"):
        contains = f"%{fallback_term}%"
        conditions.append("(sr.title LIKE %s OR sr.bm25_content LIKE %s)")
        params.extend((contains, contains))
    return " AND ".join(conditions), params
