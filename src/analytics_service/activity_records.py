"""Compact audience-specific records used by dashboard calculations."""

import json
from typing import Any


def dashboard_activity_record(
    payload_json: str,
    *,
    internal: bool,
) -> dict[str, Any]:
    """Return only the fields used to calculate dashboard activity views.

    Query Explorer responses retain the complete, audience-specific payload in
    SQLite. A large nested JSON document becomes many Python objects, so this
    short-lived projection preserves the dashboard/filter contract without
    carrying query text, enrichment, flags, or unused telemetry through the
    aggregation step.
    """
    payload = json.loads(payload_json)
    filters = dict(payload.get("filters") or {})
    record: dict[str, Any] = {
        "created_at": payload.get("created_at"),
        "normalized_query": payload.get("normalized_query"),
        "request_kind": payload.get("request_kind"),
        "outcome": payload.get("outcome"),
        "categories": list(payload.get("categories") or ()),
        "language": payload.get("language"),
        "filters": {
            name: filters[name]
            for name in (
                "main_category",
                "subcategory",
                "subcategory_id",
                "city",
                "city_id",
                "target_ad_type",
            )
            if name in filters
        },
    }
    if not internal:
        search = dict(payload.get("search") or {})
        record["search"] = {
            "result_count": search.get("result_count"),
            "total_results": search.get("total_results"),
        }
        return record

    api = dict(payload.get("api") or {})
    performance = dict(payload.get("performance") or {})
    cache = dict(performance.get("cache") or {})
    record["api"] = {"status": api.get("status")}
    record["performance"] = {
        "execution_path": performance.get("execution_path"),
        "total_server_duration_ms": performance.get("total_server_duration_ms"),
        "downstream_api_calls": performance.get("downstream_api_calls"),
        "cache": {
            name: cache[name]
            for name in ("plan_hit", "result_hit")
            if cache.get(name) is not None
        },
        "stages_ms": {
            str(name): value
            for name, value in dict(performance.get("stages_ms") or {}).items()
            if isinstance(value, (int, float))
        },
    }
    record["attempts"] = [
        {
            name: attempt.get(name)
            for name in (
                "provider",
                "operation",
                "api_calls",
                "input_tokens",
                "output_tokens",
                "thought_tokens",
                "total_tokens",
            )
        }
        for attempt in payload.get("attempts") or ()
        if isinstance(attempt, dict)
    ]
    diagnostics = dict(payload.get("diagnostics") or {})
    record["diagnostics"] = {
        "code": diagnostics.get("code"),
        "evidence_complete": diagnostics.get("evidence_complete"),
    }
    return record
