import json
import re
from copy import copy
from difflib import SequenceMatcher, get_close_matches

from core.settings import QUERY_FUZZY_MATCHING
from search.bm25 import PersistentBM25Index
from search.planner_rules import (
    CATEGORY_ATTRIBUTE_PREFIXES,
    CATEGORY_LEADING_MODIFIER_TOKENS,
    DURATION_PATTERNS,
    FUZZY_MATCH_THRESHOLDS,
    GENERIC_CATEGORY_HINT_TOKENS,
    GENERIC_LOCATION_VALUES,
    LOCATION_PREPOSITIONS,
    LOCATION_STOP_WORDS,
    QUERY_FILTER_ALIASES,
    QUERY_FILTER_FIELDS,
    QUERY_FILTER_KEYS,
    TRANSLITERATED_QUERY_REWRITES,
)


class CatalogValueMap(dict):
    """Catalogue values with stable ordering and regexes built once."""

    def __init__(self, values: dict, allow_plural: bool = False):
        super().__init__(values)
        self.allow_plural = allow_plural
        self.longest_first = tuple(
            sorted(self.items(), key=lambda item: len(item[0]), reverse=True)
        )
        pattern_text = category_term_pattern if allow_plural else re.escape
        self.match_patterns = tuple(
            (
                re.compile(rf"(?<!\w){pattern_text(normalized_value)}(?!\w)"),
                actual_value,
            )
            for normalized_value, actual_value in self.longest_first
        )


class QueryFilterCatalog(dict):
    """Small planner catalog with its stable JSON payload prepared once."""

    def __init__(self, values: dict):
        super().__init__(values)
        self.json_text = json.dumps(self, ensure_ascii=False)


def normalize_filter_value(value) -> str:
    return " ".join(str(value).casefold().split())


def default_query_plan(query: str, fallback_reason: str | None = None) -> dict:
    return {
        "semantic_query": query,
        "keyword_query": query,
        "target_ad_type": "offer",
        "sort_order": None,
        "filters": dict.fromkeys(QUERY_FILTER_KEYS),
        "inferred_categories": {
            "main_category": None,
            "subcategory": None,
        },
        "fallback_reason": fallback_reason,
    }


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
        r"looking\s+for|searching\s+for)"
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


def parse_query_plan(content: str, original_query: str) -> dict:
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("query extraction response must be a JSON object")

    semantic_query = optional_text(parsed.get("semantic_query")) or original_query
    keyword_query = optional_text(parsed.get("keyword_query")) or original_query
    target_ad_type = parsed.get("target_ad_type")
    if target_ad_type not in {"offer", "wanted"}:
        target_ad_type = "offer"
    raw_filters = parsed.get("filters")
    if not isinstance(raw_filters, dict):
        raw_filters = {}

    filters = {key: optional_text(raw_filters.get(key)) for key in QUERY_FILTER_FIELDS}
    inferred_categories = {
        "main_category": None,
        "subcategory": None,
    }
    for category_key in inferred_categories:
        value = filters[category_key]
        if value is not None and not is_explicit_category_request(
            original_query,
            value,
        ):
            inferred_categories[category_key] = value
            filters[category_key] = None
    for parent_key in ("main_category", "state"):
        value = filters[parent_key]
        if value is not None and not text_mentions_filter(original_query, value):
            filters[parent_key] = None
    filters["min_rental_fee"] = optional_number(raw_filters.get("min_rental_fee"))
    filters["max_rental_fee"] = optional_number(raw_filters.get("max_rental_fee"))
    minimum = filters["min_rental_fee"]
    maximum = filters["max_rental_fee"]
    if minimum is not None and maximum is not None and minimum > maximum:
        filters["min_rental_fee"], filters["max_rental_fee"] = maximum, minimum

    return {
        "semantic_query": semantic_query,
        "keyword_query": keyword_query,
        "target_ad_type": target_ad_type,
        "filters": filters,
        "inferred_categories": inferred_categories,
        "fallback_reason": None,
    }


def normalize_transliterated_query(
    query: str,
    query_aliases: dict[str, str] | None = None,
) -> str:
    """Normalize confirmed marketplace phrases to their search meaning."""
    normalized = query
    for pattern, replacement in TRANSLITERATED_QUERY_REWRITES:
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


