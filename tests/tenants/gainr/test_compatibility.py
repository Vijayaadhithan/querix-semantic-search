import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from api import ProductSearchService
from core.tenant_config import (
    TenantCompatibilityConfig,
    TenantPayloadConfig,
    TenantProfile,
    TenantRateLimit,
    TenantStorageConfig,
)
from storage.mysql import MySQLRuntimeConfig
from tenants.gainr.compatibility import (
    GainrCompatibilityService,
    GainrDatabaseRepository,
    GainrFilterResultRequest,
    GainrSearchFilter,
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
            active_column="is_search_active",
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
        self.ranked_page_call = None

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

    def hydrate_ranked_page(
        self,
        product_ids,
        resolved,
        request_filter,
        allowed_ad_types,
        *,
        page,
        page_size,
    ):
        self.ranked_page_call = (
            product_ids,
            resolved,
            request_filter,
            allowed_ad_types,
            page,
            page_size,
        )
        return self.hydrate_filtered(
            product_ids[(page - 1) * page_size : page * page_size],
            resolved,
            request_filter,
            allowed_ad_types,
        ), len(product_ids)


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


class CaptureAnalyticsStore:
    def __init__(self):
        self.events = []

    def submit(self, event):
        self.events.append(event)
        return True

    def close(self):
        pass


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
    assert search_kwargs["hydrate_products"] is False
    assert repository.filter_ids_call is None
    assert repository.ranked_page_call[0] == [1, 2]
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


def test_deployed_gainr_filter_payload_is_accepted_and_mapped(tmp_path):
    adapter, engine, repository = service(tmp_path)
    request = adapter.parse_filter_result(
        {
            "searchTerm": "comfortable car for long travel",
            "filter": {
                "category_id": "4",
                "subcategory_id": "313",
                "category_type": "1",
                "city_id": 129,
                "attribute_value": ["4897", 5133],
                "rental_duration": ["Per Day"],
                "ad_type": [1],
                "fee": [1],
                "fee_max": "5000",
                "fee_min": "500",
                "locality_id": [156307],
                "sort_by": "1",
            },
            "page": 2,
        }
    )

    response = adapter.filter_results(request)

    assert request.filter.category_id == 4
    assert request.filter.subcategory_id == 313
    assert request.filter.category_type == 1
    assert request.filter.attribute_value == [4897, 5133]
    assert request.filter.min_fee == 500
    assert request.filter.max_fee == 5000
    assert request.filter.sort_by == 1
    search_kwargs = engine.calls[0][2]
    assert search_kwargs["resolved_filters"]["categorical"] == {
        "main_category_id": 4,
        "subcategory_id": 313,
        "city_id": 129,
        "locality_id": [156307],
        "rental_duration": ["Per Day"],
    }
    assert search_kwargs["planned_result"]["query_plan"]["sort_order"] == ("price_asc")
    assert repository.ranked_page_call is not None
    assert response["status"] is True


def test_deployed_gainr_empty_legacy_filter_fields_do_not_fail(tmp_path):
    adapter, _, _ = service(tmp_path, execution_path="deterministic_filter")

    request = adapter.parse_filter_result(
        {
            "searchTerm": "",
            "filter": {
                "category_id": "",
                "subcategory_id": "",
                "category_type": "",
                "city_id": "",
                "attribute_value": [],
                "rental_duration": [],
                "ad_type": [],
                "fee": [],
                "fee_max": "",
                "fee_min": "",
                "locality_id": "",
                "sort_by": "",
            },
            "page": 2,
        }
    )

    response = adapter.filter_results(request)

    assert request.filter.city_id is None
    assert request.filter.locality_id == []
    assert response["current_page"] == 2
    assert response["last_page"] == 3
    assert len(response["data"]) == 1


def test_filter_result_capacity_bounds_planning_and_hydration(tmp_path):
    adapter, engine, _repository = service(tmp_path)
    adapter.product_search_service._search_slots = threading.BoundedSemaphore(1)
    adapter.product_search_service.search_slot_timeout_seconds = 0.01
    entered = threading.Event()
    release = threading.Event()
    original_plan = engine.plan

    def blocking_plan(query):
        entered.set()
        release.wait(timeout=2)
        return original_plan(query)

    engine.plan = blocking_plan
    request = adapter.parse_filter_result(
        {"searchTerm": "family bike", "filter": {}, "page": 1}
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(adapter.filter_results, request)
        assert entered.wait(timeout=1)
        second = executor.submit(adapter.filter_results, request)
        with pytest.raises(RuntimeError, match="Search capacity is busy"):
            second.result(timeout=1)
        release.set()
        assert first.result(timeout=2)["status"] is True


def test_filter_result_queues_minimized_durable_history(tmp_path):
    adapter, _engine, _repository = service(tmp_path)
    analytics = CaptureAnalyticsStore()
    adapter.product_search_service.analytics_store = analytics
    request = adapter.parse_filter_result(
        {
            "searchTerm": "family bike",
            "filter": {"city_id": 456},
            "page": 1,
        }
    )

    response = adapter.filter_results(request, user_id="user-7")

    assert response["status"] is True
    event = analytics.events[0]
    assert event.query_text == "family bike"
    assert not hasattr(event, "user_id")
    assert not hasattr(event, "filters")
    assert event.result_count == len(response["data"])
    assert event.plan_cache_hit is False
    assert event.result_cache_hit is False
    assert event.context["city_id"] == 456
    assert event.context["target_ad_type"] == "offer"
    assert "city" not in event.context
    assert event.timings_ms["total_server_ms"] == pytest.approx(
        event.duration_ms,
        abs=0.001,
    )
    assert set(event.timings_ms) >= {
        "planning_ms",
        "engine_total_ms",
        "eligibility_ms",
        "hydration_ms",
        "response_mapping_ms",
        "usage_recording_ms",
        "recent_search_ms",
    }


def test_unfiltered_semantic_search_reuses_current_engine_rows(tmp_path):
    adapter, engine, repository = service(tmp_path)

    class RepositoryConfig:
        result_id_column = "id"

    repository.config = RepositoryConfig()

    def plan(query):
        return {
            "query_plan": {
                "semantic_query": query,
                "keyword_query": query,
                "target_ad_type": "offer",
                "sort_order": None,
                "execution_path": "semantic",
                "inferred_categories": {},
            },
            "resolved_filters": {"categorical": {}},
            "unresolved_filters": {},
            "query_model_metrics": {},
            "seconds": 0.0,
            "plan_cache_hit": False,
        }

    def search(query, limit=None, **kwargs):
        engine.calls.append((query, limit, kwargs))
        return {
            "query_plan": kwargs["planned_result"]["query_plan"],
            "resolved_filters": kwargs["resolved_filters"],
            "unresolved_filters": {},
            "products": [
                {"id": 1, "type": "1", "deleted_at": None},
                {"id": 2, "type": "2", "deleted_at": None},
                {"id": 3, "type": "1", "deleted_at": "2026-01-01"},
            ],
            "product_ids": [1, 2, 3],
            "query_model_metrics": {},
            "reranker_attempts": [],
        }

    engine.plan = plan
    engine.search = search
    request = GainrFilterResultRequest.model_validate(
        {"searchTerm": "something useful", "filter": {}, "page": 1}
    )

    adapter.filter_results(request)

    assert engine.calls[0][2]["hydrate_products"] is True
    assert repository.filter_ids_call is None
    assert repository.hydrate_call[0] == [1]


def test_explicit_ids_clear_conflicting_inferred_filter_hierarchy(tmp_path):
    adapter, engine, _repository = service(tmp_path)

    def plan(_query):
        planned = FakeEngine().plan(_query)
        planned["resolved_filters"]["categorical"].update(
            {
                "main_category_name": "Audio & Video Equipments",
                "state_name": "Tamil Nadu",
                "locality_name": "T Nagar",
            }
        )
        return planned

    engine.plan = plan
    request = adapter.parse_filter_result(
        {
            "searchTerm": "camera in Chennai",
            "filter": {
                "city_id": 81,
                "subcategory_id": 313,
            },
            "page": 1,
        }
    )

    _planned, effective, meta = adapter._effective_plan(request)

    assert effective["categorical"] == {
        "rental_duration": "Per Day",
        "city_id": 81,
        "subcategory_id": 313,
    }
    assert meta["ignored_auto_filters"] == {
        "state_name": "Tamil Nadu",
        "city_name": "Chennai",
        "locality_name": "T Nagar",
        "main_category_name": "Audio & Video Equipments",
        "subcategory_name": "Bike",
    }


def test_chat_location_is_discarded_without_a_structured_location(tmp_path):
    adapter, _engine, _repository = service(tmp_path)
    request = adapter.parse_filter_result(
        {
            "searchTerm": "bike in Chennai",
            "filter": {},
            "page": 1,
        }
    )

    _planned, effective, meta = adapter._effective_plan(request)

    assert effective["categorical"] == {
        "subcategory_name": "Bike",
        "rental_duration": "Per Day",
    }
    assert meta["ignored_auto_filters"] == {
        "city_name": "Chennai",
    }


def test_explicit_locality_id_clears_inferred_location_hierarchy(tmp_path):
    adapter, engine, _repository = service(tmp_path)

    def plan(_query):
        planned = FakeEngine().plan(_query)
        planned["resolved_filters"]["categorical"].update(
            {
                "state_name": "Tamil Nadu",
                "locality_name": "T Nagar",
            }
        )
        return planned

    engine.plan = plan
    request = adapter.parse_filter_result(
        {
            "searchTerm": "bike near T Nagar",
            "filter": {"locality_id": [163496]},
            "page": 1,
        }
    )

    _planned, effective, _meta = adapter._effective_plan(request)

    assert effective["categorical"] == {
        "subcategory_name": "Bike",
        "rental_duration": "Per Day",
        "locality_id": [163496],
    }


def test_deterministic_result_uses_full_catalog_pagination(tmp_path):
    adapter, engine, repository = service(
        tmp_path,
        execution_path="deterministic_filter",
    )
    adapter._category_id_index = {
        "subcategory_name": {"bike": 312},
    }
    request = GainrFilterResultRequest.model_validate(
        {"searchTerm": "Bike", "filter": {}, "page": 2}
    )

    response = adapter.filter_results(request)

    assert engine.calls == []
    assert repository.catalog_call[2]["page"] == 2
    assert repository.catalog_call[2]["page_size"] == 20
    assert repository.catalog_call[2]["allowed_ad_types"] == {"1"}
    assert repository.catalog_call[0]["categorical"] == {
        "subcategory_id": 312,
        "rental_duration": "Per Day",
    }
    assert response["current_page"] == 2
    assert response["last_page"] == 3
    assert response["search_meta"]["total_results"] == 41
    monitor = adapter.product_search_service.monitor_status()
    assert monitor["completed"] == 1
    assert monitor["recent"][0]["execution_path"] == "deterministic_filter"
    assert [event["step"] for event in monitor["recent"][0]["timeline"]] == [
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

    def hydrate(
        product_ids,
        *_args,
        page,
        page_size,
    ):
        selected = product_ids[(page - 1) * page_size : page * page_size]
        hydrated_pages.append(list(selected))
        return (
            [{"id": str(product_id)} for product_id in selected],
            len(product_ids),
        )

    monkeypatch.setattr(engine, "search", search)
    monkeypatch.setattr(repository, "hydrate_ranked_page", hydrate)
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


def test_ranked_page_query_preserves_order_total_and_relations(
    tmp_path,
    monkeypatch,
):
    repository = GainrDatabaseRepository(profile(tmp_path))
    executions = []

    class Cursor:
        result_index = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, params):
            executions.append((sql, tuple(params)))
            self.result_index = len(executions)

        def fetchall(self):
            if self.result_index == 1:
                return [
                    {
                        "id": 2,
                        "user_id": 7,
                        "__eligible_total": 2,
                    },
                    {
                        "id": 1,
                        "user_id": 8,
                        "__eligible_total": 2,
                    },
                ]
            if self.result_index == 2:
                return [
                    {
                        "ads_id": 2,
                        "attribute_id": 959,
                        "value": "hourly",
                    }
                ]
            if self.result_index == 3:
                return [
                    {"user_id": 7, "service_ad_count": 3},
                    {"user_id": 8, "service_ad_count": 1},
                ]
            return [
                {
                    "id": 7,
                    "name": "Renter",
                },
                {
                    "id": 8,
                    "name": "Owner",
                },
            ]

    class Connection:
        def cursor(self):
            return Cursor()

        def rollback(self):
            return None

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(repository, "connection", connection)

    rows, total = repository.hydrate_ranked_page(
        [2, 1],
        {"categorical": {"city_id": 456}},
        GainrSearchFilter(),
        {"1"},
        page=1,
        page_size=20,
    )

    assert total == 2
    assert [row["id"] for row in rows] == [2, 1]
    assert rows[0]["__user"]["id"] == 7
    assert rows[0]["__user"]["name"] == "Renter"
    assert rows[0]["__ads_attributes"] == [
        {
            "ads_id": 2,
            "attribute_id": 959,
            "value": "hourly",
        }
    ]
    assert rows[1]["__ads_attributes"] == []
    assert "COUNT(*) OVER () AS __eligible_total" in executions[0][0]
    assert "AS __rank_order" in executions[0][0]
    assert "JOIN `ads` AS page_ad" in executions[0][0]
    assert "`sr`.`is_search_active` = 1" in executions[0][0]
    assert executions[0][1] == (2, 1, 456, "1", 2, 1, 20, 0)
    assert executions[1][1] == (2, 1)


def test_search_ready_ranked_page_uses_one_query_and_embedded_card_relations(
    tmp_path,
    monkeypatch,
):
    repository = GainrDatabaseRepository(
        profile(tmp_path, serves_cards_from_search_ready=True)
    )
    executions = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, params):
            executions.append((sql, tuple(params)))

        def fetchall(self):
            return [
                {
                    "id": 2,
                    "user_id": 7,
                    "city_name": "Mumbai",
                    "locality_name": "City",
                    "service_ad_count": 3,
                    "ads_attributes_json": (
                        '[{"ads_id":2,"attribute_id":959,"value":12121}]'
                    ),
                    "user_prosper_id": "AA0007",
                    "user_name": "Public User",
                    "user_photo": "profile.jpg",
                    "user_is_aadhaar_gst_verified": 1,
                    "__eligible_total": 1,
                    "__rank_order": 1,
                }
            ]

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(repository, "connection", connection)

    rows, total = repository.hydrate_ranked_page(
        [2, 1],
        {"categorical": {"city_id": 456}},
        GainrSearchFilter(),
        {"1"},
        page=1,
        page_size=20,
    )

    assert total == 1
    assert len(executions) == 1
    assert "JOIN `ads`" not in executions[0][0]
    assert "FROM `users`" not in executions[0][0]
    assert "FROM `ads_attributes`" not in executions[0][0]
    assert executions[0][1] == (2, 1, 456, "1", 2, 1, 20, 0)
    assert rows[0]["__ads_attributes"] == [
        {"ads_id": 2, "attribute_id": 959, "value": 12121}
    ]
    assert rows[0]["__user"] == {
        "id": 7,
        "prosper_id": "AA0007",
        "name": "Public User",
        "photo": "profile.jpg",
        "is_aadhaar_gst_verified": 1,
    }


def test_search_ready_catalog_counts_without_materializing_full_rows(
    tmp_path,
    monkeypatch,
):
    repository = GainrDatabaseRepository(
        profile(tmp_path, serves_cards_from_search_ready=True)
    )
    executions = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, params):
            executions.append((sql, tuple(params)))

        def fetchone(self):
            return {"total": 41}

        def fetchall(self):
            return [
                {
                    "id": 2,
                    "user_id": 7,
                    "city_name": "Mumbai",
                    "locality_name": "City",
                    "ads_attributes_json": "[]",
                }
            ]

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(repository, "connection", connection)

    rows, total = repository.search_catalog(
        {"categorical": {"subcategory_name": "Car"}},
        GainrSearchFilter(),
        search_term="car",
        page=1,
        page_size=20,
        sort_order=None,
        allowed_ad_types={"1"},
    )

    assert total == 41
    assert [row["id"] for row in rows] == [2]
    assert len(executions) == 2
    assert "SELECT COUNT(*) AS total" in executions[0][0]
    assert "COUNT(*) OVER" not in executions[1][0]
    assert "ORDER BY sr.updated_at DESC, sr.id DESC" in executions[1][0]
    assert executions[0][1] == ("Car", "1")
    assert executions[1][1] == ("Car", "1", 20, 0)


