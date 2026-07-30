"""Build the query-level projection consumed by the Query Explorer."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from .classification import (
    classify_search_query,
    detect_language,
    detect_rental_duration,
    extract_brands,
    extract_locations,
    has_typo,
    is_b2b_query,
    is_gibberish,
    is_route_query,
    normalize_query,
)
from ..utils import parse_attempts_json

LOGGER = logging.getLogger(__name__)

ATTEMPT_FIELDS = (
    "attempt_number",
    "provider",
    "model",
    "operation",
    "status",
    "api_calls",
    "input_tokens",
    "output_tokens",
    "thought_tokens",
    "total_tokens",
    "duration_ms",
    "failure_reason",
)


def _json_value(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return default
    return value


def merge_search_api(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    searches = data["search_history"].copy()
    api = data["api_usage"].copy()

    duplicate_count = int(api["request_id"].duplicated(keep=False).sum())
    if duplicate_count:
        LOGGER.warning(
            "API telemetry contains %d duplicate request-id rows; keeping the latest",
            duplicate_count,
        )
        api["_parsed_created_at"] = pd.to_datetime(api["created_at"], errors="coerce")
        api = (
            api.sort_values(["request_id", "_parsed_created_at"])
            .drop_duplicates("request_id", keep="last")
            .drop(columns="_parsed_created_at")
        )

    return searches.merge(
        api,
        on="request_id",
        how="left",
        suffixes=("_query", "_api"),
        validate="many_to_one",
    )


def _sanitize_attempts(value: Any) -> list[dict[str, Any]]:
    return [
        {
            key: _json_value(attempt.get(key))
            for key in ATTEMPT_FIELDS
            if key in attempt
        }
        for attempt in parse_attempts_json(value)
    ]


def _outcome(row: pd.Series) -> str:
    status = str(_json_value(row.get("status"), "missing")).casefold()
    if status not in {"success", "successful", "ok"}:
        return "failure" if status != "missing" else "telemetry_missing"
    return "zero_result" if _json_value(row.get("total_results"), 0) == 0 else "fulfilled"


def _build_record(
    row: pd.Series,
    enrichments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    query = str(_json_value(row.get("query_text"), ""))
    normalized = normalize_query(query)
    categories = classify_search_query(query)
    total_results = int(_json_value(row.get("total_results"), 0) or 0)
    total_tokens = int(_json_value(row.get("total_tokens"), 0) or 0)
    duration_ms = float(_json_value(row.get("duration_ms"), 0.0) or 0.0)

    record = {
        "search_id": _json_value(row.get("id_query"), _json_value(row.get("id"))),
        "request_id": str(_json_value(row.get("request_id"), "")),
        "query": query,
        "normalized_query": normalized,
        "created_at": str(
            _json_value(row.get("created_at_query"), _json_value(row.get("created_at"), ""))
        ),
        "word_count": len(normalized.split()),
        "categories": categories,
        "brands": extract_brands(query),
        "locations": extract_locations(query),
        "language": detect_language(query),
        "rental_duration": detect_rental_duration(query),
        "flags": {
            "has_typo": has_typo(query),
            "is_gibberish": is_gibberish(query),
            "is_route": is_route_query(query),
            "is_b2b": is_b2b_query(query),
            "is_uncategorized": categories == ["Other / Uncategorized"],
        },
        "outcome": _outcome(row),
        "api": {
            "status": _json_value(row.get("status"), "missing"),
            "execution_path": _json_value(row.get("execution_path"), "missing"),
            "result_count": int(_json_value(row.get("result_count"), 0) or 0),
            "total_results": total_results,
            "duration_ms": round(duration_ms, 3),
            "api_call_count": int(_json_value(row.get("api_call_count"), 0) or 0),
            "input_tokens": int(_json_value(row.get("input_tokens"), 0) or 0),
            "output_tokens": int(_json_value(row.get("output_tokens"), 0) or 0),
            "thought_tokens": int(_json_value(row.get("thought_tokens"), 0) or 0),
            "total_tokens": total_tokens,
            "tokens_per_result": (
                round(total_tokens / total_results, 2) if total_results else None
            ),
        },
        "attempts": _sanitize_attempts(row.get("attempts_json")),
    }
    enrichment = enrichments.get(normalized)
    if enrichment:
        record["ai_enrichment"] = enrichment
    return record


def build_query_records(
    data: dict[str, pd.DataFrame],
    enrichments: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    merged = merge_search_api(data)
    enrichment_map = enrichments or {}
    records = [_build_record(row, enrichment_map) for _, row in merged.iterrows()]
    return {
        "metadata": {
            "schema_version": "1.0",
            "generated_at": datetime.now(UTC).isoformat(),
            "record_count": len(records),
            "fields": {
                "outcome": [
                    "fulfilled",
                    "zero_result",
                    "failure",
                    "telemetry_missing",
                ]
            },
        },
        "queries": records,
    }
