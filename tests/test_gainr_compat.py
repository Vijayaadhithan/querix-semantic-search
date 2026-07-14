import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from api import ProductSearchService
from gainr_compat import (
    GainrCompatibilityService,
    GainrDatabaseRepository,
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
        self.filter_ids_call = None
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

    def filter_product_ids(
        self,
        product_ids,
        resolved,
        request_filter,
        allowed_ad_types,
    ):
        self.filter_ids_call = (
            product_ids,
            resolved,
            request_filter,
            allowed_ad_types,
        )
        return list(product_ids)


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
    assert search_kwargs["ranking_window"] == 40
    assert repository.filter_ids_call[0] == [1, 2]
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
    monitor = adapter.product_search_service.monitor_status()
    assert monitor["completed"] == 1
    assert monitor["recent"][0]["execution_path"] == "deterministic_filter"
    assert [
        event["step"] for event in monitor["recent"][0]["timeline"]
    ] == [
        "plan",
        "database_filter",
        "response_map",
        "filter_result",
    ]


def test_semantic_result_hydrates_only_requested_twenty_row_page(
    tmp_path,
    monkeypatch,
):
    adapter, engine, repository = service(tmp_path)

    def search(query, limit=None, **kwargs):
        engine.calls.append((query, limit, kwargs))
        return {
            "query_plan": kwargs["planned_result"]["query_plan"],
            "resolved_filters": kwargs["resolved_filters"],
            "unresolved_filters": {},
            "products": [],
            "product_ids": list(range(1, 46)),
            "query_model_metrics": {},
            "reranker_attempts": [],
        }

    hydrated_pages = []

    def hydrate(product_ids, *_args):
        hydrated_pages.append(list(product_ids))
        return [{"id": str(product_id)} for product_id in product_ids]

    monkeypatch.setattr(engine, "search", search)
    monkeypatch.setattr(repository, "hydrate_filtered", hydrate)
    request = GainrFilterResultRequest.model_validate(
        {
            "searchTerm": "comfortable vehicle",
            "filter": {"city_id": 456},
            "page": 2,
        }
    )

    response = adapter.filter_results(request)

    assert hydrated_pages == [list(range(21, 41))]
    assert [card["id"] for card in response["data"]] == list(range(21, 41))
    assert response["current_page"] == 2
    assert response["last_page"] == 3
    assert engine.calls[0][1] == 200
    assert engine.calls[0][2]["ranking_window"] == 40


def test_public_filter_result_matches_gainr_response_envelope(tmp_path):
    adapter, _, _ = service(
        tmp_path,
        execution_path="deterministic_filter",
        emit_search_meta=False,
        image_path="https://gainr.in/uploads/post/",
    )
    request = GainrFilterResultRequest.model_validate(
        {"searchTerm": "Bike", "filter": {}, "page": 1}
    )

    response = adapter.filter_results(request)

    assert list(response) == [
        "status",
        "message",
        "data",
        "current_page",
        "last_page",
        "image_path",
    ]
    assert response["image_path"] == "https://gainr.in/uploads/post/"
    assert "search_meta" not in response


def test_card_emits_minimal_numeric_attribute_contract(tmp_path):
    adapter, _, _ = service(tmp_path)

    card = adapter._card(
        {
            "id": "235255",
            "user_id": "297587",
            "service_ad_count": "1",
            "city_id": "456",
            "locality_id": "167889",
            "__city_name": "Mumbai",
            "__locality_name": "City",
            "__ads_attributes": [
                {
                    "id": "999",
                    "ads_id": "235255",
                    "attribute_id": "959",
                    "value": "12121",
                    "created_at": "ignored",
                }
            ],
        }
    )

    assert card["service_ad_count"] == 1
    assert card["ads_attributes"] == [
        {
            "ads_id": 235255,
            "attribute_id": 959,
            "value": 12121,
        }
    ]
    assert card["city"] == {"id": 456, "city": "Mumbai"}
    assert card["locality"] == {"id": 167889, "area": "City"}


def test_card_hydrates_compact_and_verified_user_contract(tmp_path):
    adapter, _, _ = service(tmp_path)

    card = adapter._card(
        {
            "id": "235570",
            "user_id": "297952",
            "is_aadhar_gst_verified_count": "0",
            "__user": {
                "id": "297952",
                "prosper_id": "BT6310",
                "name": "Verified User",
                "available_credit": "0.00",
                "city_id": "456",
                "status": "1",
                "is_aadhaar_gst_verified": "1",
            },
        }
    )

    assert card["user"] == {
        "prosper_id": "BT6310",
        "id": 297952,
        "is_aadhaar_gst_verified": 1,
    }
    assert card["is_aadhar_gst_verified_count"] == 1
    assert card["is_aadhar_gst_verified"]["id"] == 297952
    assert card["is_aadhar_gst_verified"]["available_credit"] == 0
    assert card["is_aadhar_gst_verified"]["name"] == "Verified User"


def test_card_keeps_full_verification_null_for_ordinary_user(tmp_path):
    adapter, _, _ = service(tmp_path)

    card = adapter._card(
        {
            "id": "15145",
            "user_id": "4643",
            "__user": {
                "id": "4643",
                "prosper_id": "AA6934",
                "is_aadhaar_gst_verified": "0",
            },
        }
    )

    assert card["user"] == {
        "prosper_id": "AA6934",
        "id": 4643,
        "is_aadhaar_gst_verified": 0,
    }
    assert card["is_aadhar_gst_verified_count"] == 0
    assert card["is_aadhar_gst_verified"] is None


def test_gainr_repository_does_not_filter_ad_status(tmp_path):
    repository = GainrDatabaseRepository(profile(tmp_path))

    where_clause, params = repository._where_clause(
        {"categorical": {}},
        GainrFilterResultRequest().filter,
        allowed_ad_types={"1"},
    )

    assert "a.status" not in where_clause
    assert params == ["1"]


def test_gainr_wanted_budget_keeps_rows_without_a_published_budget(tmp_path):
    repository = GainrDatabaseRepository(profile(tmp_path))

    wanted_clause, wanted_params = repository._where_clause(
        {"categorical": {}, "max_rental_fee": 1000},
        GainrFilterResultRequest().filter,
        allowed_ad_types={"2"},
    )
    offer_clause, offer_params = repository._where_clause(
        {"categorical": {}, "max_rental_fee": 1000},
        GainrFilterResultRequest().filter,
        allowed_ad_types={"1"},
    )

    assert "a.type = %s" in wanted_clause
    assert "sr.rental_fee IS NULL OR sr.rental_fee <= 1" in wanted_clause
    assert wanted_params == ["2", 1000, "2"]
    assert "sr.rental_fee IS NULL" not in offer_clause
    assert offer_params == [1000, "1"]


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


def test_recent_search_response_matches_gainr_contract(tmp_path, monkeypatch):
    adapter, _, _ = service(tmp_path)
    monkeypatch.setattr("gainr_compat.time.time", lambda: 3951.953)
    expected = [
        ("bike", 0),
        ("AA5160", 1),
        ("bike cargo rider", 0),
        ("CB7873", 1),
        ("Mumbai", 0),
        ("car", 0),
        ("Editor", 0),
        ("AY2381", 1),
        ("CB6514", 1),
        ("CA3614", 1),
    ]
    for value, _ in reversed(expected):
        adapter.remember_search("user-a", value)

    response = adapter.recent_searches("user-a")

    assert list(response) == ["status", "data"]
    assert response["status"] is True
    assert [
        (item["value"], item["is_prosper"])
        for item in response["data"]
    ] == expected
    assert all(
        list(item) == ["id", "value", "is_prosper"]
        for item in response["data"]
    )
    assert all(isinstance(item["id"], int) for item in response["data"])
    assert len({item["id"] for item in response["data"]}) == 10