def test_pooled_relation_hydration_runs_independent_queries_concurrently(
    tmp_path,
):
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    class Cursor:
        rows = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, _params):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            if "FROM `ads_attributes`" in sql:
                self.rows = [{"ads_id": 2, "attribute_id": 959, "value": "hourly"}]
            elif "service_ad_count" in sql:
                self.rows = [{"user_id": 7, "service_ad_count": 3}]
            else:
                self.rows = [{"id": 7, "name": "Renter"}]

        def fetchall(self):
            return self.rows

    class Connection:
        def cursor(self):
            return Cursor()

    class Pool:
        @contextmanager
        def connection(self):
            yield Connection()

    repository = GainrDatabaseRepository(
        profile(tmp_path),
        database_pool=Pool(),
    )
    rows = [{"id": 2, "user_id": 7}]

    repository._attach_attributes(rows)

    assert maximum_active >= 2
    assert rows[0]["__ads_attributes"] == [
        {"ads_id": 2, "attribute_id": 959, "value": "hourly"}
    ]
    assert rows[0]["service_ad_count"] == 3
    assert rows[0]["__user"] == {"id": 7, "name": "Renter"}


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
    assert card["is_aadhar_gst_verified"]["name"] == "Verified User"
    assert "available_credit" not in card["is_aadhar_gst_verified"]
    assert "email" not in card["is_aadhar_gst_verified"]
    assert "phone" not in card["is_aadhar_gst_verified"]
    assert "fcm_token" not in card["is_aadhar_gst_verified"]


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
    assert "`sr`.`is_search_active` = 1" in where_clause
    assert params == ["1"]


