from __future__ import annotations

import re

from search.planner_catalog import normalize_filter_value
from search.policy import DefaultSearchPolicy

_MARKETPLACE_ACTOR_PATTERN = (
    r"(?:people|persons?|someone|somebody|anyone|buyers?|renters?|customers?|"
    r"clients?|users?|business(?:es)?|companies)"
)
_MARKETPLACE_DEMAND_PATTERN = (
    r"(?:need(?:s|ed|ing)?|want(?:s|ed|ing)?|requir(?:e|es|ed|ing)|"
    r"seek(?:s|ing)?|(?:(?:is|are|was|were)\s+)?"
    r"(?:looking|searching)\s+for|(?:(?:is|are|was|were)\s+)?interested\s+in)"
)
_WANTED_INTENT_PATTERNS = (
    re.compile(r"^\s*wanted\b(?!\s+to\b)"),
    re.compile(r"\bwanted\s+(?:ads?|listings?)\b"),
    re.compile(r"\b(?:ads?|listings?)\s+wanted\b"),
    re.compile(r"^\s*(?:requests?|requirements?)\s+(?:for\s+)?\b"),
    re.compile(r"\b(?:requests?|requirements?)\s+(?:ads?|listings?)\b"),
    re.compile(
        rf"\b{_MARKETPLACE_ACTOR_PATTERN}\b(?:\s+[a-z0-9'-]+){{0,4}}\s+"
        rf"{_MARKETPLACE_DEMAND_PATTERN}\b"
    ),
    re.compile(
        r"\b(?:find|show|list|locate)\s+(?:me\s+)?(?:potential\s+)?"
        r"(?:buyers?|renters?|customers?|clients?)\b"
    ),
    re.compile(
        r"\b(?:find|show|list)\s+(?:me\s+)?"
        r"(?:requests?|requirements?|wanted\s+(?:ads?|listings?))\b"
    ),
    re.compile(
        r"\b(?:looking|searching)\s+for\s+"
        r"(?:people|persons?|buyers?|renters?|customers?|clients?)\b"
    ),
    re.compile(
        r"\bwho\s+(?:need(?:s|ed|ing)?|want(?:s|ed|ing)?|"
        r"requir(?:e|es|ed|ing)|seek(?:s|ing)?)\b"
    ),
    re.compile(
        r"\b(?:i|we)\s+(?:(?:currently|already|can|could|want\s+to|"
        r"need\s+to|am|are)\s+){0,3}"
        r"(?:have|own|offer|provide|supply|rent(?:ing)?\s+out|"
        r"leas(?:e|ing)\s+out)\b"
    ),
    re.compile(r"\b(?:rent|lease)\s+out\b"),
    re.compile(
        r"\b(?:suppliers?|providers?|owners?|vendors?|sellers?|landlords?|"
        r"freelancers?)\b(?:\s+[a-z0-9'-]+){0,4}\s+"
        r"(?:looking|searching)\s+for\s+"
        r"(?:work|jobs?|buyers?|renters?|customers?|clients?|leads?)\b"
    ),
)
_OFFER_INTENT_PATTERNS = (
    re.compile(r"\b(?:offer|available)\s+(?:ads?|listings?)\b"),
    re.compile(r"\b(?:ads?|listings?)\s+(?:offered|available)\b"),
    re.compile(
        r"\b(?:i|we)\b(?:\s+[a-z0-9'-]+){0,4}\s+"
        rf"{_MARKETPLACE_DEMAND_PATTERN}\b"
    ),
    re.compile(
        r"^\s*(?:need|want|require|seek|looking\s+for|searching\s+for|"
        r"find\s+me|show\s+me|get\s+me)\b"
    ),
    re.compile(r"\b(?:do|can|could)\s+you\s+(?:have|find|show|provide)\b"),
    re.compile(r"\b(?:for|available\s+for)\s+(?:rent|hire)\b"),
)
_AD_INTENT_SIGNAL_PATTERN = re.compile(
    r"\b(?:wanted|requests?|requirements?|need(?:s|ed|ing)?|"
    r"want(?:s|ed|ing)?|requir(?:e|es|ed|ing)|seek(?:s|ing)?|"
    r"looking|searching|interested|have|own|offer|provide|supply|"
    r"buyers?|renters?|customers?|clients?|suppliers?|providers?|"
    r"owners?|vendors?|sellers?|landlords?|freelancers?)\b"
)


def infer_marketplace_target_ad_type(query: str) -> tuple[str, bool]:
    normalized = normalize_filter_value(query)
    third_person_trailing_demand = bool(
        re.search(r"\b(?:wanted|requests?|requirements?)\s*$", normalized)
        and not re.search(r"\b(?:i|we|you)\b", normalized)
    )
    if third_person_trailing_demand or any(
        pattern.search(normalized) for pattern in _WANTED_INTENT_PATTERNS
    ):
        return "wanted", True
    if any(pattern.search(normalized) for pattern in _OFFER_INTENT_PATTERNS):
        return "offer", True
    if _AD_INTENT_SIGNAL_PATTERN.search(normalized):
        return "offer", False
    return "offer", True


class MarketplaceSearchPolicy(DefaultSearchPolicy):
    """Reusable offer/wanted perspective for classified marketplaces."""

    cache_key = "marketplace-v2"

    def infer_target_ad_type(self, query: str) -> tuple[str, bool]:
        return infer_marketplace_target_ad_type(query)

    def has_natural_search_intent(self, query: str) -> bool:
        target_ad_type, _decisive = self.infer_target_ad_type(query)
        return target_ad_type == "wanted" or super().has_natural_search_intent(query)

    def planner_instructions(self) -> str:
        return (
            "Interpret the request from the searcher's perspective. A person who "
            "wants an available item or service targets offer listings. A supplier "
            "looking for buyers, renters, customers, or clients targets wanted "
            "listings. Use wanted only for explicit request ads, third-party demand, "
            "or an explicit supplier perspective. Use offer as the default listing "
            "perspective when no decisive wanted/request evidence is present."
        )
