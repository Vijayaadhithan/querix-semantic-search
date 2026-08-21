from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

from core.tenant_config import TenantIngestionConfig
from ingestion.documents import prepare_bm25_index_row
from ingestion.mapping import canonicalize_search_ready_row
from search.bm25 import PersistentBM25Index
from search.planner import (
    deterministic_filter_query_plan,
    direct_semantic_query_plan,
)
from search.planner_catalog import query_filter_value_index
from search.policy_registry import build_search_policy, supported_search_policies
from tenants.compatibility import search_client_contract
from tenants.gainr.policy import GainrSearchPolicy
from tenants.registry import get_tenant_plugin, supported_tenant_plugins

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _build_gainr_catalog(path: Path) -> PersistentBM25Index:
    index = PersistentBM25Index(path)
    index.upsert(
        [
            {
                "doc_id": "bike",
                "product_id": 1,
                "content": "Electric commuter bike",
                "main_category_name": "Vehicles",
                "subcategory_name": "Bike",
                "ad_type": 1,
            },
            {
                "doc_id": "wanted-bike",
                "product_id": 2,
                "content": "Customer looking for a bike",
                "main_category_name": "Vehicles",
                "subcategory_name": "Bike",
                "ad_type": 2,
            },
        ]
    )
    return index


def test_builtin_plugins_keep_default_core_and_gainr_isolated():
    assert supported_tenant_plugins() == ("default", "gainr")
    assert supported_search_policies() == ("default", "gainr")
    assert isinstance(build_search_policy("gainr"), GainrSearchPolicy)

    gainr = build_search_policy("gainr")
    default = build_search_policy("default")
    query = "customers looking for bikes"
    assert gainr.infer_target_ad_type(query) == ("wanted", True)
    assert default.infer_target_ad_type(query) == ("offer", True)
    assert gainr.extract_user_gender_filter("female bike instructor") == 2
    assert default.extract_user_gender_filter("female bike instructor") is None
    assert "rental advertisement marketplace" in gainr.planner_instructions()
    assert "general product and service catalog" in default.planner_instructions()


def test_gainr_legacy_client_contract_is_owned_by_gainr_plugin():
    plugin = get_tenant_plugin("gainr")
    registration = plugin.compatibility_adapters["gainr_legacy"]
    contract = search_client_contract("gainr_legacy")
    profile = SimpleNamespace(
        payload=SimpleNamespace(
            request_mapping={"query": "query", "page_size": "page_size"}
        )
    )

    assert registration.client_contract is contract
    assert contract.route == "filter-result"
    assert contract.blocks_generic_search is True
    assert contract.requires_city_id is True
    assert contract.build_payload(profile, "bike", 20, 129) == {
        "searchTerm": "bike",
        "filter": {"city_id": 129},
        "page": 1,
    }

    generic = search_client_contract()
    assert generic.route == "search"
    assert generic.blocks_generic_search is False
    assert generic.build_payload(profile, "laptop", 12) == {
        "query": "laptop",
        "page_size": 12,
    }


def test_gainr_local_routes_do_not_call_external_providers(tmp_path):
    index = _build_gainr_catalog(tmp_path / "gainr.sqlite3")
    value_index = query_filter_value_index(index)
    policy = build_search_policy("gainr")
    queries = ("Bike", "electric bike", "someone looking for bikes")
    paths = []
    try:
        for query in queries:
            plan = deterministic_filter_query_plan(
                query,
                value_index,
                search_policy=policy,
            )
            if plan is None:
                plan, reason = direct_semantic_query_plan(
                    query,
                    value_index,
                    search_policy=policy,
                )
                assert plan is not None, reason
            paths.append(plan["execution_path"])
    finally:
        index.close()

    assert tuple(paths) == (
        "deterministic_filter",
        "direct_semantic",
        "direct_semantic",
    )


def test_generic_ingestion_mapping_remains_available_for_future_plugins():
    config = TenantIngestionConfig(
        field_mapping={
            "main_category_name": "department",
            "subcategory_name": "product_type",
            "rental_fee": "price",
        },
        field_defaults={"ad_type": 1},
    )
    source = {
        "product_id": 501,
        "bm25_content": "headphones audio wireless noise cancelling",
        "department": "Electronics",
        "product_type": "Headphones",
        "price": 4999,
    }
    row = canonicalize_search_ready_row(source, config)
    indexed = prepare_bm25_index_row(
        row,
        "bm25_content",
        "product_id",
        company_id="example",
    )

    assert indexed is not None
    assert indexed["main_category_name"] == "Electronics"
    assert indexed["subcategory_name"] == "Headphones"
    assert indexed["rental_fee"] == 4999
    assert indexed["ad_type"] == 1


def test_gainr_config_selects_gainr_plugin_and_analytics_adapter():
    raw = yaml.safe_load(
        (PROJECT_ROOT / "configs" / "tenants" / "gainr.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert raw["company"]["plugin"] == "gainr"
    assert raw["company"]["search_policy"] == "gainr"
    assert raw["compatibility"]["adapter"] == "gainr_legacy"
    assert raw["analytics"]["adapter"] == "gainr"