def test_gainr_repository_applies_legacy_category_and_attribute_filters(tmp_path):
    repository = GainrDatabaseRepository(
        profile(tmp_path, serves_cards_from_search_ready=True)
    )
    request_filter = GainrSearchFilter.model_validate(
        {
            "category_type": 1,
            "attribute_value": [4897, "automatic"],
        }
    )

    where_clause, params = repository._where_clause(
        {"categorical": {"main_category_id": 4, "subcategory_id": 313}},
        request_filter,
        allowed_ad_types={"1"},
    )

    assert "sr.main_category_id = %s" in where_clause
    assert "sr.subcategory_id = %s" in where_clause
    assert "sr.category_type = %s" in where_clause
    assert where_clause.count("JSON_CONTAINS") == 2
    assert "a." not in where_clause
    assert params == [
        4,
        313,
        1,
        '{"value": 4897}',
        '{"value": "automatic"}',
        "1",
    ]


def test_gainr_repository_applies_user_gender_during_final_hydration(tmp_path):
    repository = GainrDatabaseRepository(
        profile(tmp_path, serves_cards_from_search_ready=True)
    )

    where_clause, params = repository._where_clause(
        {"categorical": {"user_gender": 2}},
        GainrFilterResultRequest().filter,
        product_ids=[127869, 126564],
        allowed_ad_types={"1"},
    )

    assert "sr.user_gender = %s" in where_clause
    assert "sr.id IN (%s, %s)" in where_clause
    assert params == [2, "1", 127869, 126564]


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

    assert [item["value"] for item in adapter.recent_searches("user-a")["data"]] == [
        "bike"
    ]
    assert [item["value"] for item in adapter.recent_searches("user-b")["data"]] == [
        "camera"
    ]
    assert adapter.recent_searches(None)["data"] == []


