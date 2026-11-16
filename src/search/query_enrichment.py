"""Apply tenant policy and catalog evidence to extracted query plans."""

import re

from search.planner_catalog import (
    QueryAnalysis,
    canonical_catalog_value,
    category_term_pattern,
    extract_sort_order,
    infer_keyword_subcategory,
    is_explicit_category_request,
    is_generic_location_value,
    is_repair_subject_usage,
    normalize_filter_value,
    query_analysis,
    text_mentions_filter,
)
from search.planner_rules import (
    CATEGORY_ATTRIBUTE_PREFIXES,
    QUERY_FILTER_ALIASES,
    QUERY_FILTER_FIELDS,
)
from search.policy import DEFAULT_SEARCH_POLICY, SearchPolicy
from search.query_intent import (
    _target_ad_type_inference,
)


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
