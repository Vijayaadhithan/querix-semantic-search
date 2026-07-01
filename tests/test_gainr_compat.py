import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from api import ProductSearchService
from gainr_compat import (
    GainrCompatibilityService,
    GainrFilterResultRequest,
)
from mysql_store import MySQLRuntimeConfig
from tenant_config import (
    TenantCompatibilityConfig,
    TenantPayloadConfig,
    TenantProfile,
    TenantRateLimit,
    TenantStorageConfig,
)


def profile(tmp_path, **compatibility_overrides):
    compatibility = TenantCompatibilityConfig(
        adapter="gainr_legacy",
        **compatibility_overrides,
    )
    return TenantProfile(
        company_id="gainr",
        database=MySQLRuntimeConfig(
            host="localhost",
            port=3306,
            database="gainr",
            user="gainr",
            password="secret",
            search_table="ads_search_ready",
            content_column="embedding_content",
            bm25_column="bm25_content",
            search_id_column="id",
            result_table="ads",
            result_id_column="id",
        ),
        storage=TenantStorageConfig(
            chroma_dir=tmp_path / "chroma",
            collection_name="company_gainr",
            bm25_path=tmp_path / "bm25.sqlite3",
        ),
        payload=TenantPayloadConfig(public_fields=("id",)),
        rate_limit=TenantRateLimit(),
        planner_adapter="gainr",
        api_key_envs=("GAINR_API_KEY",),
        config_path=tmp_path / "gainr.yaml",
        compatibility=compatibility,
    )


class FakeBM25:
    def count(self):
        return 10


class FakeEngine:
    def __init__(self, execution_path="semantic"):
        self.execution_path = execution_path
        self.bm25_index = FakeBM25()
        self.calls = []
        self.ranker = None

    def plan(self, query):
        return {
            "query_plan": {
                "semantic_query": query,
                "keyword_query": query,
                "target_ad_type": "offer",
                "sort_order": None,
                "execution_path": self.execution_path,
                "inferred_categories": {},
            },
            "resolved_filters": {
                "categorical": {
                    "subcategory_name": "Bike",
                    "city_name": "Chennai",
                    "rental_duration": "Per Day",
                },
                "max_rental_fee": 500,
            },
            "unresolved_filters": {},
            "query_model_metrics": {},
            "seconds": 0.0,
            "plan_cache_hit": False,
        }

    def search(self, query, limit=None, **kwargs):
        self.calls.append((query, limit, kwargs))
        return {
            "query_plan": kwargs["planned_result"]["query_plan"],
            "resolved_filters": kwargs["resolved_filters"],
            "unresolved_filters": {},
            "products": [],
            "product_ids": [1, 2],
            "query_model_metrics": {},
            "reranker_attempts": [],
        }


class FakeRepository:
    def __init__(self):
        self.catalog_call = None
        self.hydrate_call = None

    def suggestions(self, term, limit):
        return ["Bike", "Bike Cargo Rider"][:limit]

    def filter_data(self, city_id):
        return ["Per Hour"], [{"id": 7, "area": "Churchgate"}]

    def search_catalog(self, resolved, request_filter, **kwargs):
        self.catalog_call = (resolved, request_filter, kwargs)
        return (
            [
                {
                    "id": "1",
                    "type": "1",
                    "title": "Bike",
                    "rental_fee": "350",
                    "is_rent_negotiable": "0",
                    "city_id": "456",
                    "locality_id": "7",
                    "__city_name": "Mumbai",
                    "__locality_name": "Churchgate",
                }
            ],
            41,
        )

    def hydrate_filtered(
        self,
        product_ids,
        resolved,
        request_filter,
        allowed_ad_types,
    ):
        self.hydrate_call = (
            product_ids,
            resolved,
            request_filter,
            allowed_ad_types,
        )
        return [
            {
                "id": "2",
                "type": "2",
                "title": "Need a bike",
                "rental_fee": "700",
                "is_rent_negotiable": "0",
                "city_id": "456",
                "locality_id": "7",
                "__city_name": "Mumbai",
                "__locality_name": "Churchgate",
            }
        ]