def test_concurrent_recent_search_updates_do_not_lose_entries(tmp_path, monkeypatch):
    adapter, _, _ = service(tmp_path)
    original_get = adapter._get_cached
    active_gets = 0
    max_active_gets = 0
    counter_lock = threading.Lock()

    def slow_get(key):
        nonlocal active_gets, max_active_gets
        with counter_lock:
            active_gets += 1
            max_active_gets = max(max_active_gets, active_gets)
        time.sleep(0.01)
        try:
            return original_get(key)
        finally:
            with counter_lock:
                active_gets -= 1

    monkeypatch.setattr(adapter, "_get_cached", slow_get)
    values = [f"query-{number}" for number in range(8)]

    with ThreadPoolExecutor(max_workers=len(values)) as executor:
        list(
            executor.map(lambda value: adapter.remember_search("user-a", value), values)
        )

    recent = adapter.recent_searches("user-a")["data"]
    assert max_active_gets == 1
    assert {item["value"] for item in recent} == set(values)
    assert len({item["id"] for item in recent}) == len(values)


def test_recent_search_response_matches_gainr_contract(tmp_path, monkeypatch):
    adapter, _, _ = service(tmp_path)
    monkeypatch.setattr("tenants.gainr.compatibility.time.time", lambda: 3951.953)
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
        (item["value"], item["is_prosper"]) for item in response["data"]
    ] == expected
    assert all(list(item) == ["id", "value", "is_prosper"] for item in response["data"])
    assert all(isinstance(item["id"], int) for item in response["data"])
    assert len({item["id"] for item in response["data"]}) == 10
