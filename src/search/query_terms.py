"""Normalize query values and interpret explicit category and location wording."""

import re

from search.planner_rules import (
    CATEGORY_ATTRIBUTE_PREFIXES,
    GENERIC_LOCATION_VALUES,
)


def normalize_filter_value(value) -> str:
    return " ".join(str(value).casefold().split())


def optional_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def optional_number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def optional_user_gender(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        gender = int(value)
    except (TypeError, ValueError):
        return None
    return gender if gender in {1, 2, 3} else None


def text_mentions_filter(text: str, value: str) -> bool:
    normalized_text = normalize_filter_value(text)
    normalized_value = normalize_filter_value(value)
    if normalized_value in normalized_text:
        return True
    compact_text = re.sub(r"\W+", "", normalized_text)
    compact_value = re.sub(r"\W+", "", normalized_value)
    return bool(compact_value and compact_value in compact_text)


def category_term_pattern(value: str) -> str:
    normalized = normalize_filter_value(value)
    escaped = re.escape(normalized)
    if re.fullmatch(r"[a-z0-9_-]+", normalized):
        if re.search(r"[^aeiou]y$", normalized):
            return rf"{re.escape(normalized[:-1])}(?:y|ies)"
        if normalized.endswith(("s", "x", "z", "ch", "sh")):
            return rf"{escaped}(?:es)?"
        return rf"{escaped}s?"
    return escaped


def is_repair_subject_usage(query: str, value: str) -> bool:
    """Return true when a catalogue noun is the object being repaired."""

    normalized_value = normalize_filter_value(value)
    if re.search(
        r"\b(?:repair|service|maintenance|mechanic|technician|plumber|"
        r"electrician|serviceman|servicemen)\b",
        normalized_value,
    ):
        # The catalogue value itself represents a repair/service listing.
        return False
    normalized_query = normalize_filter_value(query)
    term = category_term_pattern(value)
    action = (
        r"(?:repair(?:s|ed|ing)?|fix(?:es|ed|ing)?|"
        r"servic(?:e|es|ed|ing)|maintain(?:s|ed|ing)?|maintenance)"
    )
    return bool(
        re.search(
            rf"\b{action}\b(?:\s+[a-z0-9_-]+){{0,3}}\s+{term}(?!\w)",
            normalized_query,
        )
        or re.search(
            rf"(?<!\w){term}\s+(?:(?:that|which)\s+)?"
            rf"(?:(?:need|needs|require|requires)\s+)?(?:for\s+)?"
            rf"{action}\b",
            normalized_query,
        )
    )


def is_explicit_category_request(query: str, value: str) -> bool:
    if is_repair_subject_usage(query, value):
        return False
    normalized_query = normalize_filter_value(query)
    term = category_term_pattern(value)
    attribute = (
        "(?:"
        + "|".join(re.escape(prefix) for prefix in sorted(CATEGORY_ATTRIBUTE_PREFIXES))
        + ")"
    )
    article = r"(?:a|an|the|some)?\s*"
    request = (
        r"(?:need|want|require|rent|hire|find|show\s+me|"
        r"looking\s+for|searching\s+for|interested\s+in)"
    )
    patterns = (
        rf"^{article}{term}(?!\w)",
        rf"\b{request}\s+{article}{term}(?!\w)",
        rf"\b(?:wanted|request)\s+ads?\s+for\s+{article}{term}(?!\w)",
        rf"\b(?:rental|hire)\s+{term}(?!\w)",
        rf"(?<!\w){attribute}\s+{term}(?!\w)",
        rf"(?<!\w){term}\s+(?:with|without|having|equipped\s+with)\b",
        rf"(?<!\w){term}\s+(?:rent|hire|for\s+(?:rent|hire)|rental|"
        rf"in|near|at|under|below|within|between|per\s+hour|"
        rf"per\s+day|per\s+week|per\s+month)\b",
        rf"\b(?:budget|cheap|affordable|low[\s-]?cost)\s+{term}(?!\w)",
        rf"(?<!\w){term}\s+for\b",
        rf"\b\d+(?:\.\d+)?\s+{term}(?!\w)",
        rf"(?<!\w){term}\s+\d+(?:\.\d+)?\b",
    )
    return any(re.search(pattern, normalized_query) for pattern in patterns)


def is_category_attribute_usage(
    query: str,
    value: str,
    value_index: dict,
) -> bool:
    normalized_value = normalize_filter_value(value)
    if normalized_value not in CATEGORY_ATTRIBUTE_PREFIXES:
        return False
    normalized_query = normalize_filter_value(query)
    for key in ("main_category", "subcategory"):
        for category in value_index.get(key, {}).values():
            term = category_term_pattern(category)
            if re.search(
                rf"(?<!\w){re.escape(normalized_value)}\s+{term}(?!\w)",
                normalized_query,
            ):
                return True
    return False


def is_generic_location_value(value: str | None) -> bool:
    return bool(value and normalize_filter_value(value) in GENERIC_LOCATION_VALUES)


def normalize_transliterated_query(
    query: str,
    query_aliases: dict[str, str] | None = None,
    rewrite_rules: tuple[tuple[re.Pattern, str], ...] = (),
) -> str:
    """Normalize confirmed marketplace phrases to their search meaning."""
    normalized = query
    for pattern, replacement in rewrite_rules:
        normalized = pattern.sub(replacement, normalized)
    for source, target in sorted(
        (query_aliases or {}).items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        normalized = re.sub(
            rf"(?<!\w){re.escape(source)}(?!\w)",
            target,
            normalized,
            flags=re.IGNORECASE,
        )
    return " ".join(normalized.split())
