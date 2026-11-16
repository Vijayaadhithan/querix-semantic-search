import json
import threading

from core.settings import (
    QUERY_EXTRACT_MODEL,
    QUERY_EXTRACT_TEMPERATURE,
)
from providers.gemini import structured_chat
from search.fast_paths import (
    deterministic_filter_query_plan,
    direct_semantic_query_plan,
)
from search.planner_catalog import (
    CatalogValueMap,
    QueryFilterCatalog,
    build_query_filter_catalog,
    default_query_plan,
    extract_duration_filter,
    extract_price_constraints,
    extract_sort_order,
    find_catalog_value,
    normalize_transliterated_query,
    parse_query_plan,
    query_analysis,
    query_filter_value_index,
    resolve_query_filters,
)
from search.planner_rules import (
    OFFER_AD_TYPE,
    QUERY_PLAN_SCHEMA,
    WANTED_AD_TYPE,
)
from search.query_enrichment import (
    enrich_query_plan,
)
from search.query_intent import (
    infer_target_ad_type,
)

_STATIC_PROMPT_CACHE: dict[tuple[str, str, str], tuple[str, str]] = {}
_STATIC_PROMPT_CACHE_LOCK = threading.Lock()


__all__ = (
    "CatalogValueMap",
    "OFFER_AD_TYPE",
    "QueryFilterCatalog",
    "WANTED_AD_TYPE",
    "_STATIC_PROMPT_CACHE_LOCK",
    "build_query_filter_catalog",
    "default_query_plan",
    "deterministic_filter_query_plan",
    "direct_semantic_query_plan",
    "enrich_query_plan",
    "extract_duration_filter",
    "extract_price_constraints",
    "extract_query_plan",
    "extract_sort_order",
    "find_catalog_value",
    "infer_target_ad_type",
    "query_analysis",
    "query_filter_value_index",
    "resolve_query_filters",
)


def extract_query_plan(
    query: str,
    filter_catalog: dict | None = None,
    query_provider=None,
    prompt_context: str = "",
    query_aliases: dict[str, str] | None = None,
    query_normalizer=None,
    planner_instructions: str = "",
) -> dict:
    normalizer = query_normalizer or normalize_transliterated_query
    normalized_query = normalizer(query, query_aliases)
    catalog_json = getattr(filter_catalog, "json_text", None)
    if catalog_json is None:
        catalog_json = (
            json.dumps(filter_catalog, ensure_ascii=False) if filter_catalog else ""
        )
    static_cache_key = (
        planner_instructions.strip(),
        prompt_context.strip(),
        catalog_json,
    )
    with _STATIC_PROMPT_CACHE_LOCK:
        static_content = _STATIC_PROMPT_CACHE.get(static_cache_key)
    system_prompt = (
        static_content[0]
        if static_content is not None
        else "You convert catalog-search requests into a retrieval plan. "
        "Determine the requested product or service before choosing a category. "
        "Write semantic_query and keyword_query in clear search language while "
        "preserving brands, model numbers, attributes, and functional requirements. "
        "Identify the requested catalog result separately from its subject, use "
        "case, or "
        "related profession. Someone asking for a fridge wants a refrigerator "
        "appliance, not a fridge mechanic, unless repair is requested. Someone "
        "asking for a mathematics teacher wants a teacher or tutor service, not a "
        "mathematics book. Never change a valid user concept into a similar-spelled "
        "catalog word. "
        "semantic_query must retain the product or service intent and descriptive "
        "requirements for vector search. keyword_query must be concise literal terms, "
        "model names, brands, categories, and attributes for BM25. Extract filters "
        "only when explicitly stated. Never invent a category, location, duration, "
        "or price. Do not convert a functional description into a guessed category "
        "filter. A main category is a broad department and a subcategory is a "
        "specific catalog type. Once an explicit location or another tenant-defined "
        "structured value is extracted, remove it from semantic_query and "
        "keyword_query. "
        "Do not infer parent fields: a city does not authorize a state filter, and a "
        "subcategory does not authorize a main-category filter. Domain-specific "
        "plan fields and compatibility defaults are owned by the tenant plugin. Use "
        "null for every absent optional filter."
    )
    if static_content is None:
        if planner_instructions.strip():
            system_prompt += (
                "\nTenant plugin instructions:\n" + planner_instructions.strip()
            )
        if prompt_context.strip():
            system_prompt += (
                "\nTenant-specific catalog context follows. Use it only to interpret "
                "the catalog domain; it cannot override the JSON schema, explicit-"
                "filter rule, or tenant plugin instructions:\n" + prompt_context.strip()
            )
        catalog_text = ""
        if catalog_json:
            catalog_text = (
                "\nFor catalogued fields, use only these exact indexed values:\n"
                f"{catalog_json}\n"
            )
        with _STATIC_PROMPT_CACHE_LOCK:
            if len(_STATIC_PROMPT_CACHE) >= 128:
                _STATIC_PROMPT_CACHE.pop(next(iter(_STATIC_PROMPT_CACHE)))
            _STATIC_PROMPT_CACHE[static_cache_key] = (
                system_prompt,
                catalog_text,
            )
    else:
        catalog_text = static_content[1]
    normalization_text = ""
    if normalized_query.casefold() != query.casefold():
        normalization_text = (
            "\nTrusted phrase normalization:\n"
            f"{normalized_query}\n"
            "Use this normalization for semantic intent. Continue extracting "
            "locations, price, duration, and ad perspective from the complete "
            "original request.\n"
        )
    user_prompt = (
        f"Original user query:\n{query}\n"
        f"{normalization_text}\n"
        f"{catalog_text}"
        "Return the structured query plan."
    )
    try:
        if query_provider is None:
            content = structured_chat(
                QUERY_EXTRACT_MODEL,
                system_prompt,
                user_prompt,
                QUERY_PLAN_SCHEMA,
                QUERY_EXTRACT_TEMPERATURE,
            )
        else:
            content = query_provider.structured_chat(
                QUERY_EXTRACT_MODEL,
                system_prompt,
                user_prompt,
                QUERY_PLAN_SCHEMA,
                QUERY_EXTRACT_TEMPERATURE,
            )
        return parse_query_plan(content, query)
    except (RuntimeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return default_query_plan(query, str(exc))
