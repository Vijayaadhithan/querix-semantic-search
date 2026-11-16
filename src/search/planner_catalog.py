import json
import re
from collections.abc import Callable
from copy import copy

from search.bm25 import PersistentBM25Index
from search.catalog_matching import (
    canonical_catalog_value,
    category_catalog_matches,
    correct_explicit_query_typos,
    edit_distance,
    find_catalog_alias,
    find_catalog_value,
    find_fuzzy_location,
    fuzzy_catalog_match,
    infer_keyword_subcategory,
    location_phrases,
    ordered_subsequence,
    typo_catalog_match,
)
from search.planner_rules import (
    QUERY_FILTER_FIELDS,
    QUERY_FILTER_KEYS,
)
from search.query_constraints import (
    extract_duration_filter,
    extract_price_constraints,
    extract_sort_order,
    extract_standalone_budget,
)
from search.query_terms import (
    category_term_pattern,
    is_category_attribute_usage,
    is_explicit_category_request,
    is_generic_location_value,
    is_repair_subject_usage,
    normalize_filter_value,
    normalize_transliterated_query,
    optional_number,
    optional_text,
    optional_user_gender,
    text_mentions_filter,
)

__all__ = (
    "CatalogValueMap",
    "QueryAnalysis",
    "QueryFilterCatalog",
    "build_query_filter_catalog",
    "canonical_catalog_value",
    "category_catalog_matches",
    "category_term_pattern",
    "correct_explicit_query_typos",
    "default_query_plan",
    "edit_distance",
    "extract_duration_filter",
    "extract_price_constraints",
    "extract_sort_order",
    "extract_standalone_budget",
    "find_catalog_alias",
    "find_catalog_value",
    "find_fuzzy_location",
    "fuzzy_catalog_match",
    "infer_keyword_subcategory",
    "is_category_attribute_usage",
    "is_explicit_category_request",
    "is_generic_location_value",
    "is_repair_subject_usage",
    "location_phrases",
    "normalize_filter_value",
    "normalize_transliterated_query",
    "optional_number",
    "optional_text",
    "optional_user_gender",
    "ordered_subsequence",
    "parse_query_plan",
    "query_analysis",
    "query_filter_value_index",
    "resolve_query_filters",
    "text_mentions_filter",
    "typo_catalog_match",
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
    filters["user_gender"] = optional_user_gender(raw_filters.get("user_gender"))
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


class QueryAnalysis:
    """Original-query facts that are safe to reuse across planner passes."""

    def __init__(
        self,
        query: str,
        value_index: dict,
        query_aliases: dict[str, str] | None = None,
        normalized_query: str | None = None,
        query_normalizer: Callable[[str, dict[str, str] | None], str] | None = None,
    ):
        self.original_query = query
        normalizer = query_normalizer or normalize_transliterated_query
        self.query = normalized_query or normalizer(
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
    query_normalizer: Callable[[str, dict[str, str] | None], str] | None = None,
) -> QueryAnalysis:
    normalizer = query_normalizer or normalize_transliterated_query
    normalized_query = normalizer(query, query_aliases)
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
        normalizer,
    )
    if cache is not None:
        cache[cache_key] = analysis
    return analysis


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

    user_gender = optional_user_gender(filters.get("user_gender"))
    if user_gender is not None:
        resolved["categorical"]["user_gender"] = user_gender

    return resolved, unresolved
