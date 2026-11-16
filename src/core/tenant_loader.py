"""Load tenant YAML into validated configuration contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from core.tenant_database import (
    load_database_config,
    load_storage_config,
)
from core.tenant_models import (
    TENANT_ID_RE,
    TenantAnalyticsConfig,
    TenantCompatibilityConfig,
    TenantIngestionConfig,
    TenantPayloadConfig,
    TenantProfile,
    TenantRateLimit,
    TenantRetrievalConfig,
    validate_tenant_id,
)
from storage.mysql import MySQLRuntimeConfig
from tenants.registry import (
    get_tenant_plugin,
    plugin_for_search_policy,
    supported_tenant_plugins,
)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid tenant YAML: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Tenant profile must contain a YAML object: {path}")
    return raw


def load_tenant_profile(path: Path) -> TenantProfile:
    path = path.resolve()
    raw = _read_yaml(path)
    company = dict(raw.get("company", {}))
    company_id = validate_tenant_id(str(company.get("id", path.stem)))
    if company_id != path.stem:
        raise ValueError(
            f"Tenant profile {path.name} declares company.id={company_id!r}; "
            f"expected {path.stem!r}."
        )
    configured_plugin = str(company.get("plugin", "")).strip().casefold()
    configured_policy = str(company.get("search_policy", "")).strip().casefold()
    if configured_plugin:
        tenant_plugin = get_tenant_plugin(configured_plugin)
    elif configured_policy:
        tenant_plugin = plugin_for_search_policy(configured_policy)
        if tenant_plugin is None:
            supported = ", ".join(supported_tenant_plugins())
            raise ValueError(
                f"Tenant {company_id!r} has unsupported search_policy "
                f"{configured_policy!r}; tenant plugins: {supported}"
            )
    else:
        tenant_plugin = get_tenant_plugin("default")
    planner_adapter = str(
        company.get("planner_adapter", tenant_plugin.default_planner_adapter)
    ).strip()
    if not planner_adapter:
        raise ValueError(f"Tenant {company_id!r} must configure planner_adapter")
    search_policy = configured_policy or tenant_plugin.default_search_policy
    if not TENANT_ID_RE.fullmatch(search_policy):
        raise ValueError(
            f"Tenant {company_id!r} has invalid search_policy {search_policy!r}"
        )
    if search_policy not in tenant_plugin.search_policies:
        supported = ", ".join(sorted(tenant_plugin.search_policies))
        raise ValueError(
            f"Tenant {company_id!r} plugin {tenant_plugin.name!r} does not own "
            f"search_policy {search_policy!r}; expected one of: {supported}"
        )
    planner = dict(raw.get("planner", {}))
    planner_enabled = bool(planner.get("enabled", True))
    planner_prompt_context = str(planner.get("prompt_context", "")).strip()
    if len(planner_prompt_context) > 4000:
        raise ValueError(
            f"Tenant {company_id!r} planner prompt context exceeds 4000 characters"
        )
    raw_query_aliases = planner.get("query_aliases", {})
    if not isinstance(raw_query_aliases, dict):
        raise ValueError(
            f"Tenant {company_id!r} planner query_aliases must be an object"
        )
    if len(raw_query_aliases) > 500:
        raise ValueError(
            f"Tenant {company_id!r} planner query_aliases exceeds 500 entries"
        )
    planner_query_aliases = {}
    for source, target in raw_query_aliases.items():
        normalized_source = " ".join(str(source).casefold().split())
        normalized_target = " ".join(str(target).casefold().split())
        if (
            not normalized_source
            or not normalized_target
            or len(normalized_source) > 100
            or len(normalized_target) > 200
        ):
            raise ValueError(
                f"Tenant {company_id!r} has an invalid planner query alias"
            )
        planner_query_aliases[normalized_source] = normalized_target

    mysql, backend = load_database_config(dict(raw.get("database", {})), company_id)

    storage_config = load_storage_config(
        dict(raw.get("storage", {})),
        company_id,
        mysql,
        backend,
    )

    payload_config = load_payload_config(
        dict(raw.get("payload", {})), company_id, mysql
    )

    rate = dict(raw.get("rate_limit", {}))
    rate_limit = TenantRateLimit(
        requests_per_minute=int(rate.get("requests_per_minute", 60)),
        burst=int(rate.get("burst", 10)),
    )
    retrieval = dict(raw.get("retrieval", {}))
    min_scores = retrieval.get("reranker_min_score_by_provider", {})
    if not isinstance(min_scores, dict):
        raise ValueError(
            f"Tenant {company_id!r} reranker_min_score_by_provider must be a mapping"
        )
    retrieval_config = TenantRetrievalConfig(
        adaptive_vector_post_filter_metadata=bool(
            retrieval.get(
                "adaptive_vector_post_filter_metadata",
                False,
            )
        ),
        city_vector_exact_filter_max_rows=(
            int(retrieval["city_vector_exact_filter_max_rows"])
            if retrieval.get("city_vector_exact_filter_max_rows") is not None
            else None
        ),
        semantic_related_tail_enabled=bool(
            retrieval.get("semantic_related_tail_enabled", True)
        ),
        semantic_related_tail_requires_explicit_category=bool(
            retrieval.get(
                "semantic_related_tail_requires_explicit_category",
                False,
            )
        ),
        reranker_relative_score_floor=float(
            retrieval.get("reranker_relative_score_floor", 0.0)
        ),
        reranker_min_score_by_provider={
            str(provider).strip().casefold(): float(score)
            for provider, score in min_scores.items()
        },
    )
    api = dict(raw.get("api", {}))
    endpoint_slug = validate_tenant_id(str(api.get("endpoint_slug", company_id)))
    api_key_envs = tuple(
        str(value).strip() for value in api.get("key_envs", ()) if str(value).strip()
    )
    if not api_key_envs:
        api_key_envs = (f"{company_id.upper()}_API_KEY",)
    compatibility = dict(raw.get("compatibility", {}))
    compatibility_config = TenantCompatibilityConfig(
        adapter=str(compatibility.get("adapter", "")).strip().casefold(),
        users_table=str(compatibility.get("users_table", "users")).strip(),
        page_size=int(compatibility.get("page_size", 20)),
        semantic_ranked_window=int(compatibility.get("semantic_ranked_window", 40)),
        suggestions_limit=int(compatibility.get("suggestions_limit", 8)),
        recent_limit=int(compatibility.get("recent_limit", 10)),
        recent_ttl_seconds=int(
            compatibility.get(
                "recent_ttl_seconds",
                60 * 60 * 24 * 90,
            )
        ),
        min_fee_field=str(compatibility.get("min_fee_field", "min_fee")).strip(),
        max_fee_field=str(compatibility.get("max_fee_field", "max_fee")).strip(),
        fixed_fee_id=int(compatibility.get("fixed_fee_id", 1)),
        negotiable_fee_id=int(compatibility.get("negotiable_fee_id", 0)),
        emit_search_meta=bool(compatibility.get("emit_search_meta", True)),
        image_path=str(compatibility.get("image_path", "")).strip(),
        serves_cards_from_search_ready=bool(
            compatibility.get("serves_cards_from_search_ready", False)
        ),
    )
    if (
        compatibility_config.adapter
        and compatibility_config.adapter not in tenant_plugin.compatibility_adapters
    ):
        supported = ", ".join(sorted(tenant_plugin.compatibility_adapters)) or "none"
        raise ValueError(
            f"Tenant {company_id!r} plugin {tenant_plugin.name!r} does not own "
            f"compatibility adapter {compatibility_config.adapter!r}; "
            f"expected one of: {supported}"
        )
    analytics = dict(raw.get("analytics", {}))
    analytics_config = TenantAnalyticsConfig(
        enabled=bool(analytics.get("enabled", False)),
        search_history_table=str(
            analytics.get(
                "search_history_table",
                "semantic_search_history",
            )
        ).strip(),
        api_usage_table=str(
            analytics.get(
                "api_usage_table",
                "semantic_search_api_usage",
            )
        ).strip(),
        queue_capacity=int(analytics.get("queue_capacity", 1000)),
    )
    if analytics_config.enabled and not isinstance(
        mysql,
        MySQLRuntimeConfig,
    ):
        raise ValueError(
            f"Tenant {company_id!r} enables analytics, but durable search "
            "analytics currently requires a MySQL company database"
        )
    ingestion = dict(raw.get("ingestion", {}))
    raw_ingestion_mapping = ingestion.get("field_mapping", {})
    raw_ingestion_defaults = ingestion.get("field_defaults", {})
    if not isinstance(raw_ingestion_mapping, dict) or not isinstance(
        raw_ingestion_defaults,
        dict,
    ):
        raise ValueError(
            f"Tenant {company_id!r} ingestion mapping/defaults must be objects"
        )
    ingestion_config = TenantIngestionConfig(
        field_mapping={
            str(canonical).strip(): str(source).strip()
            for canonical, source in raw_ingestion_mapping.items()
            if str(canonical).strip() and str(source).strip()
        },
        field_defaults={
            str(canonical).strip(): value
            for canonical, value in raw_ingestion_defaults.items()
            if str(canonical).strip()
        },
    )

    return TenantProfile(
        company_id=company_id,
        database=mysql,
        storage=storage_config,
        payload=payload_config,
        rate_limit=rate_limit,
        planner_adapter=planner_adapter,
        api_key_envs=api_key_envs,
        config_path=path,
        endpoint_slug=endpoint_slug,
        search_policy=search_policy,
        planner_enabled=planner_enabled,
        planner_prompt_context=planner_prompt_context,
        planner_query_aliases=planner_query_aliases,
        retrieval=retrieval_config,
        compatibility=compatibility_config,
        analytics=analytics_config,
        tenant_plugin=tenant_plugin.name,
        ingestion=ingestion_config,
    )


def load_payload_config(payload: dict, company_id: str, mysql) -> TenantPayloadConfig:
    public_fields = tuple(
        str(value).strip()
        for value in payload.get("public_fields", ())
        if str(value).strip()
    )
    if mysql.result_id_column not in public_fields:
        raise ValueError(
            f"Tenant {company_id!r} payload.public_fields must include "
            f"result id column {mysql.result_id_column!r}."
        )
    field_mapping = {
        str(key).strip(): str(value).strip()
        for key, value in dict(payload.get("field_mapping", {})).items()
        if str(key).strip() and str(value).strip()
    }
    filter_schema = {
        str(key).strip(): str(value).strip().casefold()
        for key, value in dict(payload.get("filter_schema", {})).items()
        if str(key).strip() and str(value).strip()
    }
    allowed_filter_types = {"keyword", "number", "datetime", "boolean"}
    invalid_filter_types = sorted(
        {value for value in filter_schema.values() if value not in allowed_filter_types}
    )
    if invalid_filter_types:
        raise ValueError(
            f"Tenant {company_id!r} has unsupported filter types: "
            f"{invalid_filter_types}"
        )
    request_mapping = {
        "query": "query",
        "cursor": "cursor",
        "page_size": "page_size",
    }
    request_mapping.update(
        {
            str(key).strip(): str(value).strip()
            for key, value in dict(payload.get("request_mapping", {})).items()
            if str(key).strip() and str(value).strip()
        }
    )
    invalid_request_fields = sorted(
        set(request_mapping) - {"query", "cursor", "page_size"}
    )
    if invalid_request_fields:
        raise ValueError(
            f"Tenant {company_id!r} has unsupported request mappings: "
            f"{invalid_request_fields}"
        )
    if len(set(request_mapping.values())) != len(request_mapping):
        raise ValueError(
            f"Tenant {company_id!r} request payload fields must be unique."
        )

    return TenantPayloadConfig(
        public_fields=public_fields,
        field_mapping=field_mapping,
        filter_schema=filter_schema,
        request_mapping=request_mapping,
    )
