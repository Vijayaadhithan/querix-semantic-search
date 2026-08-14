"""Build the query-level projection consumed by the Query Explorer."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from ..utils import parse_attempts_json
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

TIMING_FIELDS = (
    "total_server_ms",
    "planning_ms",
    "query_model_ms",
    "query_model_load_ms",
    "engine_total_ms",
    "result_cache_ms",
    "embedding_ms",
    "embedding_load_ms",
    "vector_search_ms",
    "bm25_search_ms",
    "retrieval_ms",
    "parallel_retrieval_ms",
    "fusion_ms",
    "type_lookup_ms",
    "reranker_load_ms",
    "reranking_ms",
    "related_tail_ms",
    "database_filter_ms",
    "eligibility_ms",
    "hydration_ms",
    "response_mapping_ms",
    "session_storage_ms",
    "usage_recording_ms",
    "recent_search_ms",
)

_BROWSE_FILTER_FIELDS = (
    "main_category",
    "subcategory_id",
    "subcategory",
    "state",
    "city_id",
    "city",
    "locality_id",
    "locality",
    "rental_duration",
    "min_rental_fee",
    "max_rental_fee",
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
        {key: _json_value(attempt.get(key)) for key in ATTEMPT_FIELDS if key in attempt}
        for attempt in parse_attempts_json(value)
    ]


def _sanitize_filter_context(value: Any) -> dict[str, Any]:
    normalized = _json_value(value, {})
    if isinstance(normalized, str) and normalized.strip():
        try:
            normalized = json.loads(normalized)
        except (TypeError, ValueError):
            normalized = {}
    if not isinstance(normalized, dict):
        return {}
    allowed = (
        "main_category",
        "subcategory_id",
        "subcategory",
        "state",
        "city_id",
        "city",
        "locality_id",
        "locality",
        "rental_duration",
        "min_rental_fee",
        "max_rental_fee",
        "target_ad_type",
    )
    sanitized = {}
    for name in allowed:
        item = _json_value(normalized.get(name))
        if (
            item is not None
            and item != ""
            and isinstance(item, (str, int, float, bool))
        ):
            sanitized[name] = item
    return sanitized


def _cache_value(value: Any) -> bool | None:
    normalized = _json_value(value)
    if normalized is None:
        return None
    if isinstance(normalized, str):
        lowered = normalized.strip().casefold()
        if lowered in {"0", "false"}:
            return False
        if lowered in {"1", "true"}:
            return True
    return bool(normalized)


def _sanitize_timings(
    value: Any,
    duration_ms: float | None,
) -> dict[str, float | None]:
    parsed: dict[str, Any] = {}
    normalized = _json_value(value)
    if isinstance(normalized, str) and normalized.strip():
        try:
            candidate = json.loads(normalized)
        except (TypeError, ValueError):
            candidate = {}
        if isinstance(candidate, dict):
            parsed = candidate
    elif isinstance(normalized, dict):
        parsed = normalized

    timings: dict[str, float | None] = {}
    for name in TIMING_FIELDS:
        raw = _json_value(parsed.get(name))
        try:
            measured = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            measured = None
        timings[name] = (
            round(measured, 3)
            if measured is not None and math.isfinite(measured) and measured >= 0
            else None
        )
    # The normalized top-level duration predates detailed stage telemetry and
    # remains authoritative for both old and new rows.
    timings["total_server_ms"] = (
        round(duration_ms, 3) if duration_ms is not None else None
    )
    return timings


def _outcome(row: pd.Series) -> str:
    status = str(_json_value(row.get("status"), "missing")).casefold()
    if status not in {"success", "successful", "ok"}:
        return "failure" if status != "missing" else "telemetry_missing"
    return (
        "zero_result" if _json_value(row.get("total_results"), 0) == 0 else "fulfilled"
    )


def _request_kind(query: str, filters: dict[str, Any]) -> str:
    if normalize_query(query):
        return "text_search"
    if any(filters.get(name) not in {None, ""} for name in _BROWSE_FILTER_FIELDS):
        return "filtered_browse"
    if str(filters.get("target_ad_type") or "offer").casefold() != "offer":
        return "filtered_browse"
    return "catalogue_browse"


def _browse_label(filters: dict[str, Any], request_kind: str) -> str:
    if request_kind == "catalogue_browse":
        return "Catalogue browse"

    details = []
    category = filters.get("subcategory") or filters.get("main_category")
    location = filters.get("locality") or filters.get("city") or filters.get("state")
    if category:
        details.append(str(category))
    elif filters.get("subcategory_id") is not None:
        details.append(f"subcategory #{filters['subcategory_id']}")
    if location:
        details.append(str(location))
    elif filters.get("city_id") is not None:
        details.append(f"city #{filters['city_id']}")
    if filters.get("rental_duration"):
        details.append(str(filters["rental_duration"]))
    target_ad_type = str(filters.get("target_ad_type") or "offer").casefold()
    if target_ad_type == "wanted":
        details.append("wanted listings")
    return "Filtered browse" + (f": {', '.join(details)}" if details else "")


def _build_record(
    row: pd.Series,
    enrichments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    query = str(_json_value(row.get("query_text"), ""))
    normalized = normalize_query(query)
    categories = classify_search_query(query)
    total_results = int(_json_value(row.get("total_results"), 0) or 0)
    total_tokens = int(_json_value(row.get("total_tokens"), 0) or 0)
    duration_value = _json_value(row.get("duration_ms"))
    duration_ms = float(duration_value) if duration_value is not None else None
    api_call_count = int(_json_value(row.get("api_call_count"), 0) or 0)
    input_tokens = int(_json_value(row.get("input_tokens"), 0) or 0)
    output_tokens = int(_json_value(row.get("output_tokens"), 0) or 0)
    thought_tokens = int(_json_value(row.get("thought_tokens"), 0) or 0)
    execution_path = _json_value(row.get("execution_path"), "missing")
    attempts = _sanitize_attempts(row.get("attempts_json"))
    filter_context = _sanitize_filter_context(row.get("context_json"))
    request_kind = _request_kind(query, filter_context)
    stage_timings = _sanitize_timings(row.get("timings_json"), duration_ms)
    successful_attempts = sum(
        str(attempt.get("status") or "").casefold()
        in {"success", "successful", "ok", "cache_hit"}
        for attempt in attempts
    )
    failed_attempts = len(attempts) - successful_attempts
    tokens_per_result = (
        round(total_tokens / total_results, 2) if total_results else None
    )

    record = {
        "search_id": _json_value(row.get("id_query"), _json_value(row.get("id"))),
        "request_id": str(_json_value(row.get("request_id"), "")),
        "query": query,
        "normalized_query": normalized,
        "request_kind": request_kind,
        "created_at": str(
            _json_value(
                row.get("created_at_query"), _json_value(row.get("created_at"), "")
            )
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
            "is_browse": request_kind != "text_search",
        },
        "outcome": _outcome(row),
        "filters": filter_context,
        # Stable, explicitly named internal projections. ``api`` below stays
        # available for the deployed frontend during the additive rollout.
        # The duration is measured with time.perf_counter around server-side
        # search processing; it is not client/network round-trip time.
        "performance": {
            "server_duration_ms": (
                round(duration_ms, 3) if duration_ms is not None else None
            ),
            "total_server_duration_ms": (
                round(duration_ms, 3) if duration_ms is not None else None
            ),
            "measurement_scope": "server_search_processing",
            "timing_semantics": "stages_may_overlap_do_not_sum",
            "execution_path": execution_path,
            "cache": {
                "plan_hit": _cache_value(row.get("plan_cache_hit")),
                "result_hit": _cache_value(row.get("result_cache_hit")),
            },
            "stages_ms": stage_timings,
            "downstream_api_calls": api_call_count,
            "attempt_count": len(attempts),
            "successful_attempt_count": successful_attempts,
            "failed_attempt_count": failed_attempts,
        },
        "token_usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "thought_tokens": thought_tokens,
            "total_tokens": total_tokens,
            "tokens_per_result": tokens_per_result,
        },
        "api": {
            "status": _json_value(row.get("status"), "missing"),
            "execution_path": execution_path,
            "result_count": int(_json_value(row.get("result_count"), 0) or 0),
            "total_results": total_results,
            "duration_ms": (round(duration_ms, 3) if duration_ms is not None else None),
            "api_call_count": api_call_count,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "thought_tokens": thought_tokens,
            "total_tokens": total_tokens,
            "tokens_per_result": tokens_per_result,
        },
        "attempts": attempts,
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
    city_names = {
        int(row["id"]): str(row["city"])
        for _, row in data.get("location", pd.DataFrame()).iterrows()
        if _json_value(row.get("id")) is not None
        and str(_json_value(row.get("city"), "")).strip()
    }
    subcategory_names = {
        int(row["id"]): str(row["name"])
        for _, row in data.get("sub_categories", pd.DataFrame()).iterrows()
        if _json_value(row.get("id")) is not None
        and str(_json_value(row.get("name"), "")).strip()
    }
    for record in records:
        filters = record["filters"]
        city_id = filters.get("city_id")
        subcategory_id = filters.get("subcategory_id")
        if city_id is not None and not filters.get("city"):
            filters["city"] = city_names.get(int(city_id))
        if subcategory_id is not None and not filters.get("subcategory"):
            filters["subcategory"] = subcategory_names.get(int(subcategory_id))
        record["filters"] = {
            name: value
            for name, value in filters.items()
            if value is not None and value != ""
        }
        if record["request_kind"] != "text_search":
            record["query"] = _browse_label(
                record["filters"],
                record["request_kind"],
            )
            for name in ("main_category", "subcategory"):
                value = record["filters"].get(name)
                if value and value not in record["categories"]:
                    record["categories"].append(str(value))
            for name in ("state", "city", "locality"):
                value = record["filters"].get(name)
                if value and value not in record["locations"]:
                    record["locations"].append(str(value))
    return {
        "metadata": {
            "schema_version": "2.0",
            "generated_at": datetime.now(UTC).isoformat(),
            "record_count": len(records),
            "fields": {
                "outcome": [
                    "fulfilled",
                    "zero_result",
                    "failure",
                    "telemetry_missing",
                ],
                "request_kind": [
                    "text_search",
                    "filtered_browse",
                    "catalogue_browse",
                ],
            },
        },
        "queries": records,
    }
