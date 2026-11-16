"""Match literal, fuzzy, and misspelled query terms against tenant catalogs."""

import re
from difflib import SequenceMatcher, get_close_matches

from core.settings import QUERY_FUZZY_MATCHING
from search.planner_rules import (
    CATEGORY_LEADING_MODIFIER_TOKENS,
    FUZZY_MATCH_THRESHOLDS,
    GENERIC_CATEGORY_HINT_TOKENS,
    LOCATION_PREPOSITIONS,
    LOCATION_STOP_WORDS,
    QUERY_FILTER_ALIASES,
)
from search.query_terms import (
    is_explicit_category_request,
    is_generic_location_value,
    is_repair_subject_usage,
    normalize_filter_value,
)


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
        if token == "in" and index > 0 and tokens[index - 1] == "interested":
            # "Interested in <item>" is a marketplace-demand complement, not
            # a location clause. A later locative preposition is still parsed,
            # e.g. "customers interested in cars in Chennai".
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
