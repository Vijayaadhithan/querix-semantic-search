"""Deterministic, quota-free search-query classification.

These rules intentionally remain the first stage even when AI enrichment is
enabled. They are fast, explainable, and guarantee that dashboard generation
does not depend on an external service.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from ..config import (
    BRAND_KEYWORDS,
    LOCATION_KEYWORDS,
    SEARCH_CATEGORIES,
    TYPO_INDICATORS,
)

_SPACE_PATTERN = re.compile(r"\s+")
_NON_WORD_PATTERN = re.compile(r"[^\w\s-]", re.UNICODE)


def normalize_query(query: Any) -> str:
    if query is None:
        return ""
    text = str(query).casefold().strip()
    text = _NON_WORD_PATTERN.sub(" ", text)
    return _SPACE_PATTERN.sub(" ", text).strip()


@lru_cache(maxsize=4096)
def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    escaped = re.escape(normalize_query(keyword)).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


def _contains(text: str, keyword: str) -> bool:
    return bool(_keyword_pattern(keyword).search(text))


def classify_search_query(query: Any) -> list[str]:
    normalized = normalize_query(query)
    matches = [
        category
        for category, keywords in SEARCH_CATEGORIES.items()
        if any(_contains(normalized, keyword) for keyword in keywords)
    ]
    return matches or ["Other / Uncategorized"]


def extract_brands(query: Any) -> list[str]:
    normalized = normalize_query(query)
    return [
        brand
        for brand, keywords in BRAND_KEYWORDS.items()
        if any(_contains(normalized, keyword) for keyword in keywords)
    ]


def extract_locations(query: Any) -> list[str]:
    normalized = normalize_query(query)
    return [
        location.title()
        for location in LOCATION_KEYWORDS
        if _contains(normalized, location)
    ]


def detect_rental_duration(query: Any) -> str:
    normalized = normalize_query(query)
    duration_keywords = (
        ("Hourly", ("hourly", "per hour")),
        ("Daily", ("daily", "per day", "one day")),
        ("Weekly", ("weekly", "per week")),
        ("Monthly", ("monthly", "per month")),
    )
    for label, keywords in duration_keywords:
        if any(_contains(normalized, keyword) for keyword in keywords):
            return label
    return "Rent (unspecified)" if _contains(normalized, "rent") else "Not specified"


def is_gibberish(query: Any) -> bool:
    normalized = normalize_query(query)
    if len(normalized) < 3:
        return True
    if re.search(r"(.)\1{5,}", normalized):
        return True
    original = "" if query is None else str(query).strip()
    if len(original) > 5:
        valid_count = sum(
            character.isalnum() or character.isspace() for character in original
        )
        if valid_count / len(original) < 0.5:
            return True
    if normalized in {"ddun", "vfl", "autolpg"}:
        return True
    compact = normalized.replace(" ", "")
    return len(normalized) > 100 and len(set(compact)) < 10


def has_typo(query: Any) -> bool:
    normalized = normalize_query(query)
    return any(_contains(normalized, typo) for typo, _ in TYPO_INDICATORS)


def is_route_query(query: Any) -> bool:
    normalized = normalize_query(query)
    return " to " in f" {normalized} " and bool(extract_locations(normalized))


def is_service_query(query: Any) -> bool:
    normalized = normalize_query(query)
    keywords = (
        "hire",
        "service",
        "master",
        "driver",
        "electrician",
        "plumber",
        "cook",
        "therapist",
        "trainer",
        "tutor",
        "labour",
        "worker",
        "helper",
        "companion",
        "photographer",
        "instructor",
        "job",
        "work",
        "freelance",
        "nurse",
        "doctor",
    )
    return any(_contains(normalized, keyword) for keyword in keywords)


def detect_language(query: Any) -> str:
    text = "" if query is None else str(query)
    if re.search(r"[\u0B80-\u0BFF]", text):
        return "Tamil"
    if re.search(r"[\u0900-\u097F]", text):
        return "Hindi"
    normalized = normalize_query(text)
    transliterated_tamil = (
        "vandi",
        "thurai",
        "palayam",
        "nagar",
        "puram",
        "kudi",
        "kottai",
        "malai",
        "oor",
        "pattinam",
    )
    if any(_contains(normalized, word) for word in transliterated_tamil):
        return "Transliterated Tamil"
    return "English"


def is_b2b_query(query: Any) -> bool:
    normalized = normalize_query(query)
    return any(
        _contains(normalized, keyword)
        for keyword in (
            "bulk",
            "commercial",
            "business",
            "company",
            "corporate",
            "industrial",
            "wholesale",
            "contract",
        )
    )
