"""Expand ranked results with anchored hybrid and catalogue candidates."""

import time

from search.retrieval import (
    extract_product_ids,
)


def expand_ranked_results(
    ranked: dict,
    retrieved: dict,
    planned: dict,
    candidates: list,
    limit: int | None,
    allowed_ad_types: set[str] | None,
    *,
    search_table: str,
    search_id_column: str,
    bm25_index,
    type_fetcher,
    tail_lookup,
    tail_allowed: bool,
) -> tuple[list, list, list, list, float]:
    primary_product_ids = extract_product_ids(
        ranked["results"],
        search_table=search_table,
        search_id_column=search_id_column,
    )
    tail_limit = max(limit - len(primary_product_ids), 0) if limit is not None else 0
    tail_started = time.perf_counter()
    hybrid_product_ids = []
    related_product_ids = []
    query_plan = planned["query_plan"]
    inferred_categories = dict(query_plan.get("inferred_categories") or {})
    relaxed_categories = set(query_plan.get("relaxed_categories") or [])
    category_metadata = {
        "main_category": "main_category_name",
        "subcategory": "subcategory_name",
    }
    resolved_categorical = planned["resolved_filters"].get(
        "categorical",
        {},
    )
    top_ranked_metadata = (
        (ranked["results"][0].get("metadata") or {}) if ranked["results"] else {}
    )
    for category_key, metadata_key in category_metadata.items():
        if metadata_key in resolved_categorical:
            continue
        top_value = top_ranked_metadata.get(metadata_key)
        if top_value and (
            not inferred_categories.get(category_key)
            or category_key in relaxed_categories
        ):
            # The reranker's strongest surviving match is a safe dynamic
            # category anchor when planning had no useful child category.
            # It lets the result tail stay in the same marketplace intent
            # instead of degrading into unrelated city-wide inventory.
            inferred_categories[category_key] = top_value
    has_category_anchor = bool(
        any(
            metadata_key in resolved_categorical
            for metadata_key in category_metadata.values()
        )
        or any(inferred_categories.get(key) for key in category_metadata)
    )
    unanchored_hybrid_product_ids = []
    if tail_allowed and tail_limit:
        primary_identities = {str(product_id) for product_id in primary_product_ids}
        hybrid_candidates = retrieved.get("hybrid_tail_candidates", [])

        def candidate_matches_anchor(candidate):
            metadata = candidate.get("metadata") or {}
            for category_key, metadata_key in category_metadata.items():
                expected = resolved_categorical.get(metadata_key)
                if expected is None:
                    expected = inferred_categories.get(category_key)
                if expected is None:
                    continue
                if (
                    str(metadata.get(metadata_key) or "").casefold()
                    != str(expected).casefold()
                ):
                    return False
            return True

        anchored_candidates = []
        unanchored_candidates = []
        for candidate in hybrid_candidates:
            product_ids = extract_product_ids(
                [candidate],
                search_table=search_table,
                search_id_column=search_id_column,
            )
            if not product_ids:
                continue
            product_id = product_ids[0]
            if str(product_id) in primary_identities:
                continue
            target = (
                anchored_candidates
                if has_category_anchor and candidate_matches_anchor(candidate)
                else unanchored_candidates
            )
            target.append(product_id)
        if has_category_anchor:
            hybrid_product_ids = anchored_candidates[:tail_limit]
            unanchored_hybrid_product_ids = unanchored_candidates
        else:
            hybrid_product_ids = unanchored_candidates[:tail_limit]
    catalogue_tail_limit = max(
        tail_limit - len(hybrid_product_ids),
        0,
    )
    if tail_allowed and has_category_anchor and catalogue_tail_limit:
        excluded_candidates = candidates + retrieved.get("hybrid_tail_candidates", [])
        related_product_ids = tail_lookup(
            bm25_index,
            planned["resolved_filters"],
            inferred_categories,
            query_plan["target_ad_type"],
            catalogue_tail_limit,
            exclude_doc_ids={result["id"] for result in excluded_candidates},
            exclude_product_ids={*primary_product_ids, *hybrid_product_ids},
            type_fetcher=type_fetcher,
            sort_order=query_plan.get("sort_order"),
            allowed_ad_types=allowed_ad_types,
        )
    remaining_tail_limit = max(
        tail_limit - len(hybrid_product_ids) - len(related_product_ids),
        0,
    )
    if remaining_tail_limit and unanchored_hybrid_product_ids:
        related_product_ids.extend(unanchored_hybrid_product_ids[:remaining_tail_limit])
    related_tail_seconds = time.perf_counter() - tail_started
    product_ids = list(
        dict.fromkeys(
            (
                *primary_product_ids,
                *hybrid_product_ids,
                *related_product_ids,
            )
        )
    )
    return (
        primary_product_ids,
        hybrid_product_ids,
        related_product_ids,
        product_ids,
        related_tail_seconds,
    )
