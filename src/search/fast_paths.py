"""Deterministic and direct-semantic query planning without hosted extraction."""

import re

from search.planner_catalog import (
    QueryAnalysis,
    category_term_pattern,
    correct_explicit_query_typos,
    default_query_plan,
    extract_sort_order,
    extract_standalone_budget,
    normalize_filter_value,
    query_analysis,
)
from search.planner_rules import (
    DIRECT_SEMANTIC_BLOCK_TOKENS,
    DIRECT_SEMANTIC_COMPLEX_PATTERNS,
    DIRECT_SEMANTIC_MAX_TOKENS,
    FAST_PATH_DURATION_TOKENS,
    FAST_PATH_FILLER_TOKENS,
    FAST_PATH_PRICE_TOKENS,
    FAST_PATH_SORT_TOKENS,
    QUERY_FILTER_ALIASES,
    QUERY_FILTER_FIELDS,
)
from search.policy import DEFAULT_SEARCH_POLICY, SearchPolicy
from search.query_enrichment import (
    enrich_query_plan,
)
from search.query_intent import (
    _DIRECT_MARKETPLACE_WRAPPER_TOKENS,
    _has_natural_marketplace_intent,
    _target_ad_type_inference,
)


def deterministic_filter_query_plan(
    query: str,
    value_index: dict,
    query_aliases: dict[str, str] | None = None,
    analysis_cache: dict[tuple[str, str], QueryAnalysis] | None = None,
    search_policy: SearchPolicy = DEFAULT_SEARCH_POLICY,
) -> dict | None:
    """Return a direct-filter plan for simple explicit catalog queries."""
    sort_order = extract_sort_order(query)
    corrected_query, corrections = correct_explicit_query_typos(
        query,
        value_index,
    )
    if _has_natural_marketplace_intent(corrected_query, search_policy):
        # Deterministic browsing is reserved for literal catalog/filter
        # requests. Natural demand language needs semantic ranking, even when
        # it happens to mention an exact category such as Bike or Car.
        return None
    # In a rental catalog, "car retail" alongside explicit price ordering is
    # overwhelmingly a typo for "car rental". Keep the correction narrow so a
    # genuine retail query without ordering still goes through semantic search.
    if sort_order and re.search(r"\bretail\b", corrected_query.casefold()):
        corrected_query = re.sub(
            r"\bretail\b",
            "rental",
            corrected_query,
            flags=re.IGNORECASE,
        )
        corrections.append({"field": "intent", "input": "retail", "value": "rental"})
    plan = enrich_query_plan(
        corrected_query,
        default_query_plan(corrected_query),
        value_index,
        query_aliases,
        query_analysis(
            corrected_query,
            value_index,
            query_aliases,
            analysis_cache,
            search_policy.normalize_query,
        ),
        search_policy,
    )
    filters = plan["filters"]
    if not any(filters.get(key) for key in ("main_category", "subcategory")):
        return None
    if filters.get("min_rental_fee") is None and filters.get("max_rental_fee") is None:
        filters["max_rental_fee"] = extract_standalone_budget(corrected_query)

    residual = normalize_filter_value(corrected_query)
    for key in QUERY_FILTER_FIELDS:
        value = filters.get(key)
        if not value:
            continue
        residual = re.sub(
            rf"(?<!\w){category_term_pattern(value)}(?!\w)",
            " ",
            residual,
        )
        normalized_value = normalize_filter_value(value)
        for alias, canonical in QUERY_FILTER_ALIASES.get(key, {}).items():
            if canonical == normalized_value:
                residual = re.sub(
                    rf"(?<!\w){re.escape(alias)}(?!\w)",
                    " ",
                    residual,
                )

    has_price = any(
        filters.get(key) is not None for key in ("min_rental_fee", "max_rental_fee")
    )
    has_duration = filters.get("rental_duration") is not None
    allowed_tokens = set(FAST_PATH_FILLER_TOKENS)
    if has_price:
        allowed_tokens.update(FAST_PATH_PRICE_TOKENS)
    if has_duration:
        allowed_tokens.update(FAST_PATH_DURATION_TOKENS)
    if plan.get("sort_order"):
        allowed_tokens.update(FAST_PATH_SORT_TOKENS)
    unexplained_tokens = []
    for token in re.findall(r"[^\W_]+", residual):
        if token in allowed_tokens:
            continue
        if has_price and token.replace(".", "", 1).isdigit():
            continue
        unexplained_tokens.append(token)
    if unexplained_tokens:
        return None

    plan["semantic_query"] = query
    plan["keyword_query"] = query
    plan["query_corrections"] = corrections
    plan["execution_path"] = "deterministic_filter"
    plan["route_reason"] = "complete_structured_catalog_match"
    return plan


