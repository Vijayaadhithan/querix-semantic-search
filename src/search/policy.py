from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CategoryIntent:
    """A tenant-confirmed query phrase that safely selects one catalog type."""

    subcategory: str
    consumed_tokens: frozenset[str]


class SearchPolicy(Protocol):
    """Tenant-owned hooks around otherwise generic planning and ranking."""

    cache_key: str

    def rewrite_semantic_query(self, query: str, semantic_query: str) -> str:
        ...

    def rewrite_keyword_query(self, query: str, keyword_query: str) -> str:
        ...

    def infer_subcategory(self, query: str, values: dict) -> str | None:
        ...

    def category_intent(
        self,
        query: str,
        values: dict,
    ) -> CategoryIntent | None:
        ...

    def infer_main_category(self, query: str, values: dict) -> str | None:
        ...

    def adjust_candidates(
        self,
        query_plan: dict,
        candidates: list[dict],
    ) -> list[dict]:
        ...

    def rerank_context(self, query_plan: dict | None) -> str | None:
        ...


class DefaultSearchPolicy:
    """Identity policy used by tenants without marketplace-specific rules."""

    cache_key = "default-v1"

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

    def adjust_candidates(
        self,
        query_plan: dict,
        candidates: list[dict],
    ) -> list[dict]:
        return candidates

    def rerank_context(self, query_plan: dict | None) -> str | None:
        return None


DEFAULT_SEARCH_POLICY = DefaultSearchPolicy()
