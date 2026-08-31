"""Shared compatibility utilities.

Search classification now lives in ``analytics.search.classification``. The
re-exports keep existing report modules and downstream imports stable.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from .search.classification import (
    classify_search_query,
    detect_language,
    detect_rental_duration,
    extract_brands,
    extract_locations,
    has_typo,
    is_b2b_query,
    is_gibberish,
    is_route_query,
    is_service_query,
)

__all__ = [
    "classify_search_query",
    "detect_language",
    "detect_rental_duration",
    "extract_brands",
    "extract_locations",
    "has_typo",
    "is_b2b_query",
    "is_gibberish",
    "is_route_query",
    "is_service_query",
    "parse_attempts_json",
]


def parse_attempts_json(value: Any) -> list[dict[str, Any]]:
    """Parse provider attempts without letting malformed rows fail a build."""
    if value is None or (not isinstance(value, (str, list)) and pd.isna(value)):
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]

    raw = str(value).strip()
    if not raw or raw == "[]":
        return []

    candidates = [raw]
    cleaned = raw.replace('""', '"')
    if cleaned.startswith('"') and cleaned.endswith('"'):
        cleaned = cleaned[1:-1]
    if cleaned != raw:
        candidates.append(cleaned)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    return []
