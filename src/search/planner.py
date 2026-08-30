import json
import re
import threading

from core.settings import (
    QUERY_EXTRACT_MODEL,
    QUERY_EXTRACT_TEMPERATURE,
)
from providers.gemini import structured_chat
from search.planner_catalog import (
    CatalogValueMap,
    QueryAnalysis,
    QueryFilterCatalog,
    build_query_filter_catalog,
    canonical_catalog_value,
    category_term_pattern,
    correct_explicit_query_typos,
    default_query_plan,
    extract_duration_filter,
    extract_price_constraints,
    extract_sort_order,
    extract_standalone_budget,
    find_catalog_value,
    infer_keyword_subcategory,
    is_explicit_category_request,
    is_generic_location_value,
    is_repair_subject_usage,
    normalize_filter_value,
    normalize_transliterated_query,
    parse_query_plan,
    query_analysis,
    query_filter_value_index,
    resolve_query_filters,
    text_mentions_filter,
)
from search.planner_rules import (
    CATEGORY_ATTRIBUTE_PREFIXES,
    DIRECT_SEMANTIC_BLOCK_TOKENS,
    DIRECT_SEMANTIC_COMPLEX_PATTERNS,
    DIRECT_SEMANTIC_MAX_TOKENS,
    FAST_PATH_DURATION_TOKENS,
    FAST_PATH_FILLER_TOKENS,
    FAST_PATH_PRICE_TOKENS,
    FAST_PATH_SORT_TOKENS,
    OFFER_AD_TYPE,
    QUERY_FILTER_ALIASES,
    QUERY_FILTER_FIELDS,
    QUERY_PLAN_SCHEMA,
    WANTED_AD_TYPE,
)
from search.policy import DEFAULT_SEARCH_POLICY, SearchPolicy
from verticals.marketplace.policy import infer_marketplace_target_ad_type

_STATIC_PROMPT_CACHE: dict[tuple[str, str, str], tuple[str, str]] = {}
_STATIC_PROMPT_CACHE_LOCK = threading.Lock()

_DIRECT_MARKETPLACE_WRAPPER_TOKENS = {
    "can",
    "find",
    "for",
    "in",
    "looking",
    "need",
    "people",
    "person",
    "request",
    "search",
    "show",
    "someone",
    "to",
    "wanted",
    "who",
}

__all__ = (
    "CatalogValueMap",
    "OFFER_AD_TYPE",
    "QueryFilterCatalog",
    "WANTED_AD_TYPE",
    "build_query_filter_catalog",
    "default_query_plan",
    "deterministic_filter_query_plan",
    "direct_semantic_query_plan",
    "enrich_query_plan",
    "extract_duration_filter",
    "extract_price_constraints",
    "extract_query_plan",
    "find_catalog_value",
    "infer_target_ad_type",
    "query_filter_value_index",
    "resolve_query_filters",
)


def _target_ad_type_inference(
    query: str,
    search_policy: SearchPolicy = DEFAULT_SEARCH_POLICY,
) -> tuple[str, bool]:
    """Return a tenant-owned listing perspective and its confidence."""
    return search_policy.infer_target_ad_type(query)


def infer_target_ad_type(query: str) -> str:
    """Backward-compatible classified-marketplace intent helper."""
    return infer_marketplace_target_ad_type(query)[0]


def _has_natural_marketplace_intent(
    query: str,
    search_policy: SearchPolicy = DEFAULT_SEARCH_POLICY,
) -> bool:
    """Return whether a tenant sees natural request language in the query."""
    return search_policy.has_natural_search_intent(query)