def find_catalog_value(
    query: str,
    values: dict,
    allow_plural: bool = False,
) -> str | None:
    normalized_query = normalize_filter_value(query)
    compiled_patterns = getattr(values, "match_patterns", None)
    if (
        compiled_patterns is not None
        and getattr(values, "allow_plural", False) == allow_plural
    ):
        for pattern, actual_value in compiled_patterns:
            if pattern.search(normalized_query):
                return actual_value
        return None
    ordered_values = getattr(values, "longest_first", None)
    if ordered_values is None:
        ordered_values = sorted(
            values.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        )
    for normalized_value, actual_value in ordered_values:
        escaped_value = re.escape(normalized_value)
        if allow_plural and re.fullmatch(r"[a-z0-9_-]+", normalized_value):
            if re.search(r"[^aeiou]y$", normalized_value):
                escaped_value = rf"{re.escape(normalized_value[:-1])}(?:y|ies)"
            elif normalized_value.endswith(("s", "x", "z", "ch", "sh")):
                escaped_value = rf"{escaped_value}(?:es)?"
            else:
                escaped_value = rf"{escaped_value}s?"
        pattern = rf"(?<!\w){escaped_value}(?!\w)"
        if re.search(pattern, normalized_query):
            return actual_value
    return None


def _category_concept_token(token: str) -> str:
    """Normalize conservative noun variants used in catalog category names."""
    token = normalize_filter_value(token)
    # Common derivational pair: users naturally say "music instrument" while
    # catalogs generally call the category "Musical Instruments".
    if token == "musical":
        return "music"
    if token.endswith("ies") and len(token) > 4:
        return f"{token[:-3]}y"
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        return token[:-1]
    return token


def category_catalog_matches(query: str, values: dict) -> list[dict]:
    """Return category concepts mentioned in a query, including safe variants."""
    query_tokens = re.findall(r"[^\W_]+", normalize_filter_value(query))
    normalized_query_tokens = [_category_concept_token(token) for token in query_tokens]
    matches = []
    for actual_value in values.values():
        value_tokens = re.findall(
            r"[^\W_]+",
            normalize_filter_value(actual_value),
        )
        normalized_value_tokens = [
            _category_concept_token(token) for token in value_tokens
        ]
        width = len(normalized_value_tokens)
        if not width or width > len(normalized_query_tokens):
            continue
        for start in range(len(normalized_query_tokens) - width + 1):
            if (
                normalized_query_tokens[start : start + width]
                != normalized_value_tokens
            ):
                continue
            end = start + width
            span_at_head = start == 0 or all(
                token in CATEGORY_LEADING_MODIFIER_TOKENS
                or token
                in {
                    "a",
                    "an",
                    "find",
                    "hire",
                    "looking",
                    "need",
                    "searching",
                    "show",
                    "the",
                    "want",
                }
                for token in query_tokens[:start]
            )
            followed_by_constraint = end < len(query_tokens) and query_tokens[end] in {
                "at",
                "equipped",
                "for",
                "having",
                "in",
                "near",
                "under",
                "with",
                "without",
            }
            matches.append(
                {
                    "value": actual_value,
                    "start": start,
                    "end": end,
                    "width": width,
                    "explicit": bool(
                        not is_repair_subject_usage(query, actual_value)
                        and (
                            span_at_head
                            or followed_by_constraint
                            or is_explicit_category_request(query, actual_value)
                        )
                    ),
                }
            )
            break
    return sorted(
        matches,
        key=lambda match: (
            bool(match["explicit"]),
            int(match["width"]),
            -int(match["start"]),
            len(str(match["value"])),
        ),
        reverse=True,
    )