def service(tmp_path, execution_path="semantic", **compatibility):
    engine = FakeEngine(execution_path)
    product_service = ProductSearchService(
        engine,
        max_results=200,
        company_id="gainr",
    )
    repository = FakeRepository()
    adapter = GainrCompatibilityService(
        profile(tmp_path, **compatibility),
        product_service,
        repository=repository,
    )
    return adapter, engine, repository


def test_explicit_filters_override_only_matching_auto_filters(tmp_path):
    adapter, engine, repository = service(tmp_path)
    request = adapter.parse_filter_result(
        {
            "searchTerm": "cheap bike in Chennai per day",
            "filter": {
                "city_id": 456,
                "locality_id": [7, 8],
                "rental_duration": ["Per Hour"],
                "ad_type": [2],
                "fee": [1],
                "min_fee": 100,
                "max_fee": 1000,
            },
            "page": 1,
        }
    )

    response = adapter.filter_results(request, user_id="user-1")

    _, _, search_kwargs = engine.calls[0]
    effective = search_kwargs["resolved_filters"]
    categorical = effective["categorical"]
    assert categorical["subcategory_name"] == "Bike"
    assert "city_name" not in categorical
    assert categorical["city_id"] == 456
    assert categorical["locality_id"] == [7, 8]
    assert categorical["rental_duration"] == ["Per Hour"]
    assert effective["min_rental_fee"] == 100
    assert effective["max_rental_fee"] == 1000
    assert search_kwargs["allowed_ad_types"] == {"2"}
    assert repository.hydrate_call[0] == [1, 2]
    assert response["data"][0]["city"] == {
        "id": 456,
        "city": "Mumbai",
    }
    assert response["search_meta"]["route"] == "semantic"
    assert response["search_meta"]["ignored_auto_filters"] == {
        "city_name": "Chennai",
        "rental_duration": "Per Day",
        "max_rental_fee": 500,
        "target_ad_type": "offer",
    }


def test_deterministic_result_uses_full_catalog_pagination(tmp_path):
    adapter, engine, repository = service(
        tmp_path,
        execution_path="deterministic_filter",
    )
    request = GainrFilterResultRequest.model_validate(
        {"searchTerm": "Bike", "filter": {}, "page": 2}
    )

    response = adapter.filter_results(request)

    assert engine.calls == []
    assert repository.catalog_call[2]["page"] == 2
    assert repository.catalog_call[2]["page_size"] == 20
    assert repository.catalog_call[2]["allowed_ad_types"] == {"1"}
    assert response["current_page"] == 2
    assert response["last_page"] == 3
    assert response["search_meta"]["total_results"] == 41


def test_fee_range_keys_can_be_changed_per_gainr_config(tmp_path):
    adapter, _, _ = service(
        tmp_path,
        min_fee_field="minimum_price",
        max_fee_field="maximum_price",
    )

    request = adapter.parse_filter_result(
        {
            "searchTerm": "Bike",
            "filter": {
                "minimum_price": 100,
                "maximum_price": 900,
            },
            "page": 1,
        }
    )

    assert request.filter.min_fee == 100
    assert request.filter.max_fee == 900


def test_invalid_fee_range_is_rejected():
    with pytest.raises(ValueError, match="min_fee"):
        GainrFilterResultRequest.model_validate(
            {
                "searchTerm": "Bike",
                "filter": {"min_fee": 1000, "max_fee": 100},
                "page": 1,
            }
        )


def test_recent_searches_are_isolated_by_user(tmp_path):
    adapter, _, _ = service(tmp_path)

    adapter.remember_search("user-a", "bike")
    adapter.remember_search("user-b", "camera")
    adapter.remember_search(None, "must not be shared")

    assert [
        item["value"]
        for item in adapter.recent_searches("user-a")["data"]
    ] == ["bike"]
    assert [
        item["value"]
        for item in adapter.recent_searches("user-b")["data"]
    ] == ["camera"]
    assert adapter.recent_searches(None)["data"] == []
