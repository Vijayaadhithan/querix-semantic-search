"""Build identical Explorer facets from durable or temporary snapshot records."""

from __future__ import annotations

import sqlite3
from typing import Any


def build_query_facets(
    connection: sqlite3.Connection,
    company_id: str,
    version: str,
    *,
    internal: bool,
) -> dict[str, Any]:
    field = "internal_json" if internal else "company_json"

    def choices(path: str) -> list[str]:
        rows = connection.execute(
            f"""
            SELECT DISTINCT json_extract(records.{field}, ?) AS value
            FROM analytics_query_records AS records
            WHERE records.company_id = ?
              AND records.snapshot_version = ?
              AND json_extract(records.{field}, ?) IS NOT NULL
            ORDER BY value COLLATE NOCASE
            """,
            (path, company_id, version, path),
        ).fetchall()
        return [str(row["value"]) for row in rows if str(row["value"] or "")]

    def id_labels(id_path: str, label_path: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            f"""
            SELECT
                json_extract(records.{field}, ?) AS id,
                MAX(json_extract(records.{field}, ?)) AS label
            FROM analytics_query_records AS records
            WHERE records.company_id = ?
              AND records.snapshot_version = ?
              AND json_extract(records.{field}, ?) IS NOT NULL
            GROUP BY id
            ORDER BY label COLLATE NOCASE, id
            """,
            (id_path, label_path, company_id, version, id_path),
        ).fetchall()
        return [
            {"id": int(row["id"]), "label": str(row["label"] or f"ID {row['id']}")}
            for row in rows
        ]

    return {
        "request_kinds": choices("$.request_kind"),
        "cities": id_labels("$.filters.city_id", "$.filters.city"),
        "subcategories": id_labels("$.filters.subcategory_id", "$.filters.subcategory"),
        "ad_types": choices("$.filters.target_ad_type"),
        "diagnostic_codes": choices("$.diagnostics.code") if internal else [],
    }
