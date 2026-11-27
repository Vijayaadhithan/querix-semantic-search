"""Validated tenant configuration contracts shared by loaders and registries."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.settings import PROJECT_ROOT
from storage.mysql import MySQLRuntimeConfig
from storage.postgres import PostgresRuntimeConfig
from tenants.compatibility import supported_compatibility_adapters

TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

DEFAULT_TENANT_CONFIG_DIR = PROJECT_ROOT / "configs" / "tenants"

CANONICAL_INDEX_FIELDS = frozenset(
    {
        "ad_type",
        "city_id",
        "city_name",
        "is_rent_negotiable",
        "locality_id",
        "locality_name",
        "main_category_id",
        "main_category_name",
        "rental_duration",
        "rental_fee",
        "state_id",
        "state_name",
        "subcategory_id",
        "subcategory_name",
        "user_gender",
        "user_is_aadhaar_gst_verified",
    }
)


@dataclass(frozen=True)
class TenantStorageConfig:
    bm25_path: Path
    vector_backend: str = "pgvector"
    pgvector_database: PostgresRuntimeConfig | None = None
    pgvector_table: str = "search_vectors"
    vector_dimensions: int = 768
    pgvector_hnsw_m: int = 16
    pgvector_hnsw_ef_construction: int = 64
    pgvector_hnsw_ef_search: int = 100
    pgvector_query_mode: str = "legacy"
    pgvector_city_membership: bool = False
    pgvector_prewarm_on_startup: bool = False
    pgvector_prewarm_mode: str = "read"


@dataclass(frozen=True)
class TenantRateLimit:
    requests_per_minute: int = 60
    burst: int = 10

    def __post_init__(self) -> None:
        if self.requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be greater than zero")
        if self.burst <= 0:
            raise ValueError("rate-limit burst must be greater than zero")


@dataclass(frozen=True)
class TenantRetrievalConfig:
    adaptive_vector_post_filter_metadata: bool = False
    city_vector_exact_filter_max_rows: int | None = None
    semantic_related_tail_enabled: bool = True
    semantic_related_tail_requires_explicit_category: bool = False
    reranker_relative_score_floor: float = 0.0
    reranker_min_score_by_provider: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            self.city_vector_exact_filter_max_rows is not None
            and self.city_vector_exact_filter_max_rows <= 0
        ):
            raise ValueError("city_vector_exact_filter_max_rows must be positive")
        if not 0 <= self.reranker_relative_score_floor <= 1:
            raise ValueError("reranker_relative_score_floor must be between 0 and 1")
        for provider, score in self.reranker_min_score_by_provider.items():
            if not provider:
                raise ValueError(
                    "reranker_min_score_by_provider keys must not be empty"
                )
            if not math.isfinite(score):
                raise ValueError("reranker_min_score_by_provider values must be finite")


@dataclass(frozen=True)
class TenantPayloadConfig:
    public_fields: tuple[str, ...]
    field_mapping: dict[str, str] = field(default_factory=dict)
    filter_schema: dict[str, str] = field(default_factory=dict)
    request_mapping: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TenantCompatibilityConfig:
    adapter: str = ""
    users_table: str = "users"
    page_size: int = 20
    semantic_ranked_window: int = 40
    suggestions_limit: int = 8
    recent_limit: int = 10
    recent_ttl_seconds: int = 60 * 60 * 24 * 90
    min_fee_field: str = "min_fee"
    max_fee_field: str = "max_fee"
    fixed_fee_id: int = 1
    negotiable_fee_id: int = 0
    emit_search_meta: bool = True
    image_path: str = ""
    serves_cards_from_search_ready: bool = False

    def __post_init__(self) -> None:
        supported_adapters = set(supported_compatibility_adapters())
        if self.adapter and self.adapter not in supported_adapters:
            supported = ", ".join(supported_compatibility_adapters())
            raise ValueError(
                f"Unsupported compatibility adapter {self.adapter!r}; "
                f"expected one of: {supported}"
            )
        if not self.users_table:
            raise ValueError("Compatibility users_table must not be empty")
        if self.page_size <= 0 or self.page_size > 100:
            raise ValueError("Compatibility page_size must be between 1 and 100")
        if (
            self.semantic_ranked_window < self.page_size
            or self.semantic_ranked_window > 100
        ):
            raise ValueError(
                "Compatibility semantic_ranked_window must be between page_size and 100"
            )
        if self.suggestions_limit <= 0 or self.suggestions_limit > 50:
            raise ValueError("Compatibility suggestions_limit must be between 1 and 50")
        if self.recent_limit <= 0 or self.recent_limit > 50:
            raise ValueError("Compatibility recent_limit must be between 1 and 50")
        if self.recent_ttl_seconds <= 0:
            raise ValueError(
                "Compatibility recent_ttl_seconds must be greater than zero"
            )
        if not self.min_fee_field or not self.max_fee_field:
            raise ValueError("Compatibility fee field names must not be empty")
        if self.min_fee_field == self.max_fee_field:
            raise ValueError("Compatibility fee field names must be different")
        if self.fixed_fee_id == self.negotiable_fee_id:
            raise ValueError("Fixed and negotiable fee IDs must be different")
        if self.image_path and not self.image_path.endswith("/"):
            raise ValueError("Compatibility image_path must end with '/'")


@dataclass(frozen=True)
class TenantAnalyticsConfig:
    enabled: bool = False
    search_history_table: str = "semantic_search_history"
    api_usage_table: str = "semantic_search_api_usage"
    queue_capacity: int = 1000

    def __post_init__(self) -> None:
        identifier_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
        for name, value in (
            ("search_history_table", self.search_history_table),
            ("api_usage_table", self.api_usage_table),
        ):
            if not identifier_pattern.fullmatch(value):
                raise ValueError(f"Analytics {name} must be a safe MySQL identifier")
        if self.search_history_table == self.api_usage_table:
            raise ValueError("Analytics table names must be different")
        if self.queue_capacity <= 0 or self.queue_capacity > 10000:
            raise ValueError("Analytics queue_capacity must be between 1 and 10000")


@dataclass(frozen=True)
class TenantIngestionConfig:
    """Map a company search-ready row into the canonical retrieval schema."""

    field_mapping: dict[str, str] = field(default_factory=dict)
    field_defaults: dict[str, bool | int | float | str | None] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        identifier_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
        unknown = sorted(
            (set(self.field_mapping) | set(self.field_defaults))
            - CANONICAL_INDEX_FIELDS
        )
        if unknown:
            raise ValueError(f"Unsupported canonical ingestion fields: {unknown}")
        invalid_sources = sorted(
            source
            for source in self.field_mapping.values()
            if not identifier_pattern.fullmatch(source)
        )
        if invalid_sources:
            raise ValueError(
                f"Ingestion source fields must be safe identifiers: {invalid_sources}"
            )
        invalid_defaults = sorted(
            key
            for key, value in self.field_defaults.items()
            if not isinstance(value, (bool, int, float, str, type(None)))
        )
        if invalid_defaults:
            raise ValueError(
                f"Ingestion defaults must be scalar values: {invalid_defaults}"
            )


@dataclass(frozen=True)
class TenantProfile:
    company_id: str
    database: MySQLRuntimeConfig | PostgresRuntimeConfig
    storage: TenantStorageConfig
    payload: TenantPayloadConfig
    rate_limit: TenantRateLimit
    planner_adapter: str
    api_key_envs: tuple[str, ...]
    config_path: Path
    endpoint_slug: str = ""
    search_policy: str = "default"
    planner_enabled: bool = True
    planner_prompt_context: str = ""
    planner_query_aliases: dict[str, str] = field(default_factory=dict)
    retrieval: TenantRetrievalConfig = field(default_factory=TenantRetrievalConfig)
    compatibility: TenantCompatibilityConfig = field(
        default_factory=TenantCompatibilityConfig
    )
    analytics: TenantAnalyticsConfig = field(default_factory=TenantAnalyticsConfig)
    tenant_plugin: str = "default"
    ingestion: TenantIngestionConfig = field(default_factory=TenantIngestionConfig)


def validate_tenant_id(value: str) -> str:
    tenant_id = value.strip().casefold()
    if not TENANT_ID_RE.fullmatch(tenant_id):
        raise ValueError(
            f"Unsafe company id {value!r}; use lowercase letters, numbers, "
            "underscores, or hyphens."
        )
    return tenant_id