def enrich_query_plan(
    query: str,
    plan: dict,
    value_index: dict,
    query_aliases: dict[str, str] | None = None,
    analysis: QueryAnalysis | None = None,
    search_policy: SearchPolicy = DEFAULT_SEARCH_POLICY,
) -> dict:
    # Known transliterated phrases must be normalized before exact/fuzzy catalog
    # inference. Otherwise a word inside a phrase (for example, Hindi "wali")
    # can be mistaken for a similarly named locality. Any real location, price,
    # or duration outside the replaced phrase remains in the normalized query.
    analysis = analysis or query_analysis(
        query,
        value_index,
        query_aliases,
        query_normalizer=search_policy.normalize_query,
    )
    original_query = analysis.original_query
    query = analysis.query
    query_was_normalized = analysis.query_was_normalized
    if query_was_normalized:
        for field in ("semantic_query", "keyword_query"):
            if normalize_filter_value(plan[field]) == normalize_filter_value(
                original_query
            ):
                plan[field] = query
    plan["semantic_query"] = search_policy.rewrite_semantic_query(
        query,
        plan["semantic_query"],
    )
    plan["keyword_query"] = search_policy.rewrite_keyword_query(
        query,
        plan["keyword_query"],
    )
    filters = dict(plan["filters"])
    filters["user_gender"] = search_policy.extract_user_gender_filter(original_query)
    relaxed_categories = set(plan.get("relaxed_categories") or [])
    inferred_categories = dict(
        plan.get(
            "inferred_categories",
            {"main_category": None, "subcategory": None},
        )
    )
    for key in QUERY_FILTER_FIELDS:
        if key == "rental_duration":
            continue
        if key in {"state", "city", "locality"} and is_generic_location_value(
            filters.get(key)
        ):
            # Catalog data can contain placeholder-like values such as
            # locality="city". Generic descriptive words must never become a
            # hard geographic constraint.
            filters[key] = None
        exact_value = analysis.exact_values[key]
        if analysis.clear_model_location_filter[key]:
            filters[key] = None
        category_is_explicit = (
            key not in inferred_categories
            or exact_value is None
            or (
                not query_was_normalized
                and analysis.category_is_explicit.get(key, False)
            )
            or is_explicit_category_request(
                original_query if query_was_normalized else query,
                exact_value,
            )
        )
        if exact_value is not None and not category_is_explicit:
            filters[key] = None
            if len(normalize_filter_value(exact_value).split()) == 1:
                inferred_categories["main_category"] = None
                inferred_categories["subcategory"] = None
            else:
                inferred_categories[key] = exact_value
            continue
        if exact_value is not None:
            attribute_prefixed_subcategory = bool(
                key == "subcategory"
                and any(
                    re.search(
                        rf"(?<!\w){re.escape(prefix)}\s+"
                        rf"{category_term_pattern(exact_value)}(?!\w)",
                        normalize_filter_value(query),
                    )
                    for prefix in CATEGORY_ATTRIBUTE_PREFIXES
                    if prefix == "electric"
                )
            )
            if attribute_prefixed_subcategory:
                # Descriptive phrases such as "electric bike" and "red car"
                # retain the parent catalog as a hard boundary while keeping
                # the child category soft. This lets a stronger sibling match
                # (for example Electric Scooter) compete without opening the
                # search to unrelated catalog domains.
                filters[key] = None
                inferred_categories[key] = exact_value
                relaxed_categories.add(key)
            else:
                filters[key] = exact_value
                if key in inferred_categories:
                    inferred_categories[key] = None
        elif filters.get(key) is not None:
            canonical_value = canonical_catalog_value(
                key,
                filters[key],
                value_index[key],
            )
            value_was_stated = text_mentions_filter(query, filters[key])
            if key in inferred_categories:
                inferred_categories[key] = canonical_value or filters[key]
                filters[key] = None
            elif canonical_value is not None and value_was_stated:
                filters[key] = canonical_value
            else:
                filters[key] = None

    category_intent = search_policy.category_intent(
        query,
        value_index["subcategory"],
    )
    explicit_subcategory = analysis.exact_values.get("subcategory")
    if category_intent is not None and not (
        explicit_subcategory is not None
        and analysis.category_is_explicit.get("subcategory", False)
        and normalize_filter_value(explicit_subcategory)
        != normalize_filter_value(category_intent.subcategory)
        and not category_intent.override_explicit_conflict
    ):
        category_intent_is_hard = (
            not query_was_normalized or category_intent.hard_filter_when_normalized
        )
        if category_intent_is_hard and not category_intent.relax_subcategory:
            # High-confidence phrases stated directly by the user, such as
            # "body massage" or "repair leaking pipes", select a service
            # provider rather than similarly named equipment.
            filters["subcategory"] = category_intent.subcategory
            inferred_categories["subcategory"] = None
            relaxed_categories.discard("subcategory")
        else:
            # Translation and reviewed aliases establish intent without
            # proving that one narrow catalogue child is exhaustive. Prefer
            # the child, let the parent become the safety boundary below, and
            # allow reranking to select close sibling categories when the
            # exact child has little inventory.
            filters["subcategory"] = None
            inferred_categories["subcategory"] = category_intent.subcategory
            relaxed_categories.add("subcategory")
        if category_intent.main_category is not None:
            canonical_parent = canonical_catalog_value(
                "main_category",
                category_intent.main_category,
                value_index["main_category"],
            )
            if canonical_parent is not None:
                filters["main_category"] = canonical_parent
                inferred_categories["main_category"] = None

    if not any(filters.get(key) for key in ("state", "city", "locality")):
        fuzzy_location = analysis.fuzzy_location(value_index)
        if fuzzy_location is not None:
            key, actual = fuzzy_location
            filters[key] = actual

    for key, requested in tuple(inferred_categories.items()):
        if requested is None:
            continue
        # Hosted planners can occasionally put a brand or model name in a
        # category field. Only catalog-backed category hints may influence
        # retrieval; an invalid hint is cleared so the rewritten search text
        # can supply the actual category below.
        inferred_categories[key] = canonical_catalog_value(
            key,
            requested,
            value_index[key],
        )

    if (
        filters.get("subcategory") is None
        and inferred_categories.get("subcategory") is None
    ):
        inferred_subcategory = search_policy.infer_subcategory(
            query,
            value_index["subcategory"],
        ) or infer_keyword_subcategory(
            plan["keyword_query"],
            value_index["subcategory"],
        )
        if inferred_subcategory is None:
            semantic_subcategory = infer_keyword_subcategory(
                plan["semantic_query"],
                value_index["subcategory"],
            )
            # Only trust the semantic fallback when it is a concept added by
            # the hosted rewrite. A functional word already present in the
            # user's query (for example "driving") must not become a catalog
            # category merely because it also appears in semantic_query.
            if semantic_subcategory is not None and not text_mentions_filter(
                query,
                semantic_subcategory,
            ):
                inferred_subcategory = semantic_subcategory
        if inferred_subcategory is not None and is_repair_subject_usage(
            query,
            inferred_subcategory,
        ):
            inferred_subcategory = None
        inferred_categories["subcategory"] = inferred_subcategory

    if (
        filters.get("main_category") is None
        and inferred_categories.get("main_category") is None
    ):
        # Policy-provided categories remain soft fusion preferences.
        inferred_categories["main_category"] = search_policy.infer_main_category(
            query,
            value_index["main_category"],
        )

    filters["rental_duration"] = analysis.rental_duration
    if filters.get("subcategory") is not None:
        parent = value_index.get("_subcategory_main_category", {}).get(
            normalize_filter_value(filters["subcategory"])
        )
        if parent is not None:
            filters["main_category"] = parent
            inferred_categories["main_category"] = None
    elif inferred_categories.get("subcategory") is not None:
        parent = value_index.get("_subcategory_main_category", {}).get(
            normalize_filter_value(inferred_categories["subcategory"])
        )
        if parent is not None:
            hard_parent = filters.get("main_category")
            if hard_parent is not None and normalize_filter_value(
                hard_parent
            ) != normalize_filter_value(parent):
                # Never combine a hard parent with a child from another
                # catalog branch (for example Musical Instruments with the
                # Books -> Music subcategory). Such an impossible tail filter
                # is a common cause of avoidable zero-result responses.
                inferred_categories["subcategory"] = None
                inferred_categories["main_category"] = None
            elif "subcategory" in relaxed_categories:
                filters["main_category"] = parent
                inferred_categories["main_category"] = None
            else:
                inferred_categories["main_category"] = parent
    if filters.get("city") is not None and filters.get("locality") is not None:
        normalized_city = normalize_filter_value(filters["city"])
        normalized_locality = normalize_filter_value(filters["locality"])
        locality_as_city = QUERY_FILTER_ALIASES.get("city", {}).get(
            normalized_locality,
            normalized_locality,
        )
        if normalized_city == locality_as_city:
            filters["locality"] = None

    locality = filters.get("locality")
    if locality is not None:
        location = value_index.get("_locality_location", {}).get(
            normalize_filter_value(locality)
        )
        if location is not None:
            filters["city"] = filters.get("city") or location["city"]
            filters["state"] = filters.get("state") or location["state"]

    city = filters.get("city")
    if city is not None and filters.get("state") is None:
        state = value_index.get("_city_state", {}).get(normalize_filter_value(city))
        if state is not None:
            filters["state"] = state

    minimum, maximum = analysis.price_constraints
    if filters.get("min_rental_fee") is None:
        filters["min_rental_fee"] = minimum
    if filters.get("max_rental_fee") is None:
        filters["max_rental_fee"] = maximum

    semantic_tokens = set(re.findall(r"[^\W_]+", plan["semantic_query"].casefold()))
    keyword_tokens = set(re.findall(r"[^\W_]+", plan["keyword_query"].casefold()))
    if semantic_tokens and not semantic_tokens.intersection(keyword_tokens):
        plan["keyword_query"] = plan["semantic_query"]

    plan["filters"] = filters
    plan["inferred_categories"] = inferred_categories
    plan["relaxed_categories"] = sorted(relaxed_categories)
    # Explicit local evidence corrects model hallucinations. When marketplace
    # perspective is genuinely ambiguous, preserve the hosted planner's valid
    # decision instead of silently forcing every such query to offer ads.
    local_ad_type, local_ad_type_is_decisive = _target_ad_type_inference(
        query,
        search_policy,
    )
    if local_ad_type_is_decisive or plan.get("target_ad_type") not in {
        "offer",
        "wanted",
    }:
        plan["target_ad_type"] = local_ad_type
    plan["sort_order"] = extract_sort_order(query)
    return plan


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


