"""Search-domain analytics, classification, and query-level projections."""

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
]
