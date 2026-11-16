from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from pathlib import Path

from core.tenant_database import _path
from core.tenant_loader import (
    load_tenant_profile,
)
from core.tenant_models import (
    CANONICAL_INDEX_FIELDS,
    DEFAULT_TENANT_CONFIG_DIR,
    TENANT_ID_RE,
    TenantAnalyticsConfig,
    TenantCompatibilityConfig,
    TenantIngestionConfig,
    TenantPayloadConfig,
    TenantProfile,
    TenantRateLimit,
    TenantRetrievalConfig,
    TenantStorageConfig,
    validate_tenant_id,
)
from storage.index_layout import (
    index_manifest_path,
    shadow_bm25_path,
    shadow_vector_table,
)
from storage.postgres import PostgresRuntimeConfig

__all__ = (
    "CANONICAL_INDEX_FIELDS",
    "DEFAULT_TENANT_CONFIG_DIR",
    "TENANT_ID_RE",
    "TenantAnalyticsConfig",
    "TenantCompatibilityConfig",
    "TenantIngestionConfig",
    "TenantPayloadConfig",
    "TenantProfile",
    "TenantRateLimit",
    "TenantRegistry",
    "TenantRetrievalConfig",
    "TenantStorageConfig",
    "api_key_digest",
    "discover_tenant_profiles",
    "load_tenant_profile",
    "load_tenant_registry",
    "validate_tenant_id",
    "validate_tenant_isolation",
)


def discover_tenant_profiles(
    directory: Path = DEFAULT_TENANT_CONFIG_DIR,
) -> dict[str, TenantProfile]:
    directory = _path(directory)
    if not directory.exists():
        return {}
    profiles: dict[str, TenantProfile] = {}
    for path in sorted(directory.glob("*.yaml")):
        profile = load_tenant_profile(path)
        if profile.company_id in profiles:
            raise ValueError(f"Duplicate tenant profile: {profile.company_id}")
        profiles[profile.company_id] = profile
    validate_tenant_isolation(profiles.values())
    return profiles


def validate_tenant_isolation(profiles: Iterable[TenantProfile]) -> None:
    source_owners: dict[tuple[str, str, int, str, str, str], tuple[str, str]] = {}
    file_owners: dict[Path, tuple[str, str]] = {}
    endpoint_owners: dict[str, str] = {}

    def claim_table(key, company_id: str, resource: str) -> None:
        owner = source_owners.get(key)
        if owner is not None and owner[0] != company_id:
            raise ValueError(
                f"Tenants {owner[0]!r} and {company_id!r} "
                f"share a company {resource} table ({owner[1]})."
            )
        source_owners[key] = (company_id, resource)

    for profile in profiles:
        endpoint_slug = profile.endpoint_slug or profile.company_id
        endpoint_owner = endpoint_owners.get(endpoint_slug)
        if endpoint_owner is not None:
            raise ValueError(
                f"Tenants {endpoint_owner!r} and {profile.company_id!r} "
                f"share API endpoint slug {endpoint_slug!r}."
            )
        endpoint_owners[endpoint_slug] = profile.company_id

        source_database = profile.database
        source_prefix = (
            "postgres"
            if isinstance(source_database, PostgresRuntimeConfig)
            else "mysql",
            source_database.host.casefold().rstrip("."),
            source_database.port,
            source_database.database,
            (
                source_database.schema
                if isinstance(source_database, PostgresRuntimeConfig)
                else ""
            ),
        )
        for resource_name, table_name in (
            ("search-data", source_database.search_table),
            ("result-data", source_database.result_table),
        ):
            claim_table((*source_prefix, table_name), profile.company_id, resource_name)

        database = profile.storage.pgvector_database
        if database is None:
            raise ValueError(f"Tenant {profile.company_id!r} has no pgvector database.")
        vector_prefix = (
            "postgres",
            database.host.casefold().rstrip("."),
            database.port,
            database.database,
            database.schema,
        )
        for table in (
            profile.storage.pgvector_table,
            shadow_vector_table(profile.storage.pgvector_table),
        ):
            claim_table((*vector_prefix, table), profile.company_id, "pgvector")

        base = profile.storage.bm25_path
        resources = [(index_manifest_path(base), "generation manifest")]
        for bm25 in (base, shadow_bm25_path(base)):
            resources.extend(
                [
                    (bm25, "BM25"),
                    (Path(str(bm25) + "-wal"), "BM25 WAL"),
                    (Path(str(bm25) + "-shm"), "BM25 shared memory"),
                    (bm25.parent / ".ingestion-state.json", "ingestion state"),
                    (bm25.parent / "ingestion-manifest.json", "ingestion manifest"),
                ]
            )
        for path, resource in resources:
            path = path.resolve()
            owner = file_owners.get(path)
            if owner is not None:
                raise ValueError(
                    f"Tenants {owner[0]!r} and {profile.company_id!r} share "
                    f"{resource} path {path} ({owner[1]})."
                )
            file_owners[path] = (profile.company_id, resource)


def api_key_digest(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


class TenantRegistry:
    def __init__(
        self,
        profiles: dict[str, TenantProfile],
        *,
        require_api_keys: bool = True,
        api_keys: dict[str, Iterable[str]] | None = None,
    ):
        if not profiles:
            raise ValueError("At least one tenant profile is required")
        validate_tenant_isolation(profiles.values())
        self._profiles = dict(profiles)
        self._endpoint_profiles = {
            (profile.endpoint_slug or profile.company_id): profile
            for profile in self._profiles.values()
        }
        self._key_owners: dict[str, str] = {}
        supplied = api_keys or {}
        for company_id, profile in self._profiles.items():
            values = [
                value.strip()
                for value in supplied.get(company_id, ())
                if value and value.strip()
            ]
            if not values:
                values = [
                    value.strip()
                    for name in profile.api_key_envs
                    if (value := os.getenv(name, "")).strip()
                ]
            if require_api_keys and not values:
                raise ValueError(
                    f"Tenant {company_id!r} has no configured API key; set one "
                    f"of: {', '.join(profile.api_key_envs)}"
                )
            for value in values:
                digest = api_key_digest(value)
                owner = self._key_owners.get(digest)
                if owner is not None:
                    raise ValueError(
                        f"Tenants {owner!r} and {company_id!r} share an API key"
                    )
                self._key_owners[digest] = company_id

    @property
    def profiles(self) -> dict[str, TenantProfile]:
        return dict(self._profiles)

    def get(self, company_id: str) -> TenantProfile:
        try:
            return self._profiles[validate_tenant_id(company_id)]
        except KeyError as exc:
            raise KeyError(f"Unknown tenant {company_id!r}") from exc

    def resolve_api_key(self, api_key: str) -> TenantProfile | None:
        if not api_key:
            return None
        company_id = self._key_owners.get(api_key_digest(api_key))
        return self._profiles.get(company_id) if company_id else None

    def resolve_endpoint(self, endpoint_slug: str) -> TenantProfile | None:
        try:
            endpoint_slug = validate_tenant_id(endpoint_slug)
        except ValueError:
            return None
        return self._endpoint_profiles.get(endpoint_slug)


def load_tenant_registry(
    directory: Path = DEFAULT_TENANT_CONFIG_DIR,
    *,
    require_api_keys: bool = True,
) -> TenantRegistry:
    return TenantRegistry(
        discover_tenant_profiles(directory),
        require_api_keys=require_api_keys,
    )