class QueryAnalysis:
    """Original-query facts that are safe to reuse across planner passes."""

    def __init__(
        self,
        query: str,
        value_index: dict,
        query_aliases: dict[str, str] | None = None,
        normalized_query: str | None = None,
    ):
        self.original_query = query
        self.query = normalized_query or normalize_transliterated_query(
            query,
            query_aliases,
        )
        self.query_was_normalized = self.query.casefold() != query.casefold()
        self.exact_values = {}
        self.category_is_explicit = {}
        self.category_matches = {}
        self.clear_model_location_filter = {}
        for key in QUERY_FILTER_FIELDS:
            if key == "rental_duration":
                continue
            category_matches = (
                category_catalog_matches(self.query, value_index[key])
                if key in {"main_category", "subcategory"}
                else []
            )
            self.category_matches[key] = category_matches
            exact_value = (
                category_matches[0]["value"]
                if category_matches
                else find_catalog_value(
                    self.query,
                    value_index[key],
                    allow_plural=key in {"main_category", "subcategory"},
                )
            )
            if exact_value is None:
                exact_value = find_catalog_alias(
                    self.query,
                    key,
                    value_index[key],
                )
            if key in {"state", "city", "locality"} and is_generic_location_value(
                exact_value
            ):
                exact_value = None
            clear_model_location_filter = bool(
                key in {"state", "city", "locality"}
                and exact_value is not None
                and is_category_attribute_usage(
                    self.query,
                    exact_value,
                    value_index,
                )
            )
            if clear_model_location_filter:
                exact_value = None
            self.exact_values[key] = exact_value
            self.category_is_explicit[key] = bool(
                category_matches and category_matches[0]["explicit"]
            )
            self.clear_model_location_filter[key] = clear_model_location_filter
        main_matches = self.category_matches.get("main_category") or []
        subcategory_matches = self.category_matches.get("subcategory") or []
        if main_matches and subcategory_matches:
            main_match = main_matches[0]
            subcategory_match = subcategory_matches[0]
            overlaps = int(main_match["start"]) < int(subcategory_match["end"]) and int(
                subcategory_match["start"]
            ) < int(main_match["end"])
            if overlaps and int(main_match["width"]) > int(subcategory_match["width"]):
                # Prefer the complete head concept ("Musical Instruments")
                # over a shorter overlapping catalog value (Books -> "Music").
                self.exact_values["subcategory"] = None
                self.category_is_explicit["subcategory"] = False
        self.rental_duration = extract_duration_filter(
            self.query,
            value_index["rental_duration"],
        )
        self.price_constraints = extract_price_constraints(self.query)
        self._fuzzy_location_ready = False
        self._fuzzy_location = None

    def fuzzy_location(self, value_index: dict):
        if not self._fuzzy_location_ready:
            fuzzy_location = find_fuzzy_location(self.query, value_index)
            if fuzzy_location is not None and is_category_attribute_usage(
                self.query,
                fuzzy_location[1],
                value_index,
            ):
                fuzzy_location = None
            self._fuzzy_location = fuzzy_location
            self._fuzzy_location_ready = True
        return self._fuzzy_location


def query_analysis(
    query: str,
    value_index: dict,
    query_aliases: dict[str, str] | None = None,
    cache: dict[tuple[str, str], QueryAnalysis] | None = None,
) -> QueryAnalysis:
    normalized_query = normalize_transliterated_query(query, query_aliases)
    cache_key = (
        normalize_filter_value(query),
        normalize_filter_value(normalized_query),
    )
    if cache is not None and cache_key in cache:
        analysis = copy(cache[cache_key])
        # Matching facts are case/whitespace insensitive, but enrichment must
        # retain the exact surface text used by each pass.
        analysis.original_query = query
        analysis.query = normalized_query
        analysis.query_was_normalized = normalized_query.casefold() != query.casefold()
        return analysis
    analysis = QueryAnalysis(
        query,
        value_index,
        query_aliases,
        normalized_query,
    )
    if cache is not None:
        cache[cache_key] = analysis
    return analysis


def canonical_catalog_value(
    query_key: str,
    requested_value: str,
    values: dict,
    allow_fuzzy: bool = True,
) -> str | None:
    normalized = normalize_filter_value(requested_value)
    normalized = QUERY_FILTER_ALIASES.get(query_key, {}).get(
        normalized,
        normalized,
    )
    exact = values.get(normalized)
    if exact is not None or not allow_fuzzy:
        return exact

    threshold = FUZZY_MATCH_THRESHOLDS.get(query_key)
    if threshold is None or len(normalized) < 4:
        return None
    match = fuzzy_catalog_match(normalized, values, threshold)
    return match[0] if match is not None else None


def fuzzy_catalog_match(
    normalized: str,
    values: dict,
    threshold: float,
) -> tuple[str, float] | None:
    matches = get_close_matches(
        normalized,
        values.keys(),
        n=2,
        cutoff=threshold,
    )
    if not matches:
        return None
    first_score = SequenceMatcher(None, normalized, matches[0]).ratio()
    if len(matches) > 1:
        second_score = SequenceMatcher(None, normalized, matches[1]).ratio()
        if first_score - second_score < 0.04:
            return None
    return values[matches[0]], first_score


