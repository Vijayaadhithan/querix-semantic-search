"""Measure which explicit or inferred filter blocks available inventory."""

from __future__ import annotations

import copy
from typing import Any

from tenants.gainr.models import (
    GainrFilterResultRequest,
    GainrSearchFilter,
)


def zero_result_filter_diagnostics(
    count_filter_variants,
    request: GainrFilterResultRequest,
    effective: dict,
    allowed_ad_types: set[str],
    *,
    product_ids: list[Any] | None,
) -> dict[str, Any]:
    """Measure which applied filter can recover candidates when removed."""
    variants: dict[str, tuple[dict, GainrSearchFilter, set[str] | None]] = {}

    def without_categorical(*names: str) -> dict:
        relaxed = copy.deepcopy(effective)
        categorical = relaxed.setdefault("categorical", {})
        for name in names:
            categorical.pop(name, None)
        return relaxed

    category_names = (
        "main_category_name",
        "main_category_id",
        "subcategory_name",
        "subcategory_id",
    )
    if (
        any(name in dict(effective.get("categorical") or {}) for name in category_names)
        or request.filter.category_id not in (None, "")
        or (request.filter.subcategory_id not in (None, ""))
    ):
        variants["without_category"] = (
            without_categorical(*category_names),
            request.filter.model_copy(
                update={
                    "category_id": "",
                    "subcategory_id": "",
                    "category_type": "",
                }
            ),
            allowed_ad_types,
        )

    location_names = (
        "state_name",
        "state_id",
        "city_name",
        "city_id",
        "locality_name",
        "locality_id",
    )
    if (
        any(name in dict(effective.get("categorical") or {}) for name in location_names)
        or request.filter.city_id is not None
        or request.filter.locality_id
    ):
        variants["without_location"] = (
            without_categorical(*location_names),
            request.filter.model_copy(update={"city_id": None, "locality_id": []}),
            allowed_ad_types,
        )

    if allowed_ad_types != {"1", "2"} or request.filter.ad_type:
        variants["without_ad_type"] = (
            copy.deepcopy(effective),
            request.filter.model_copy(update={"ad_type": []}),
            {"1", "2"},
        )

    if (
        effective.get("min_rental_fee") is not None
        or effective.get("max_rental_fee") is not None
        or request.filter.fee
    ):
        relaxed = copy.deepcopy(effective)
        relaxed.pop("min_rental_fee", None)
        relaxed.pop("max_rental_fee", None)
        variants["without_price"] = (
            relaxed,
            request.filter.model_copy(
                update={"min_fee": None, "max_fee": None, "fee": []}
            ),
            allowed_ad_types,
        )

    if request.filter.attribute_value:
        variants["without_attributes"] = (
            copy.deepcopy(effective),
            request.filter.model_copy(update={"attribute_value": []}),
            allowed_ad_types,
        )

    if not variants:
        return {
            "evidence_complete": True,
            "diagnosis": "no_structured_filter_blocker",
            "counterfactual_counts": {},
            "blocking_filters": [],
        }
    counts = count_filter_variants(
        variants,
        product_ids=product_ids,
        fallback_term=(request.searchTerm if product_ids is None else ""),
    )
    blocking = [
        name.removeprefix("without_") for name, count in counts.items() if count > 0
    ]
    return {
        "evidence_complete": True,
        "diagnosis": (
            "filter_blocked_candidates"
            if blocking
            else "no_candidates_after_relaxing_single_filters"
        ),
        "counterfactual_counts": counts,
        "blocking_filters": blocking,
    }
