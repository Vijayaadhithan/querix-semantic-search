"""Read audience-specific query records with filters and cursor pagination."""

from __future__ import annotations

import json
from typing import Any

from analytics_service.query_pagination import (
    _query_sort_expression,
    decode_query_cursor,
    decode_query_sort_cursor,
    encode_query_cursor,
    encode_query_sort_cursor,
)


def query_records(
    connection_factory,
    query_facets,
    company_id: str,
    *,
    internal: bool,
    limit: int,
    cursor: str | None = None,
    query: str | None = None,
    outcome: str | None = None,
    category: str | None = None,
    execution_path: str | None = None,
    language: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    include_filtered_results: bool = False,
    request_kind: str | None = None,
    city_id: int | None = None,
    subcategory_id: int | None = None,
    ad_type: str | None = None,
    diagnostic_code: str | None = None,
    has_filters: bool | None = None,
    sort_by: str = "created_at",
    sort_direction: str = "desc",
    include_facets: bool = True,
) -> dict[str, Any]:
    field = "internal_json" if internal else "company_json"
    normalized_sort = str(sort_by or "created_at").strip().casefold()
    normalized_direction = str(sort_direction or "desc").strip().casefold()
    if normalized_direction not in {"asc", "desc"}:
        raise ValueError("Query sort direction must be asc or desc")
    sort_expression = _query_sort_expression(normalized_sort, internal=internal)
    uses_default_cursor = (
        normalized_sort == "created_at" and normalized_direction == "desc"
    )
    offset = 0
    clauses = [
        "records.company_id = ?",
        "records.snapshot_version = snapshots.active_version",
    ]
    values: list[Any] = [company_id]
    if not include_filtered_results:
        # Keep ordinary catalogue/filter browsing out of the default Query
        # Explorer, but never hide failed requests. A failed browse is an
        # operational incident rather than demand noise and must remain
        # visible without a frontend-only opt-in parameter.
        clauses.append(
            "("
            f"COALESCE(json_extract(records.{field}, "
            "'$.request_kind'), 'text_search') = 'text_search' "
            "OR records.outcome = 'failure'"
            ")"
        )
    if cursor and uses_default_cursor:
        cursor_created, cursor_request = decode_query_cursor(cursor)
        clauses.append(
            """
            (
                records.created_at < ?
                OR (
                    records.created_at = ?
                    AND records.request_id < ?
                )
            )
            """
        )
        values.extend([cursor_created, cursor_created, cursor_request])
    elif cursor:
        cursor_sort, cursor_direction, offset = decode_query_sort_cursor(cursor)
        if (cursor_sort, cursor_direction) != (
            normalized_sort,
            normalized_direction,
        ):
            raise ValueError("Analytics query cursor does not match sorting")
    if query:
        clauses.append("LOWER(records.query_text) LIKE ? ESCAPE '\\'")
        escaped = (
            query.casefold()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        values.append(f"%{escaped}%")
    if outcome:
        clauses.append("records.outcome = ?")
        values.append(outcome)
    if category:
        clauses.append("records.categories_text LIKE ?")
        values.append(f"%|{category}|%")
    if execution_path:
        clauses.append(
            "LOWER(json_extract(records.internal_json, "
            "'$.performance.execution_path')) = ?"
        )
        values.append(execution_path.casefold())
    if language:
        clauses.append("records.language = ?")
        values.append(language)
    for json_path, expected in (
        ("$.request_kind", request_kind),
        ("$.filters.city_id", city_id),
        ("$.filters.subcategory_id", subcategory_id),
        ("$.filters.target_ad_type", ad_type),
    ):
        if expected is not None and expected != "":
            clauses.append(f"json_extract(records.{field}, '{json_path}') = ?")
            values.append(expected)
    if diagnostic_code and internal:
        clauses.append("json_extract(records.internal_json, '$.diagnostics.code') = ?")
        values.append(diagnostic_code)
    if has_filters is not None:
        operator = "!=" if has_filters else "="
        clauses.append(
            f"COALESCE(json_extract(records.{field}, '$.filters'), '{{}}') "
            f"{operator} '{{}}'"
        )
    if created_from:
        clauses.append("records.created_at >= ?")
        values.append(created_from)
    if created_to:
        clauses.append("records.created_at <= ?")
        values.append(created_to)
    order_clause = (
        "records.created_at DESC, records.request_id DESC"
        if uses_default_cursor
        else (
            f"({sort_expression} IS NULL) ASC, "
            f"{sort_expression} {normalized_direction.upper()}, "
            "records.created_at DESC, records.request_id DESC"
        )
    )
    values.append(limit + 1)
    if not uses_default_cursor:
        values.append(offset)
    with connection_factory() as connection:
        rows = connection.execute(
            f"""
            SELECT
                records.request_id,
                records.created_at,
                records.{field} AS payload_json
            FROM analytics_query_records AS records
            INNER JOIN analytics_snapshots AS snapshots
                ON snapshots.company_id = records.company_id
            WHERE {" AND ".join(clauses)}
            ORDER BY {order_clause}
            LIMIT ?
            {"OFFSET ?" if not uses_default_cursor else ""}
            """,
            tuple(values),
        ).fetchall()
    has_more = len(rows) > limit
    visible = rows[:limit]
    next_cursor = None
    if has_more and visible:
        next_cursor = (
            encode_query_cursor(
                visible[-1]["created_at"],
                visible[-1]["request_id"],
            )
            if uses_default_cursor
            else encode_query_sort_cursor(
                normalized_sort,
                normalized_direction,
                offset + len(visible),
            )
        )
    result = {
        "company_id": company_id,
        "items": [json.loads(row["payload_json"]) for row in visible],
        "returned": len(visible),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "sorting": {
            "sort_by": normalized_sort,
            "sort_direction": normalized_direction,
        },
    }
    if include_facets:
        result["facets"] = query_facets(company_id, internal=internal)
    return result
