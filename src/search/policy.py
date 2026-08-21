import re
from dataclasses import dataclass
from typing import Protocol

_NATURAL_CATALOG_REQUEST_PATTERN = re.compile(
    r"\b(?:i|im|we|me|my|our|us|you|your|please|people|persons?|someone|"
    r"somebody|anyone|find|show|give|get|help|recommend|suggest|looking|"
    r"searching|need|needs|needed|want|wants|required?|require|requires|"
    r"seek|seeks|seeking|interested|can|could|would|should|who|what|where|"
    r"which|how|do|does)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CategoryIntent:
    """A tenant-confirmed query phrase that safely selects one catalog type."""

    subcategory: str
    consumed_tokens: frozenset[str]
    main_category: str | None = None
    override_explicit_conflict: bool = False
    # Some service phrases name a family of closely related catalog children.
    # Keep the parent hard while using the best child as a ranking preference.
    relax_subcategory: bool = False
    # A translated or spelling-normalized phrase is model-assisted evidence,
    # not the same as an explicit catalogue filter in the user's original
    # wording. Keep its child category soft by default so close sibling
    # categories can fill the page; original-language category intents remain
    # hard boundaries.
    hard_filter_when_normalized: bool = False


class SearchPolicy(Protocol):
    """Tenant-owned hooks around otherwise generic planning and ranking."""

    cache_key: str

    def normalize_query(
        self,
        query: str,
        query_aliases: dict[str, str] | None = None,
    ) -> str: ...

    def extract_user_gender_filter(self, query: str) -> int | None: ...

    def infer_target_ad_type(self, query: str) -> tuple[str, bool]: ...

    def has_natural_search_intent(self, query: str) -> bool: ...

    def planner_instructions(self) -> str: ...

    def rewrite_semantic_query(self, query: str, semantic_query: str) -> str: ...

    def rewrite_keyword_query(self, query: str, keyword_query: str) -> str: ...

    def infer_subcategory(self, query: str, values: dict) -> str | None: ...

    def category_intent(
        self,
        query: str,
        values: dict,
    ) -> CategoryIntent | None: ...

    def infer_main_category(self, query: str, values: dict) -> str | None: ...

    def allows_descriptive_direct_semantic(self, query: str) -> bool: ...

    def allows_decisive_marketplace_direct_semantic(self, query: str) -> bool: ...

    def requires_hosted_planner(self, query: str) -> bool: ...

    def adjust_candidates(
        self,
        query_plan: dict,
        candidates: list[dict],
    ) -> list[dict]: ...

    def rerank_context(self, query_plan: dict | None) -> str | None: ...


class DefaultSearchPolicy:
    """Identity policy used by tenants without marketplace-specific rules."""

    cache_key = "default-v1"

    def normalize_query(
        self,
        query: str,
        query_aliases: dict[str, str] | None = None,
    ) -> str:
        from search.planner_catalog import normalize_transliterated_query

        return normalize_transliterated_query(query, query_aliases)

    def extract_user_gender_filter(self, query: str) -> int | None:
        return None

    def infer_target_ad_type(self, query: str) -> tuple[str, bool]:
        return "offer", True

    def has_natural_search_intent(self, query: str) -> bool:
        return bool(
            _NATURAL_CATALOG_REQUEST_PATTERN.search(query) or re.search(r"[?!]", query)
        )

    def planner_instructions(self) -> str:
        return (
            "This is a general product and service catalog. Target available offer "
            "listings. Do not infer wanted/request advertisements unless a tenant "
            "plugin explicitly enables classified-marketplace behavior."
        )

    def rewrite_semantic_query(self, query: str, semantic_query: str) -> str:
        return semantic_query

    def rewrite_keyword_query(self, query: str, keyword_query: str) -> str:
        return keyword_query

    def infer_subcategory(self, query: str, values: dict) -> str | None:
        return None

    def category_intent(
        self,
        query: str,
        values: dict,
    ) -> CategoryIntent | None:
        return None

    def infer_main_category(self, query: str, values: dict) -> str | None:
        return None

    def allows_descriptive_direct_semantic(self, query: str) -> bool:
        return False

    def allows_decisive_marketplace_direct_semantic(self, query: str) -> bool:
        return False

    def requires_hosted_planner(self, query: str) -> bool:
        return False

    def adjust_candidates(
        self,
        query_plan: dict,
        candidates: list[dict],
    ) -> list[dict]:
        return candidates

    def rerank_context(self, query_plan: dict | None) -> str | None:
        return None


DEFAULT_SEARCH_POLICY = DefaultSearchPolicy()