def edit_distance(left: str, right: str) -> int:
    """Return Levenshtein distance for short catalog terms."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def ordered_subsequence(shorter: str, longer: str) -> bool:
    iterator = iter(longer)
    return all(character in iterator for character in shorter)


def typo_catalog_match(
    normalized: str,
    values: dict,
    single_token_only: bool = False,
) -> tuple[str, float] | None:
    """Match a likely typo while rejecting close or ambiguous catalog values."""
    normalized = normalize_filter_value(normalized)
    if len(normalized) < 3:
        return None

    candidates: dict[str, tuple[str, float]] = {}
    variants = [normalized]
    if normalized.endswith("s") and len(normalized) > 3:
        variants.append(normalized[:-1])

    for candidate, actual in values.items():
        if single_token_only and " " in candidate:
            continue
        best_score = 0.0
        for variant in variants:
            length = max(len(variant), len(candidate))
            if abs(len(variant) - len(candidate)) > 3:
                continue
            distance = edit_distance(variant, candidate)
            max_edits = 1 if length <= 5 else 2
            if distance <= max_edits:
                best_score = max(
                    best_score,
                    0.98 - (0.02 * distance),
                )
            if (
                len(variant) >= 3
                and len(candidate) - len(variant) in range(1, 4)
                and variant[:2] == candidate[:2]
                and variant[-1] == candidate[-1]
                and ordered_subsequence(variant, candidate)
            ):
                best_score = max(best_score, 0.92)
        if best_score:
            identity = normalize_filter_value(actual)
            current = candidates.get(identity)
            if current is None or best_score > current[1]:
                candidates[identity] = (actual, best_score)

    ranked = sorted(
        candidates.values(),
        key=lambda item: (item[1], -len(str(item[0]))),
        reverse=True,
    )
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 0.04:
        return None
    return ranked[0]


def location_phrases(query: str) -> list[str]:
    tokens = re.findall(r"[^\W_]+", normalize_filter_value(query))
    phrases = []
    for index, token in enumerate(tokens):
        if token not in LOCATION_PREPOSITIONS:
            continue
        location_tokens = []
        for candidate in tokens[index + 1 : index + 5]:
            if candidate in LOCATION_STOP_WORDS:
                break
            location_tokens.append(candidate)
        if not location_tokens or location_tokens[0].isdigit():
            continue
        phrases.extend(
            " ".join(location_tokens[:length])
            for length in range(len(location_tokens), 0, -1)
        )
    return list(dict.fromkeys(phrases))


def find_fuzzy_location(query: str, value_index: dict) -> tuple[str, str] | None:
    candidates = []
    key_priority = {"city": 3, "state": 2, "locality": 1}
    for phrase in location_phrases(query):
        for key in ("city", "state", "locality"):
            threshold = FUZZY_MATCH_THRESHOLDS[key]
            match = fuzzy_catalog_match(
                phrase,
                value_index[key],
                threshold,
            )
            if match is None and QUERY_FUZZY_MATCHING:
                match = typo_catalog_match(phrase, value_index[key])
            if match is not None:
                actual, score = match
                if is_generic_location_value(actual):
                    continue
                candidates.append((score, key_priority[key], key, actual))
    if not candidates:
        return None

    unique_candidates = {}
    for candidate in candidates:
        identity = normalize_filter_value(candidate[3])
        if candidate > unique_candidates.get(identity, (-1, -1, "", "")):
            unique_candidates[identity] = candidate
    candidates = list(unique_candidates.values())
    candidates.sort(reverse=True)
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.03:
        return None
    _, _, key, actual = candidates[0]
    return key, actual


def correct_explicit_query_typos(
    query: str,
    value_index: dict,
) -> tuple[str, list[dict[str, str]]]:
    """Correct conservative location typos before deterministic planning."""
    if not QUERY_FUZZY_MATCHING:
        return query, []

    corrected = normalize_filter_value(query)
    corrections: list[dict[str, str]] = []

    location_candidates = []
    key_priority = {"city": 3, "state": 2, "locality": 1}
    for phrase in location_phrases(corrected):
        for key in ("city", "state", "locality"):
            if (
                canonical_catalog_value(
                    key,
                    phrase,
                    value_index[key],
                    allow_fuzzy=False,
                )
                is not None
            ):
                continue
            match = typo_catalog_match(phrase, value_index[key])
            if match is not None:
                actual, score = match
                location_candidates.append(
                    (score, key_priority[key], key, phrase, actual)
                )
    location_candidates.sort(reverse=True)
    if location_candidates and (
        len(location_candidates) == 1
        or location_candidates[0][0] - location_candidates[1][0] >= 0.04
        or normalize_filter_value(location_candidates[0][4])
        == normalize_filter_value(location_candidates[1][4])
    ):
        _, _, key, source, actual = location_candidates[0]
        corrected = re.sub(
            rf"(?<!\w){re.escape(source)}(?!\w)",
            normalize_filter_value(actual),
            corrected,
            count=1,
        )
        corrections.append({"field": key, "input": source, "value": actual})

    return corrected, corrections


def infer_keyword_subcategory(keyword_query: str, values: dict) -> str | None:
    query_tokens = {
        token
        for token in re.findall(r"[^\W_]+", normalize_filter_value(keyword_query))
        if len(token) >= 3 and token not in GENERIC_CATEGORY_HINT_TOKENS
    }
    if not query_tokens:
        return None

    token_categories: dict[str, set[str]] = {}
    for actual_value in values.values():
        category_tokens = set(
            re.findall(r"[^\W_]+", normalize_filter_value(actual_value))
        )
        # A single shared token must not promote a more specific multiword
        # profession or service. For example, "fridge home appliance" does
        # not imply the catalog category "Fridge Mechanic" unless mechanic is
        # also present. Exact one-word concepts and fully supported phrases
        # remain eligible as soft hints.
        if len(category_tokens) > 1 and not category_tokens.issubset(query_tokens):
            continue
        for token in query_tokens.intersection(category_tokens):
            token_categories.setdefault(token, set()).add(actual_value)

    scores: dict[str, int] = {}
    for categories in token_categories.values():
        if len(categories) != 1:
            continue
        category = next(iter(categories))
        scores[category] = scores.get(category, 0) + 1
    if not scores:
        return None

    ranked = sorted(
        scores.items(),
        key=lambda item: (item[1], len(item[0])),
        reverse=True,
    )
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def find_catalog_alias(query: str, query_key: str, values: dict) -> str | None:
    normalized_query = normalize_filter_value(query)
    for alias, canonical in QUERY_FILTER_ALIASES.get(query_key, {}).items():
        pattern = rf"(?<!\w){re.escape(alias)}(?!\w)"
        if re.search(pattern, normalized_query):
            return values.get(canonical)
    return None


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


def query_filter_value_index(bm25_index: PersistentBM25Index) -> dict:
    stored_values = bm25_index.filter_value_index()
    value_index = {
        query_key: CatalogValueMap(
            stored_values[metadata_key],
            allow_plural=query_key in {"main_category", "subcategory"},
        )
        for query_key, metadata_key in QUERY_FILTER_FIELDS.items()
    }
    value_index["_subcategory_main_category"] = bm25_index.subcategory_parent_index()
    value_index["_city_state"] = bm25_index.city_state_index()
    value_index["_locality_location"] = bm25_index.locality_location_index()
    return value_index


def build_query_filter_catalog(value_index: dict, max_values: int = 100) -> dict:
    catalog = {}
    for key in ("main_category", "state", "rental_duration"):
        values = sorted(
            value_index[key].values(),
            key=lambda value: str(value).casefold(),
        )
        if values and len(values) <= max_values:
            catalog[key] = values
    return QueryFilterCatalog(catalog)


def resolve_query_filters(filters: dict, value_index: dict) -> tuple[dict, dict]:
    resolved = {"categorical": {}}
    unresolved = {}

    for query_key, metadata_key in QUERY_FILTER_FIELDS.items():
        requested = filters.get(query_key)
        if requested is None:
            continue
        actual = canonical_catalog_value(
            query_key,
            requested,
            value_index[query_key],
        )
        if actual is None:
            unresolved[query_key] = requested
            continue
        resolved["categorical"][metadata_key] = actual

    for key in ("min_rental_fee", "max_rental_fee"):
        value = filters.get(key)
        if value is not None:
            resolved[key] = value

    return resolved, unresolved
