"""Stable JSON encoding for analytics snapshots, rows, and cursors."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any


def _json_default(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value)


def json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )
