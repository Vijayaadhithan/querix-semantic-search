"""Tenant-owned intent interpretation shared by planner execution paths."""

from search.policy import DEFAULT_SEARCH_POLICY, SearchPolicy
from verticals.marketplace.policy import infer_marketplace_target_ad_type

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