def direct_semantic_query_plan(
    query: str,
    value_index: dict,
    query_aliases: dict[str, str] | None = None,
    analysis_cache: dict[tuple[str, str], QueryAnalysis] | None = None,
    search_policy: SearchPolicy = DEFAULT_SEARCH_POLICY,
) -> tuple[dict | None, str]:
    """Route only high-confidence objective catalog phrases around the LLM."""
    analysis = query_analysis(
        query,
        value_index,
        query_aliases,
        analysis_cache,
        search_policy.normalize_query,
    )
    routing_query = analysis.query if analysis.query_was_normalized else query
    normalized = normalize_filter_value(routing_query)
    tokens = re.findall(r"[^\W_]+", normalized)
    token_set = set(tokens)
    target_ad_type, target_ad_type_is_decisive = _target_ad_type_inference(
        routing_query,
        search_policy,
    )
    if not tokens:
        return None, "empty_query"
    if len(tokens) > DIRECT_SEMANTIC_MAX_TOKENS:
        return None, "too_many_tokens"
    if not query.isascii():
        return None, "non_ascii_language"
    if not target_ad_type_is_decisive:
        return None, "ambiguous_ad_type_intent"
    planner_requirement = getattr(
        search_policy,
        "requires_hosted_planner",
        None,
    )
    if planner_requirement and planner_requirement(routing_query):
        return None, "tenant_ambiguous_compound"
    if any(token.isdigit() for token in tokens):
        return None, "numeric_constraint_or_model"
    marketplace_hook = getattr(
        search_policy,
        "allows_decisive_marketplace_direct_semantic",
        None,
    )
    tenant_allows_marketplace_direct = bool(
        marketplace_hook and marketplace_hook(analysis.query)
    )
    if (
        target_ad_type != "wanted"
        and not tenant_allows_marketplace_direct
        and token_set
        & {
            "people",
            "person",
            "someone",
            "wanted",
            "request",
            "requests",
            "renters",
            "customers",
        }
    ):
        return None, "ad_type_intent"
    if (
        any(
            analysis.exact_values.get(key) is not None
            for key in ("state", "city", "locality")
        )
        or analysis.fuzzy_location(value_index) is not None
    ):
        return None, "location_language"
    if analysis.rental_duration is not None or any(
        value is not None for value in analysis.price_constraints
    ):
        return None, "price_or_duration_language"
    if extract_sort_order(routing_query) is not None:
        return None, "sort_language"
    category_intent = search_policy.category_intent(
        analysis.query,
        value_index["subcategory"],
    )
    descriptive_hook = getattr(
        search_policy,
        "allows_descriptive_direct_semantic",
        None,
    )
    descriptive_direct = bool(descriptive_hook and descriptive_hook(analysis.query))
    catalog_grounded = bool(
        any(
            analysis.exact_values.get(key) is not None
            for key in ("main_category", "subcategory")
        )
        or category_intent is not None
    )
    if analysis.query_was_normalized and category_intent is None:
        # A reviewed alias may skip the hosted planner only when its normalized
        # meaning resolves through a tenant-owned intent rule, which can set a
        # safe parent boundary. Spelling-only and free-form multilingual
        # language still need the planner for intent and filter extraction.
        return None, "query_requires_normalization"
    if not descriptive_direct and not catalog_grounded:
        return None, "no_explicit_catalog_category"
    blocked_tokens = token_set & DIRECT_SEMANTIC_BLOCK_TOKENS
    complex_shape = any(
        pattern.search(routing_query) for pattern in DIRECT_SEMANTIC_COMPLEX_PATTERNS
    )
    marketplace_direct = bool(
        catalog_grounded
        and tenant_allows_marketplace_direct
        and _has_natural_marketplace_intent(routing_query, search_policy)
        and blocked_tokens.issubset(_DIRECT_MARKETPLACE_WRAPPER_TOKENS)
        and not complex_shape
    )
    if target_ad_type == "wanted" and not (descriptive_direct or marketplace_direct):
        # Tenants must explicitly opt into the semantic fast path before a
        # buyer-demand query can bypass hosted planning.
        return None, "ad_type_intent"
    if not descriptive_direct and blocked_tokens and not marketplace_direct:
        return None, "complex_or_subjective_language"
    if not descriptive_direct and complex_shape:
        return None, "complex_query_shape"

    plan = enrich_query_plan(
        query,
        default_query_plan(query),
        value_index,
        query_aliases,
        analysis,
        search_policy,
    )
    non_category_filters = {
        key: value
        for key, value in plan["filters"].items()
        if key not in {"main_category", "subcategory"} and value is not None
    }
    if non_category_filters:
        return None, "structured_intent_detected"
    plan["execution_path"] = "direct_semantic"
    if plan["target_ad_type"] == "wanted":
        plan["route_reason"] = "buyer_demand_semantic"
    elif analysis.query_was_normalized:
        plan["route_reason"] = "reviewed_normalization_offer"
    else:
        plan["route_reason"] = (
            "descriptive_marketplace_offer"
            if descriptive_direct
            and (not catalog_grounded or blocked_tokens or complex_shape)
            else "marketplace_intent_semantic"
            if marketplace_direct
            else "objective_catalog_phrase"
        )
    return plan, plan["route_reason"]
