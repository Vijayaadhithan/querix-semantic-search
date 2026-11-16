"""Parameterized vector metadata filters and shadow-result comparisons."""

from __future__ import annotations

from typing import Any

FILTER_INDEX_KEYS = (
    "main_category_name",
    "subcategory_name",
    "state_name",
    "city_name",
    "locality_name",
    "rental_duration",
    "city_id",
    "subcategory_id",
    "locality_id",
    "user_gender",
)


def _metadata_key_literal(key: str) -> str:
    if "\x00" in key:
        raise ValueError("PostgreSQL metadata keys cannot contain null bytes")
    return "'" + key.replace("'", "''") + "'"


def _metadata_filter_sql(where: dict[str, Any] | None) -> tuple[str, list[Any]]:
    if not where:
        return "", []
    if "$and" in where:
        clauses = []
        params: list[Any] = []
        for child in where["$and"]:
            clause, child_params = _metadata_filter_sql(child)
            if clause:
                clauses.append(f"({clause})")
                params.extend(child_params)
        return " AND ".join(clauses), params
    clauses = []
    params: list[Any] = []
    for key, expected in where.items():
        key_literal = _metadata_key_literal(str(key))
        if not isinstance(expected, dict):
            clauses.append(f"metadata ->> {key_literal} = %s")
            params.append(str(expected))
            continue
        for operator, value in expected.items():
            if operator == "$in":
                values = [str(item) for item in value]
                if not values:
                    clauses.append("FALSE")
                else:
                    placeholders = ", ".join(["%s"] * len(values))
                    clauses.append(f"metadata ->> {key_literal} IN ({placeholders})")
                    params.extend(values)
            elif operator in {"$gte", "$lte"}:
                comparator = ">=" if operator == "$gte" else "<="
                clauses.append(
                    f"(metadata ->> {key_literal}) ~ %s "
                    f"AND (metadata ->> {key_literal})::double precision "
                    f"{comparator} %s"
                )
                params.extend(
                    (
                        r"^-?[0-9]+(\.[0-9]+)?$",
                        float(value),
                    )
                )
            else:
                raise ValueError(f"Unsupported pgvector metadata operator {operator!r}")
    return " AND ".join(clauses), params


def _filter_uses_index(where: dict[str, Any] | None) -> bool:
    if not where:
        return False
    if "$and" in where:
        return any(_filter_uses_index(child) for child in where["$and"])
    return any(str(key) in FILTER_INDEX_KEYS for key in where)


def _metadata_matches_where(
    metadata: dict[str, Any],
    where: dict[str, Any] | None,
) -> bool:
    if not where:
        return True
    if "$and" in where:
        return all(_metadata_matches_where(metadata, child) for child in where["$and"])
    for key, expected in where.items():
        actual = metadata.get(key)
        if not isinstance(expected, dict):
            if str(actual) != str(expected):
                return False
            continue
        if "$in" in expected:
            if str(actual) not in {str(value) for value in expected["$in"]}:
                return False
            continue
        try:
            numeric_actual = float(actual)
        except (TypeError, ValueError):
            return False
        if "$gte" in expected and numeric_actual < float(expected["$gte"]):
            return False
        if "$lte" in expected and numeric_actual > float(expected["$lte"]):
            return False
    return True


def _shadow_rows(
    rows: list[dict[str, Any]],
    where: dict[str, Any] | None,
    limit: int,
) -> list[dict[str, Any]]:
    return [
        row for row in rows if _metadata_matches_where(row.get("metadata") or {}, where)
    ][:limit]


def _rows_equivalent(
    legacy_rows: list[dict[str, Any]],
    optimized_rows: list[dict[str, Any]],
) -> bool:
    if len(legacy_rows) != len(optimized_rows):
        return False
    for legacy, optimized in zip(legacy_rows, optimized_rows):
        if str(legacy.get("id")) != str(optimized.get("id")):
            return False
        if legacy.get("document") != optimized.get("document"):
            return False
        if legacy.get("metadata") != optimized.get("metadata"):
            return False
        if (
            abs(
                float(legacy.get("distance", 0.0))
                - float(optimized.get("distance", 0.0))
            )
            > 1e-9
        ):
            return False
    return True
