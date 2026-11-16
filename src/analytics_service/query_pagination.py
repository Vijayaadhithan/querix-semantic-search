"""Versioned query cursors and validated SQL sort expressions."""

from __future__ import annotations

import base64
import json

from analytics_service.serialization import (
    json_dumps,
)


def encode_query_cursor(created_at: str, request_id: str) -> str:
    payload = json_dumps(
        {"v": 1, "created_at": created_at, "request_id": request_id}
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_query_cursor(cursor: str) -> tuple[str, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode())
        if payload.get("v") != 1:
            raise ValueError("Unsupported cursor version")
        created_at = str(payload["created_at"])
        request_id = str(payload["request_id"])
        if not created_at or not request_id:
            raise ValueError("Incomplete cursor")
        return created_at, request_id
    except Exception as exc:
        raise ValueError("Invalid analytics query cursor") from exc


def encode_query_sort_cursor(sort_by: str, sort_direction: str, offset: int) -> str:
    payload = json_dumps(
        {
            "v": 2,
            "sort_by": sort_by,
            "sort_direction": sort_direction,
            "offset": offset,
        }
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_query_sort_cursor(cursor: str) -> tuple[str, str, int]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode())
        if payload.get("v") != 2:
            raise ValueError("Unsupported cursor version")
        sort_by = str(payload["sort_by"])
        sort_direction = str(payload["sort_direction"])
        offset = int(payload["offset"])
        if not sort_by or sort_direction not in {"asc", "desc"} or offset < 0:
            raise ValueError("Incomplete cursor")
        return sort_by, sort_direction, offset
    except Exception as exc:
        raise ValueError("Invalid analytics query cursor") from exc


def _query_sort_expression(sort_by: str, *, internal: bool) -> str:
    common = {
        "created_at": "records.created_at",
        "outcome": "records.outcome",
        "results": (
            "json_extract(records.internal_json, '$.api.result_count')"
            if internal
            else "json_extract(records.company_json, '$.search.result_count')"
        ),
    }
    internal_only = {
        "execution_path": (
            "NULLIF(json_extract(records.internal_json, "
            "'$.performance.execution_path'), '')"
        ),
        "duration": (
            "json_extract(records.internal_json, "
            "'$.performance.total_server_duration_ms')"
        ),
        "tokens": "json_extract(records.internal_json, '$.token_usage.total_tokens')",
    }
    expressions = {**common, **(internal_only if internal else {})}
    try:
        return expressions[sort_by]
    except KeyError as exc:
        allowed = ", ".join(expressions)
        raise ValueError(f"Query sort field must be one of: {allowed}") from exc
