"""Extract deterministic price, ordering, and rental-duration constraints."""

import re

from search.planner_rules import (
    DURATION_PATTERNS,
)
from search.query_terms import (
    normalize_filter_value,
)


def extract_price_constraints(query: str) -> tuple[float | None, float | None]:
    normalized = query.casefold().replace(",", "")
    currency = r"(?:rs\.?|inr|₹)?\s*"
    number = r"(\d+(?:\.\d+)?)"

    range_match = re.search(
        rf"\bbetween\s+{currency}{number}\s+and\s+{currency}{number}",
        normalized,
    )
    if not range_match:
        range_match = re.search(
            rf"\bfrom\s+{currency}{number}\s+to\s+{currency}{number}",
            normalized,
        )
    if range_match:
        first, second = float(range_match.group(1)), float(range_match.group(2))
        return min(first, second), max(first, second)

    maximum_match = re.search(
        rf"\b(?:under|below|less\s+than|not\s+more\s+than|up\s+to|"
        rf"within|withing|budget(?:\s+of)?|maximum|max)\s+"
        rf"{currency}{number}",
        normalized,
    )
    if not maximum_match:
        maximum_match = re.search(
            rf"\b(?:(?:in|around)\s+(?:the\s+)?)?"
            rf"{currency}{number}\s+(?:price\s+)?range\b",
            normalized,
        )
    minimum_match = re.search(
        rf"\b(?:over|above|more\s+than|at\s+least|minimum|min)\s+"
        rf"{currency}{number}",
        normalized,
    )
    minimum = float(minimum_match.group(1)) if minimum_match else None
    maximum = float(maximum_match.group(1)) if maximum_match else None
    return minimum, maximum


def extract_sort_order(query: str) -> str | None:
    """Extract explicit price ordering without treating it as relevance text."""
    normalized = normalize_filter_value(query)
    price_term = r"(?:rental\s+)?(?:price|prices|rate|rates|rent|fee|fees)"
    low_to_high = (
        rf"(?:{price_term}\s+(?:from\s+)?low(?:est)?\s+to\s+high(?:est)?|"
        rf"low(?:est)?\s+to\s+high(?:est)?\s+{price_term})"
    )
    high_to_low = (
        rf"(?:{price_term}\s+(?:from\s+)?high(?:est)?\s+to\s+low(?:est)?|"
        rf"high(?:est)?\s+to\s+low(?:est)?\s+{price_term})"
    )
    if re.search(rf"\b{low_to_high}\b", normalized):
        return "price_asc"
    if re.search(rf"\b{high_to_low}\b", normalized):
        return "price_desc"
    if re.search(
        r"\b(?:l2h|low\s*2\s*high|lo\s+to\s+hi)\b",
        normalized,
    ):
        return "price_asc"
    if re.search(
        r"\b(?:h2l|high\s*2\s*low|hi\s+to\s+lo)\b",
        normalized,
    ):
        return "price_desc"
    ascending_patterns = (
        r"\b(?:cheapest|low(?:er|est)(?:[\s-]+priced)?|"
        r"least[\s-]+expensive)\b",
        r"\b(?:cheap|affordable|economical|inexpensive|"
        r"bargain(?:[\s-]+priced)?|budget[\s-]+friendly|"
        r"pocket[\s-]+friendly|reasonabl(?:e|y)[\s-]+priced|"
        r"low[\s-]+cost)\b",
        rf"\b(?:low|lowest|minimum|min)\s+{price_term}\b",
        r"\blow(?:est)?\s+(?:cost|costs)\b",
        rf"\b{price_term}\s+(?:from\s+)?low(?:est)?\s+to\s+high(?:est)?\b",
        rf"\blow(?:est)?\s+to\s+high(?:est)?\s+{price_term}\b",
        rf"\b(?:sort|order|sorted|ordered)(?:\s+by)?\s+{price_term}"
        rf"\s+(?:asc|ascending|low(?:est)?\s+to\s+high(?:est)?)\b",
        rf"\b(?:asc|ascending)\s+{price_term}\b",
        rf"\b{price_term}\s+(?:asc|ascending)\b",
    )
    descending_patterns = (
        r"\b(?:most[\s-]+expensive|high(?:er|est)(?:[\s-]+priced)?|"
        r"costliest|dearest|priciest|top[\s-]+priced)\b",
        rf"\b(?:high|highest|maximum|max)\s+{price_term}\b",
        rf"\b{price_term}\s+(?:from\s+)?high(?:est)?\s+to\s+low(?:est)?\b",
        rf"\bhigh(?:est)?\s+to\s+low(?:est)?\s+{price_term}\b",
        rf"\b(?:sort|order|sorted|ordered)(?:\s+by)?\s+{price_term}"
        rf"\s+(?:desc|descending|high(?:est)?\s+to\s+low(?:est)?)\b",
        rf"\b(?:desc|descending)\s+{price_term}\b",
        rf"\b{price_term}\s+(?:desc|descending)\b",
    )
    if any(re.search(pattern, normalized) for pattern in ascending_patterns):
        return "price_asc"
    if any(re.search(pattern, normalized) for pattern in descending_patterns):
        return "price_desc"
    return None


def extract_standalone_budget(query: str) -> float | None:
    """Infer one bare amount only for the conservative direct-filter path."""
    normalized = query.casefold().replace(",", "")
    if re.search(
        r"\b(?:cc|bhp|hp|km|kms|kilometers?|model|seater|year)\b",
        normalized,
    ):
        return None
    amounts = re.findall(
        r"(?<![\w.])(?:rs\.?|inr|₹)?\s*(\d+(?:\.\d+)?)(?![\w.])",
        normalized,
    )
    if len(amounts) != 1:
        return None
    amount = float(amounts[0])
    if amount < 10 or 1900 <= amount <= 2100:
        return None
    return amount


def extract_duration_filter(query: str, values: dict) -> str | None:
    normalized_query = normalize_filter_value(query)
    for canonical_value, pattern in DURATION_PATTERNS:
        if re.search(pattern, normalized_query):
            return (
                values.get(normalize_filter_value(canonical_value)) or canonical_value
            )
    return None