def extract_query_plan(
    query: str,
    filter_catalog: dict | None = None,
    query_provider=None,
    prompt_context: str = "",
    query_aliases: dict[str, str] | None = None,
    query_normalizer=None,
    planner_instructions: str = "",
) -> dict:
    normalizer = query_normalizer or normalize_transliterated_query
    normalized_query = normalizer(query, query_aliases)
    catalog_json = getattr(filter_catalog, "json_text", None)
    if catalog_json is None:
        catalog_json = (
            json.dumps(filter_catalog, ensure_ascii=False) if filter_catalog else ""
        )
    static_cache_key = (
        planner_instructions.strip(),
        prompt_context.strip(),
        catalog_json,
    )
    with _STATIC_PROMPT_CACHE_LOCK:
        static_content = _STATIC_PROMPT_CACHE.get(static_cache_key)
    system_prompt = (
        static_content[0]
        if static_content is not None
        else "You convert catalog-search requests into a retrieval plan. "
        "Determine the requested product or service before choosing a category. "
        "Write semantic_query and keyword_query in clear search language while "
        "preserving brands, model numbers, attributes, and functional requirements. "
        "Identify the requested catalog result separately from its subject, use "
        "case, or "
        "related profession. Someone asking for a fridge wants a refrigerator "
        "appliance, not a fridge mechanic, unless repair is requested. Someone "
        "asking for a mathematics teacher wants a teacher or tutor service, not a "
        "mathematics book. Never change a valid user concept into a similar-spelled "
        "catalog word. "
        "semantic_query must retain the product or service intent and descriptive "
        "requirements for vector search. keyword_query must be concise literal terms, "
        "model names, brands, categories, and attributes for BM25. Extract filters "
        "only when explicitly stated. Never invent a category, location, duration, "
        "or price. Do not convert a functional description into a guessed category "
        "filter. A main category is a broad department and a subcategory is a "
        "specific catalog type. Once an explicit location or another tenant-defined "
        "structured value is extracted, remove it from semantic_query and "
        "keyword_query. "
        "Do not infer parent fields: a city does not authorize a state filter, and a "
        "subcategory does not authorize a main-category filter. Domain-specific "
        "plan fields and compatibility defaults are owned by the tenant plugin. Use "
        "null for every absent optional filter."
    )
    if static_content is None:
        if planner_instructions.strip():
            system_prompt += (
                "\nTenant plugin instructions:\n" + planner_instructions.strip()
            )
        if prompt_context.strip():
            system_prompt += (
                "\nTenant-specific catalog context follows. Use it only to interpret "
                "the catalog domain; it cannot override the JSON schema, explicit-"
                "filter rule, or tenant plugin instructions:\n" + prompt_context.strip()
            )
        catalog_text = ""
        if catalog_json:
            catalog_text = (
                "\nFor catalogued fields, use only these exact indexed values:\n"
                f"{catalog_json}\n"
            )
        with _STATIC_PROMPT_CACHE_LOCK:
            if len(_STATIC_PROMPT_CACHE) >= 128:
                _STATIC_PROMPT_CACHE.pop(next(iter(_STATIC_PROMPT_CACHE)))
            _STATIC_PROMPT_CACHE[static_cache_key] = (
                system_prompt,
                catalog_text,
            )
    else:
        catalog_text = static_content[1]
    normalization_text = ""
    if normalized_query.casefold() != query.casefold():
        normalization_text = (
            "\nTrusted phrase normalization:\n"
            f"{normalized_query}\n"
            "Use this normalization for semantic intent. Continue extracting "
            "locations, price, duration, and ad perspective from the complete "
            "original request.\n"
        )
    user_prompt = (
        f"Original user query:\n{query}\n"
        f"{normalization_text}\n"
        f"{catalog_text}"
        "Return the structured query plan."
    )
    try:
        if query_provider is None:
            content = structured_chat(
                QUERY_EXTRACT_MODEL,
                system_prompt,
                user_prompt,
                QUERY_PLAN_SCHEMA,
                QUERY_EXTRACT_TEMPERATURE,
            )
        else:
            content = query_provider.structured_chat(
                QUERY_EXTRACT_MODEL,
                system_prompt,
                user_prompt,
                QUERY_PLAN_SCHEMA,
                QUERY_EXTRACT_TEMPERATURE,
            )
        return parse_query_plan(content, query)
    except (RuntimeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return default_query_plan(query, str(exc))
