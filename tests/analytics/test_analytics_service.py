from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import NullPool

from analytics_service import source as analytics_source
from analytics_service.api import create_app
from analytics_service.auth import (
    COMPANY_USER,
    INTERNAL_ADMIN,
    AnalyticsAuthStore,
)
from analytics_service.config import (
    AnalyticsRegistry,
    AnalyticsSettings,
    CompanyAnalyticsConfig,
    DatabaseTarget,
    DatasetMapping,
    load_company_analytics_config,
)
from analytics_service.filters import DashboardFilters
from analytics_service.service import AnalyticsRefreshService
from analytics_service.source import SqlAnalyticsDataSource, _validate_frame
from analytics_service.store import AnalyticsSnapshotStore
from tenants.gainr.analytics import (
    GAINR_ANALYTICS_CONTRACT,
    GAINR_MARKETPLACE_SCOPE,
    GainrAnalyticsAdapter,
)
from tenants.gainr.analytics_schema import (
    GAINR_DATASET_SPECS,
    GAINR_DEFAULT_TABLES,
)
from verticals.marketplace.analytics.dashboard_filters import (
    build_dashboard_overview,
)
from verticals.marketplace.analytics.domain import (
    build_company_business_insights,
    process_part_a,
    process_part_b,
    process_part_c,
    process_part_d,
)
from verticals.marketplace.analytics.domain.search.records import build_query_records
from verticals.marketplace.analytics.metrics import (
    COMPANY_DEEP_METRICS,
    COMPANY_MARKET_METRICS,
    COMPANY_SEARCH_METRICS,
    INTERNAL_API_METRICS,
)


def analytics_company(tmp_path: Path, company_id: str = "gainr"):
    database = DatabaseTarget(
        backend="mysql",
        host="db",
        port=3306,
        database=f"catalog_{company_id}",
        user="reader",
        password="secret",
        tls_mode="disable",
    )
    return CompanyAnalyticsConfig(
        company_id=company_id,
        endpoint_slug=company_id,
        api_key_envs=(f"{company_id.upper()}_ANALYTICS_API_KEY",),
        database=database,
        telemetry_database=database,
        datasets={
            name: DatasetMapping(table=table)
            for name, table in GAINR_DEFAULT_TABLES.items()
        },
        config_path=tmp_path / f"{company_id}.yaml",
        adapter="gainr",
        dataset_specs=GAINR_DATASET_SPECS,
    )


def test_analytics_registry_rejects_shared_dataset_tables(tmp_path):
    gainr = analytics_company(tmp_path, "gainr")
    acme = analytics_company(tmp_path, "acme")
    acme = replace(
        acme,
        database=gainr.database,
        telemetry_database=gainr.telemetry_database,
    )

    with pytest.raises(ValueError, match="share dataset table"):
        AnalyticsRegistry({"gainr": gainr, "acme": acme})


def analytics_data() -> dict[str, pd.DataFrame]:
    searches = pd.DataFrame(
        [
            {
                "id": 1,
                "request_id": "req-1",
                "query_text": "Tata Ace rent Chennai",
                "created_at": "2026-07-29 10:00:00",
            },
            {
                "id": 2,
                "request_id": "req-2",
                "query_text": "camera rent",
                "created_at": "2026-07-29 11:00:00",
            },
        ]
    )
    usage = pd.DataFrame(
        [
            {
                "id": 10,
                "request_id": "req-1",
                "company_id": "gainr",
                "execution_path": "semantic",
                "result_count": 10,
                "total_results": 20,
                "status": "success",
                "api_call_count": 3,
                "input_tokens": 100,
                "output_tokens": 20,
                "thought_tokens": 0,
                "total_tokens": 120,
                "duration_ms": 500.0,
                "plan_cache_hit": True,
                "result_cache_hit": False,
                "timings_json": (
                    '{"total_server_ms":500.0,"planning_ms":200.0,'
                    '"retrieval_ms":180.0,"reranking_ms":90.0}'
                ),
                "context_json": (
                    '{"main_category":"Vehicles","city_id":301,'
                    '"target_ad_type":"offer"}'
                ),
                "attempts_json": (
                    '[{"attempt_number":1,"provider":"groq",'
                    '"model":"planner","operation":"query_planning",'
                    '"status":"success","total_tokens":120,'
                    '"duration_ms":200}]'
                ),
                "created_at": "2026-07-29 10:00:00",
            },
            {
                "id": 11,
                "request_id": "req-2",
                "company_id": "gainr",
                "execution_path": "deterministic_filter",
                "result_count": 0,
                "total_results": 0,
                "status": "success",
                "api_call_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "thought_tokens": 0,
                "total_tokens": 0,
                "duration_ms": 100.0,
                "plan_cache_hit": False,
                "result_cache_hit": False,
                "timings_json": (
                    '{"total_server_ms":100.0,"planning_ms":5.0,'
                    '"database_filter_ms":80.0}'
                ),
                "context_json": (
                    '{"main_category":"Electronics","city_id":301,'
                    '"target_ad_type":"offer"}'
                ),
                "attempts_json": "[]",
                "created_at": "2026-07-29 11:00:00",
            },
        ]
    )
    ads = pd.DataFrame(
        [
            {
                "id": 101,
                "user_id": 201,
                "category_type": 1,
                "parent_id": 1,
                "category_id": 11,
                "title": "Tata Ace",
                "rental_duration": "Daily",
                "rental_fee": 1000.0,
                "is_rent_negotiable": 1,
                "city_id": 301,
                "locality_id": 401,
                "photos": "photo.jpg",
                "total_favorite": 5,
                "total_like": 2,
                "user_contact_view_count": 4,
                "user_viewed_count": 12,
                "actual_view_count": 20,
                "status": 1,
                "keywords": "truck",
                "top_start_date": "2026-07-01",
                "top_end_date": "2026-08-01",
                "premium_start_date": "2026-07-01",
                "premium_end_date": "2026-08-01",
                "created_at": "2026-06-01",
                "deleted_at": None,
                "description": "A useful rental truck",
            },
            {
                "id": 102,
                "user_id": 202,
                "category_type": 1,
                "parent_id": 2,
                "category_id": 12,
                "title": "Camera",
                "rental_duration": "Daily",
                "rental_fee": 500.0,
                "is_rent_negotiable": 0,
                "city_id": 302,
                "locality_id": 402,
                "photos": None,
                "total_favorite": 1,
                "total_like": 1,
                "user_contact_view_count": 0,
                "user_viewed_count": 2,
                "actual_view_count": 3,
                "status": 1,
                "keywords": None,
                "top_start_date": None,
                "top_end_date": None,
                "premium_start_date": None,
                "premium_end_date": None,
                "created_at": "2026-06-02",
                "deleted_at": None,
                "description": "",
            },
        ]
    )
    users = pd.DataFrame(
        [
            {
                "id": 201,
                "state_id": 501,
                "city_id": 301,
                "gender": 1,
                "platform": 2,
                "device_platform": "android",
                "role": "seller",
                "is_verified": 1,
                "status": 1,
                "created_at": "2026-05-01",
                "updated_at": "2026-07-20",
                "user_type": 1,
                "deleted_at": None,
            },
            {
                "id": 202,
                "state_id": 502,
                "city_id": 302,
                "gender": 2,
                "platform": 3,
                "device_platform": "web",
                "role": "seller",
                "is_verified": 0,
                "status": 1,
                "created_at": "2026-05-02",
                "updated_at": "2026-06-01",
                "user_type": 1,
                "deleted_at": None,
            },
        ]
    )
    return {
        "search_history": searches,
        "api_usage": usage,
        "categories": pd.DataFrame(
            [
                {"id": 1, "name": "Vehicles", "cat_group": 1},
                {"id": 2, "name": "Electronics", "cat_group": 1},
            ]
        ),
        "sub_categories": pd.DataFrame(
            [
                {"id": 11, "categoryId": 1, "name": "Trucks"},
                {"id": 12, "categoryId": 2, "name": "Cameras"},
            ]
        ),
        "states": pd.DataFrame(
            [
                {"id": 501, "name": "Tamil Nadu"},
                {"id": 502, "name": "Karnataka"},
            ]
        ),
        "location": pd.DataFrame(
            [
                {
                    "id": 301,
                    "city": "Chennai",
                    "state_id": 501,
                    "price": 100.0,
                },
                {
                    "id": 302,
                    "city": "Bengaluru",
                    "state_id": 502,
                    "price": 50.0,
                },
            ]
        ),
        "attributes": pd.DataFrame([{"id": 601, "name": "Brand"}]),
        "attribute_values": pd.DataFrame(
            [{"id": 701, "attributeId": 601, "value": "Tata"}]
        ),
        "ads_attributes": pd.DataFrame(
            [{"ads_id": 101, "attribute_id": 601, "value": 701}]
        ),
        "ads": ads,
        "users": users,
    }


def test_company_supply_metrics_use_active_inventory_only():
    data = analytics_data()
    inactive = data["ads"].iloc[0].copy()
    inactive["id"] = 999
    inactive["status"] = 4
    inactive["created_at"] = "2026-01-01"
    data["ads"] = pd.concat([data["ads"], pd.DataFrame([inactive])], ignore_index=True)
    data["ads_attributes"] = pd.concat(
        [
            data["ads_attributes"],
            pd.DataFrame([{"ads_id": 999, "attribute_id": 601, "value": 701}]),
        ],
        ignore_index=True,
    )

    deep = process_part_c(
        {name: frame.copy() for name, frame in data.items()},
        scope=GAINR_MARKETPLACE_SCOPE,
    )
    market = process_part_d(
        {name: frame.copy() for name, frame in data.items()},
        scope=GAINR_MARKETPLACE_SCOPE,
    )
    business = build_company_business_insights(
        data,
        [],
        scope=GAINR_MARKETPLACE_SCOPE,
        marketplace_name="Gainr",
    )

    assert sum(deep["q47_supply_by_category"]["values"]) == 2
    assert sum(deep["q62_ad_status"]["values"]) == 3
    assert sum(deep["q73_attribute_completeness"]["values"]) == 1
    assert sum(market["q76_geographic_heatmap"]["values"]) == 2
    assert market["q80_active_listings"]["title"] == (
        "Current Active Listings by Creation Month"
    )
    assert business["q96_demand_supply_gap"]["demand_window_days"] == 90
    assert business["q96_demand_supply_gap"]["note"].startswith(
        "Available listings are non-deleted ads in active statuses 1 or 8 "
    )


class FakeSource:
    def __init__(self, data):
        self.data = data

    def load(self, company):
        del company
        if isinstance(self.data, Exception):
            raise self.data
        return {name: frame.copy() for name, frame in self.data.items()}


class WrappedAnalyticsAdapter(GainrAnalyticsAdapter):
    def dashboard_response(self, dashboard):
        return {
            "contract": f"{self.company_id}-dashboard",
            "payload": dashboard,
        }

    def queries_response(self, queries):
        return {
            "contract": f"{self.company_id}-queries",
            "payload": queries,
        }

    def status_response(self, status):
        return {
            "contract": f"{self.company_id}-status",
            "payload": status,
        }


def test_tenant_yaml_builds_normalized_sql_source_config(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("TEST_DB_HOST", "db.internal")
    monkeypatch.setenv("TEST_DB_PORT", "3306")
    monkeypatch.setenv("TEST_DB_NAME", "company")
    monkeypatch.setenv("TEST_DB_USER", "reader")
    monkeypatch.setenv("TEST_DB_PASSWORD", "secret")
    monkeypatch.setenv("TEST_DB_TLS_MODE", "require")
    path = tmp_path / "tenant.yaml"
    path.write_text(
        """
company:
  id: testco
api:
  endpoint_slug: testco
  key_envs: [TESTCO_API_KEY]
database:
  backend: mysql
  host_env: TEST_DB_HOST
  port_env: TEST_DB_PORT
  database_env: TEST_DB_NAME
  user_env: TEST_DB_USER
  password_env: TEST_DB_PASSWORD
  tls:
    mode: disable
    mode_env: TEST_DB_TLS_MODE
analytics:
  enabled: true
  endpoint_slug: testco-analytics
  adapter: gainr
  api_key_envs: [TESTCO_ANALYTICS_API_KEY]
  history_days: 120
  metrics:
    company:
      search_intelligence:
        - q1_category_distribution
        - q10_language
      market_intelligence: []
    internal:
      api_performance:
        - q21_success_rate
        - q23_latency_stats
  tables:
    ads: listings
  columns:
    ads:
      id: listing_id
      title: listing_title
  telemetry:
    use_company_database: true
""",
        encoding="utf-8",
    )

    config = load_company_analytics_config(path)

    assert config.database.host == "db.internal"
    assert config.endpoint_slug == "testco-analytics"
    assert config.adapter == "gainr"
    assert config.api_key_envs == ("TESTCO_ANALYTICS_API_KEY",)
    assert config.database.tls_mode == "require"
    assert config.telemetry_database is config.database
    assert config.datasets["ads"].table == "listings"
    assert config.datasets["ads"].columns["id"] == "listing_id"
    assert config.history_days == 120
    assert config.company_metric_profile == {
        "search_intelligence": (
            "q1_category_distribution",
            "q10_language",
        ),
        "market_intelligence": (),
    }
    assert config.internal_metric_profile == {
        "api_performance": (
            "q21_success_rate",
            "q23_latency_stats",
        ),
    }
    sql = SqlAnalyticsDataSource._select_sql(
        config.database,
        config.datasets["ads"],
        GAINR_DATASET_SPECS["ads"],
    )
    assert "`listing_id` AS `id`" in sql
    assert "`listing_title` AS `title`" in sql
    assert "FROM `listings`" in sql
    history_sql = SqlAnalyticsDataSource._select_sql(
        config.database,
        config.datasets["search_history"],
        GAINR_DATASET_SPECS["search_history"],
        history_days=config.history_days,
    )
    assert "`created_at` >= CURRENT_TIMESTAMP - INTERVAL 120 DAY" in history_sql


def test_unknown_company_analytics_adapter_is_rejected(tmp_path):
    company = analytics_company(tmp_path)

    with pytest.raises(ValueError, match="Unsupported analytics adapter"):
        replace(company, adapter="missing")


def test_source_normalizes_configured_numeric_columns():
    frame = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "user_id": [1, 1, 1],
            "category_id": [1, 1, 1],
            "title": ["A", "B", "C"],
            "created_at": ["2026-01-01"] * 3,
            "rental_fee": ["1000.50", "", "not-a-number"],
            "actual_view_count": ["10", "2", None],
        }
    )

    normalized = _validate_frame(
        "ads",
        frame,
        GAINR_DATASET_SPECS["ads"],
    )

    assert normalized["rental_fee"].iloc[0] == 1000.5
    assert pd.isna(normalized["rental_fee"].iloc[1])
    assert pd.isna(normalized["rental_fee"].iloc[2])
    assert normalized["actual_view_count"].iloc[0] == 10


@pytest.mark.parametrize(
    ("backend", "driver"),
    (("mysql", "mysql+pymysql"), ("postgres", "postgresql+psycopg")),
)
def test_sql_source_uses_unpooled_sqlalchemy_connection(
    backend,
    driver,
    monkeypatch,
):
    target = DatabaseTarget(
        backend=backend,
        host="db.internal",
        port=3306 if backend == "mysql" else 5432,
        database="company",
        user="reader",
        password="special:/@password",
        tls_mode="disable",
    )
    captured = {}

    class FakeConnectionContext:
        def __enter__(self):
            return "sqlalchemy-connection"

        def __exit__(self, exc_type, exc, traceback):
            return False

    class FakeEngine:
        disposed = False

        def connect(self):
            return FakeConnectionContext()

        def dispose(self):
            self.disposed = True

    engine = FakeEngine()

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return engine

    monkeypatch.setattr(analytics_source, "create_engine", fake_create_engine)

    with analytics_source._connection(target) as connection:
        assert connection == "sqlalchemy-connection"

    assert captured["url"].drivername == driver
    assert captured["url"].password == "special:/@password"
    assert captured["poolclass"] is NullPool
    assert captured["connect_args"]["connect_timeout"] == 10
    assert engine.disposed is True


def test_daily_refresh_publishes_both_audiences_and_queries(tmp_path):
    company = analytics_company(tmp_path)
    store = AnalyticsSnapshotStore(tmp_path / "snapshots.sqlite3")
    result = AnalyticsRefreshService(
        FakeSource(analytics_data()),
        store,
    ).refresh(company)

    assert result["status"] == "complete"
    company_dashboard = store.dashboard("gainr", internal=False)
    internal_dashboard = store.dashboard("gainr", internal=True)
    assert set(company_dashboard) >= {
        "search_intelligence",
        "deep_analytics",
        "market_intelligence",
    }
    assert "api_performance" not in company_dashboard
    assert tuple(company_dashboard["search_intelligence"]) == (COMPANY_SEARCH_METRICS)
    assert tuple(company_dashboard["deep_analytics"]) == (COMPANY_DEEP_METRICS)
    assert tuple(company_dashboard["market_intelligence"]) == (COMPANY_MARKET_METRICS)
    assert "api_performance" in internal_dashboard
    assert tuple(internal_dashboard["api_performance"]) == (INTERNAL_API_METRICS)
    assert internal_dashboard["metadata"]["modules"] == [
        "individual_queries",
        "api_performance",
    ]
    assert "search_intelligence" not in internal_dashboard
    assert "deep_analytics" not in internal_dashboard
    assert "market_intelligence" not in internal_dashboard
    assert company_dashboard["metadata"]["schema_version"] == "3.3"
    assert company_dashboard["business_overview"] == {
        "scope": "Latest completed company snapshot",
        "total_users": 2,
        "active_users": 2,
        "total_listings": 2,
        "active_listings": 2,
        "active_sellers": 2,
        "cities_with_active_supply": 2,
        "recorded_demand": 2,
        "fulfilled_demand": 1,
        "zero_result_demand": 1,
        "failed_requests": 0,
        "fulfillment_rate": 50.0,
    }
    assert (
        company_dashboard["metadata"]["metric_definitions"]["q94_catalog_demand"][
            "group"
        ]
        == "demand"
    )
    assert company_dashboard["search_intelligence"]["q94_catalog_demand"]["labels"] == [
        "Vehicles",
        "Electronics",
    ]

    company_queries = store.query_records(
        "gainr",
        internal=False,
        limit=1,
    )
    assert company_queries["has_more"] is True
    assert "api" not in company_queries["items"][0]
    assert "attempts" not in company_queries["items"][0]
    second_page = store.query_records(
        "gainr",
        internal=False,
        limit=1,
        cursor=company_queries["next_cursor"],
        include_facets=False,
    )
    assert "facets" not in second_page
    assert (
        second_page["items"][0]["request_id"]
        != (company_queries["items"][0]["request_id"])
    )

    result_sorted = store.query_records(
        "gainr",
        internal=False,
        limit=1,
        sort_by="results",
        sort_direction="desc",
    )
    result_sorted_second_page = store.query_records(
        "gainr",
        internal=False,
        limit=1,
        cursor=result_sorted["next_cursor"],
        sort_by="results",
        sort_direction="desc",
    )
    assert result_sorted["sorting"] == {
        "sort_by": "results",
        "sort_direction": "desc",
    }
    assert (
        result_sorted["items"][0]["search"]["result_count"]
        >= result_sorted_second_page["items"][0]["search"]["result_count"]
    )
    assert (
        result_sorted["items"][0]["request_id"]
        != result_sorted_second_page["items"][0]["request_id"]
    )
    with pytest.raises(ValueError, match="cursor does not match sorting"):
        store.query_records(
            "gainr",
            internal=False,
            limit=1,
            cursor=result_sorted["next_cursor"],
            sort_by="outcome",
            sort_direction="asc",
        )
    with pytest.raises(ValueError, match="sort field"):
        store.query_records(
            "gainr",
            internal=False,
            limit=10,
            sort_by="duration",
        )

    internal_queries = store.query_records(
        "gainr",
        internal=True,
        limit=10,
    )
    detailed = next(
        item for item in internal_queries["items"] if item["request_id"] == "req-1"
    )
    assert detailed["performance"] == {
        "server_duration_ms": 500.0,
        "total_server_duration_ms": 500.0,
        "measurement_scope": "server_search_processing",
        "timing_semantics": "stages_may_overlap_do_not_sum",
        "execution_path": "semantic",
        "cache": {"plan_hit": True, "result_hit": False},
        "stages_ms": {
            "total_server_ms": 500.0,
            "planning_ms": 200.0,
            "query_model_ms": None,
            "query_model_load_ms": None,
            "engine_total_ms": None,
            "result_cache_ms": None,
            "embedding_ms": None,
            "embedding_load_ms": None,
            "vector_search_ms": None,
            "bm25_search_ms": None,
            "retrieval_ms": 180.0,
            "parallel_retrieval_ms": None,
            "fusion_ms": None,
            "type_lookup_ms": None,
            "reranker_load_ms": None,
            "reranking_ms": 90.0,
            "related_tail_ms": None,
            "database_filter_ms": None,
            "eligibility_ms": None,
            "hydration_ms": None,
            "response_mapping_ms": None,
            "session_storage_ms": None,
            "usage_recording_ms": None,
            "recent_search_ms": None,
            "filter_diagnostics_ms": None,
        },
        "downstream_api_calls": 3,
        "attempt_count": 1,
        "successful_attempt_count": 1,
        "failed_attempt_count": 0,
    }
    assert detailed["token_usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "thought_tokens": 0,
        "total_tokens": 120,
        "tokens_per_result": 6.0,
    }
    assert detailed["api"]["duration_ms"] == 500.0
    assert detailed["attempts"][0]["duration_ms"] == 200
    zero_result = next(
        item for item in internal_queries["items"] if item["request_id"] == "req-2"
    )
    assert zero_result["diagnostics"]["code"] == ("no_results_for_applied_filters")
    assert zero_result["filters"]["city"] == "Chennai"
    semantic_queries = store.query_records(
        "gainr",
        internal=True,
        limit=10,
        execution_path="SEMANTIC",
    )
    assert semantic_queries["returned"] == 1
    assert semantic_queries["items"][0]["performance"]["execution_path"] == "semantic"
    city_queries = store.query_records(
        "gainr",
        internal=True,
        limit=10,
        city_id=301,
        diagnostic_code="no_results_for_applied_filters",
    )
    assert [item["request_id"] for item in city_queries["items"]] == ["req-2"]
    assert internal_queries["facets"]["cities"] == [{"id": 301, "label": "Chennai"}]
    assert "text_search" in internal_queries["facets"]["request_kinds"]
    assert "performance" not in company_queries["items"][0]
    assert "token_usage" not in company_queries["items"][0]


def test_refresh_normalizes_business_timestamps_from_tenant_timezone(tmp_path):
    company = replace(
        analytics_company(tmp_path),
        timezone="Asia/Kolkata",
    )
    data = analytics_data()
    data["search_history"].loc[0, "created_at"] = "2026-08-28 12:59:00"
    data["search_history"].loc[1, "created_at"] = "2026-08-28 12:58:00"
    data["api_usage"].loc[0, "created_at"] = "2026-08-28 12:59:00"
    data["api_usage"].loc[1, "created_at"] = "2026-08-28 12:58:00"
    data["ads"].loc[0, "created_at"] = "2026-08-28 18:30:00"
    data["ads"].loc[1, "created_at"] = "2026-08-28 18:00:00"
    data["users"].loc[:, "created_at"] = "2026-08-28 17:30:00"
    store = AnalyticsSnapshotStore(tmp_path / "snapshots.sqlite3")

    result = AnalyticsRefreshService(FakeSource(data), store).refresh(company)

    assert result["source_watermark"] == "2026-08-28T13:00:00+00:00"
    dashboard = store.dashboard("gainr", internal=False)
    assert dashboard["metadata"]["source_timezone"] == "Asia/Kolkata"
    assert dashboard["metadata"]["normalized_timezone"] == "UTC"
    queries = store.query_records("gainr", internal=True, limit=10)["items"]
    assert queries[0]["created_at"] == "2026-08-28T12:59:00+00:00"


def test_provider_fallback_metrics_are_separated_by_operation():
    data = analytics_data()
    data["api_usage"].loc[0, "attempts_json"] = (
        '[{"provider":"groq","model":"planner-a",'
        '"operation":"query_planning","status":"fallback"},'
        '{"provider":"google","model":"planner-b",'
        '"operation":"query_planning","status":"success"},'
        '{"provider":"voyage-2.5","model":"rerank-2.5",'
        '"operation":"reranking","status":"success"}]'
    )
    data["api_usage"].loc[1, "attempts_json"] = (
        '[{"provider":"groq","model":"planner-a",'
        '"operation":"query_planning","status":"success"},'
        '{"provider":"voyage-2.5","model":"rerank-2.5",'
        '"operation":"reranking","status":"fallback"},'
        '{"provider":"openrouter-nemotron","model":"nemotron",'
        '"operation":"reranking","status":"success"}]'
    )

    reports = process_part_b(data)
    fallback = reports["q32_reranking_fallback"]

    assert fallback == {
        "total_reranking_calls": 3,
        "total_reranking_requests": 2,
        "voyage_fallbacks": 1,
        "nemotron_rescues": 1,
        "fallback_rate": 50.0,
        "requests_with_fallback": 1,
        "any_provider_fallback_requests": 2,
        "query_planner_fallback_requests": 1,
        "reranker_fallback_requests": 1,
        "voyage_to_nemotron_rescue_requests": 1,
        "title": "Reranking Fallback Analysis",
        "chart_type": "stat",
    }
    assert reports["q29_provider_reliability"]["fallback_requests"] == {
        "any_provider": 2,
        "query_planner": 1,
        "reranker": 1,
    }


def test_dashboard_activity_cache_uses_compact_records_without_changing_metrics(
    tmp_path,
):
    company = analytics_company(tmp_path)
    store = AnalyticsSnapshotStore(tmp_path / "snapshots.sqlite3")
    AnalyticsRefreshService(FakeSource(analytics_data()), store).refresh(company)

    compact_company = store.dashboard_activity_records("gainr", internal=False)
    compact_internal = store.dashboard_activity_records("gainr", internal=True)
    full_company = store.query_records(
        "gainr",
        internal=False,
        limit=10,
        include_filtered_results=True,
    )["items"]
    full_internal = store.query_records("gainr", internal=True, limit=10)["items"]

    assert "query" not in compact_company[0]
    assert "flags" not in compact_company[0]
    assert "token_usage" not in compact_internal[0]
    assert "measurement_scope" not in compact_internal[0]["performance"]
    assert build_dashboard_overview(
        list(compact_company),
        internal=False,
        filters=DashboardFilters(),
        timezone_name="UTC",
    ) == build_dashboard_overview(
        full_company,
        internal=False,
        filters=DashboardFilters(),
        timezone_name="UTC",
    )
    assert build_dashboard_overview(
        list(compact_internal),
        internal=True,
        filters=DashboardFilters(),
        timezone_name="UTC",
    ) == build_dashboard_overview(
        full_internal,
        internal=True,
        filters=DashboardFilters(),
        timezone_name="UTC",
    )


def test_daily_refresh_supports_empty_search_telemetry(tmp_path):
    company = analytics_company(tmp_path)
    data = analytics_data()
    data["search_history"] = data["search_history"].iloc[0:0].copy()
    data["api_usage"] = data["api_usage"].iloc[0:0].copy()
    store = AnalyticsSnapshotStore(tmp_path / "snapshots.sqlite3")

    result = AnalyticsRefreshService(
        FakeSource(data),
        store,
    ).refresh(company)

    assert result["status"] == "complete"
    assert result["query_records"] == 0
    company_dashboard = store.dashboard("gainr", internal=False)
    internal_dashboard = store.dashboard("gainr", internal=True)
    assert (
        company_dashboard["search_intelligence"]["q7_zero_results"]["percentage"] == 0
    )
    assert internal_dashboard["api_performance"]["q23_latency_stats"]["avg"] == 0
    assert internal_dashboard["api_performance"]["q40_avg_api_calls"]["avg"] == 0
    assert (
        store.query_records(
            "gainr",
            internal=False,
            limit=10,
        )["items"]
        == []
    )


def test_query_explorer_shows_filtered_failures_by_default(tmp_path):
    store = AnalyticsSnapshotStore(tmp_path / "snapshots.sqlite3")
    run_id = store.begin_refresh("gainr")
    text_search = {
        "request_id": "text-search",
        "created_at": "2026-08-20T00:00:00+00:00",
        "query": "camera rental",
        "request_kind": "text_search",
        "outcome": "fulfilled",
        "categories": ["Camera"],
        "language": "English",
    }
    browse_success = {
        "request_id": "browse-success",
        "created_at": "2026-08-20T00:01:00+00:00",
        "query": "Filtered browse: Camera",
        "request_kind": "filtered_browse",
        "outcome": "zero_result",
        "categories": ["Camera"],
        "language": "Unknown",
    }
    browse_failure = {
        "request_id": "browse-failure",
        "created_at": "2026-08-20T00:02:00+00:00",
        "query": "Filtered browse: Camera",
        "request_kind": "filtered_browse",
        "outcome": "failure",
        "categories": ["Camera"],
        "language": "Unknown",
    }
    store.publish(
        run_id=run_id,
        company_id="gainr",
        generated_at="2026-08-20T00:03:00+00:00",
        source_watermark=None,
        source_rows={},
        company_dashboard={},
        internal_dashboard={},
        query_records=[
            (text_search, text_search),
            (browse_success, browse_success),
            (browse_failure, browse_failure),
        ],
    )

    for internal in (False, True):
        default_ids = {
            item["request_id"]
            for item in store.query_records("gainr", internal=internal, limit=10)[
                "items"
            ]
        }
        failure_ids = {
            item["request_id"]
            for item in store.query_records(
                "gainr",
                internal=internal,
                limit=10,
                outcome="failure",
            )["items"]
        }

        assert default_ids == {"text-search", "browse-failure"}
        assert failure_ids == {"browse-failure"}


def test_internal_query_marks_missing_operational_telemetry_as_unavailable():
    data = analytics_data()
    data["api_usage"] = data["api_usage"].iloc[0:0].copy()

    record = build_query_records(data)["queries"][0]

    assert record["outcome"] == "telemetry_missing"
    assert record["performance"]["total_server_duration_ms"] is None
    assert record["performance"]["cache"] == {
        "plan_hit": None,
        "result_hit": None,
    }
    assert all(value is None for value in record["performance"]["stages_ms"].values())


def test_filter_only_browse_is_labeled_and_excluded_from_text_search_metrics(
    tmp_path,
):
    data = analytics_data()
    data["search_history"] = pd.concat(
        [
            data["search_history"],
            pd.DataFrame(
                [
                    {
                        "id": 3,
                        "request_id": "req-browse",
                        "query_text": "",
                        "created_at": "2026-07-29 12:00:00",
                    },
                    {
                        "id": 4,
                        "request_id": "req-failure",
                        "query_text": "planner failed",
                        "created_at": "2026-07-29 13:00:00",
                    },
                ]
            ),
        ],
        ignore_index=True,
    )
    browse_usage = data["api_usage"].iloc[1].to_dict()
    browse_usage.update(
        {
            "id": 12,
            "request_id": "req-browse",
            "context_json": (
                '{"subcategory":"Camera","city":"Chennai","target_ad_type":"offer"}'
            ),
            "created_at": "2026-07-29 12:00:00",
        }
    )
    failure_usage = data["api_usage"].iloc[1].to_dict()
    failure_usage.update(
        {
            "id": 13,
            "request_id": "req-failure",
            "status": "failure",
            "result_count": 0,
            "total_results": 0,
            "created_at": "2026-07-29 13:00:00",
        }
    )
    data["api_usage"] = pd.concat(
        [data["api_usage"], pd.DataFrame([browse_usage, failure_usage])],
        ignore_index=True,
    )

    browse_record = next(
        record
        for record in build_query_records(data)["queries"]
        if record["request_id"] == "req-browse"
    )
    search_metrics = process_part_a(data)
    api_metrics = process_part_b(data)

    assert browse_record["request_kind"] == "filtered_browse"
    assert browse_record["query"] == "Filtered browse: Camera, Chennai"
    assert browse_record["normalized_query"] == ""
    assert browse_record["flags"]["is_browse"] is True
    assert "Camera" in browse_record["categories"]
    assert "Chennai" in browse_record["locations"]
    assert search_metrics["q7_zero_results"]["total_searches"] == 2
    assert search_metrics["q7_zero_results"]["total_text_requests"] == 3
    assert search_metrics["q7_zero_results"]["failed_text_requests"] == 1
    assert search_metrics["q7_zero_results"]["total_zero"] == 1
    assert sum(api_metrics["q21_success_rate"]["values"]) == 4
    assert api_metrics["q36_zero_result_rate"]["total"] == 2
    assert api_metrics["q36_zero_result_rate"]["total_text_requests"] == 3
    assert api_metrics["q36_zero_result_rate"]["failed_text_requests"] == 1
    assert api_metrics["q36_zero_result_rate"]["zero_count"] == 1
    path_outcome = next(
        row
        for row in search_metrics["q91_path_outcomes"]["rows"]
        if row["execution_path"] == "deterministic_filter"
    )
    assert path_outcome["requests"] == 2
    assert path_outcome["successful_requests"] == 1
    assert path_outcome["zero_result_rate"] == 100.0
    assert path_outcome["failure_rate"] == 50.0

    store = AnalyticsSnapshotStore(tmp_path / "snapshots.sqlite3")
    AnalyticsRefreshService(
        FakeSource(data),
        store,
    ).refresh(analytics_company(tmp_path))
    text_only = store.query_records("gainr", internal=False, limit=20)
    with_browse = store.query_records(
        "gainr",
        internal=False,
        limit=20,
        include_filtered_results=True,
    )

    assert "req-browse" not in {item["request_id"] for item in text_only["items"]}
    assert "req-browse" in {item["request_id"] for item in with_browse["items"]}


def test_refresh_applies_separate_company_and_internal_metric_profiles(
    tmp_path,
):
    base = analytics_company(tmp_path)
    company = CompanyAnalyticsConfig(
        company_id=base.company_id,
        endpoint_slug=base.endpoint_slug,
        api_key_envs=base.api_key_envs,
        database=base.database,
        telemetry_database=base.telemetry_database,
        datasets=base.datasets,
        config_path=base.config_path,
        adapter="gainr",
        dataset_specs=GAINR_ANALYTICS_CONTRACT.dataset_specs,
        company_metric_profile={
            "search_intelligence": (
                "q1_category_distribution",
                "q10_language",
            ),
            "market_intelligence": (),
        },
        internal_metric_profile={
            "api_performance": (
                "q21_success_rate",
                "q23_latency_stats",
            ),
        },
    )
    store = AnalyticsSnapshotStore(tmp_path / "snapshots.sqlite3")
    AnalyticsRefreshService(
        FakeSource(analytics_data()),
        store,
    ).refresh(company)

    external = store.dashboard("gainr", internal=False)
    internal = store.dashboard("gainr", internal=True)
    assert tuple(external["search_intelligence"]) == (
        "q1_category_distribution",
        "q10_language",
    )
    assert "market_intelligence" not in external
    assert "market_intelligence" not in external["metadata"]["modules"]
    assert external["metadata"]["metric_counts"]["market_intelligence"] == 0
    assert "search_intelligence" not in internal
    assert tuple(internal["api_performance"]) == (
        "q21_success_rate",
        "q23_latency_stats",
    )
    assert "api_performance" not in external


def test_tenant_metric_profile_rejects_internal_data_for_company(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("TEST_DB_HOST", "db.internal")
    monkeypatch.setenv("TEST_DB_NAME", "company")
    monkeypatch.setenv("TEST_DB_USER", "reader")
    monkeypatch.setenv("TEST_DB_PASSWORD", "secret")
    path = tmp_path / "unsafe-profile.yaml"
    path.write_text(
        """
company:
  id: testco
database:
  backend: mysql
  host_env: TEST_DB_HOST
  database_env: TEST_DB_NAME
  user_env: TEST_DB_USER
  password_env: TEST_DB_PASSWORD
analytics:
  enabled: true
  metrics:
    company:
      api_performance:
        - q21_success_rate
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported module"):
        load_company_analytics_config(path)


def test_failed_refresh_keeps_last_completed_snapshot(tmp_path):
    company = analytics_company(tmp_path)
    store = AnalyticsSnapshotStore(tmp_path / "snapshots.sqlite3")
    AnalyticsRefreshService(
        FakeSource(analytics_data()),
        store,
    ).refresh(company)
    generated_at = store.dashboard("gainr", internal=False)["metadata"]["generated_at"]

    with pytest.raises(RuntimeError):
        AnalyticsRefreshService(
            FakeSource(RuntimeError("source unavailable")),
            store,
        ).refresh(company)

    assert (
        store.dashboard("gainr", internal=False)["metadata"]["generated_at"]
        == generated_at
    )
    assert store.company_status("gainr")["latest_run"]["status"] == "failed"


def test_startup_reconciliation_closes_only_stale_running_refreshes(tmp_path):
    path = tmp_path / "snapshots.sqlite3"
    store = AnalyticsSnapshotStore(path)
    stale_run = store.begin_refresh("gainr")
    recent_run = store.begin_refresh("gainr")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE analytics_refresh_runs SET started_at = ? WHERE run_id = ?",
            ("2026-08-28T00:00:00+00:00", stale_run),
        )
        connection.execute(
            "UPDATE analytics_refresh_runs SET started_at = ? WHERE run_id = ?",
            ("2026-08-30T09:00:00+00:00", recent_run),
        )

    reconciled = store.reconcile_stale_refreshes(
        now=pd.Timestamp("2026-08-30T10:00:00Z").to_pydatetime()
    )

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = {
            row["run_id"]: row
            for row in connection.execute(
                "SELECT run_id, status, completed_at, error_type "
                "FROM analytics_refresh_runs"
            )
        }
    assert reconciled == 1
    assert rows[stale_run]["status"] == "interrupted"
    assert rows[stale_run]["completed_at"] == "2026-08-30T10:00:00+00:00"
    assert rows[stale_run]["error_type"] == "process_interrupted"
    assert rows[recent_run]["status"] == "running"


def test_analytics_readiness_requires_snapshot_store_and_auth_db(
    tmp_path,
    monkeypatch,
):
    company = analytics_company(tmp_path)
    registry = AnalyticsRegistry({"gainr": company})
    settings = AnalyticsSettings(
        host="127.0.0.1",
        port=8010,
        snapshot_db_path=tmp_path / "snapshots.sqlite3",
        tenant_config_dir=tmp_path,
        cors_origins=(),
        query_page_size=50,
        query_max_page_size=200,
        session_cookie_secure=False,
    )
    store = AnalyticsSnapshotStore(settings.snapshot_db_path)
    auth_store = AnalyticsAuthStore(settings.snapshot_db_path)
    AnalyticsRefreshService(FakeSource(analytics_data()), store).refresh(company)
    app = create_app(
        settings=settings,
        registry=registry,
        store=store,
        auth_store=auth_store,
    )

    with TestClient(app) as client:
        healthy = client.get("/api/v1/ready")
        monkeypatch.setattr(
            store,
            "readiness",
            lambda: {"ok": False, "error_type": "OperationalError"},
        )
        store_failed = client.get("/api/v1/ready")
        monkeypatch.setattr(store, "readiness", lambda: {"ok": True})
        monkeypatch.setattr(
            auth_store,
            "readiness",
            lambda: {"ok": False, "error_type": "OperationalError"},
        )
        auth_failed = client.get("/api/v1/ready")

    assert healthy.status_code == 200
    assert store_failed.status_code == 503
    assert auth_failed.status_code == 503
    assert set(healthy.json()) == set(store_failed.json()) == set(auth_failed.json())


def test_api_enforces_company_and_internal_field_boundaries(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        "GAINR_ANALYTICS_API_KEY",
        "gainr-analytics-secret",
    )
    monkeypatch.setenv("GAINR_API_KEY", "gainr-search-secret")
    company = analytics_company(tmp_path)
    other_company = analytics_company(tmp_path, company_id="acme")
    registry = AnalyticsRegistry({"gainr": company, "acme": other_company})
    settings = AnalyticsSettings(
        host="127.0.0.1",
        port=8010,
        snapshot_db_path=tmp_path / "snapshots.sqlite3",
        tenant_config_dir=tmp_path,
        cors_origins=(),
        query_page_size=50,
        query_max_page_size=200,
        session_cookie_secure=False,
    )
    store = AnalyticsSnapshotStore(settings.snapshot_db_path)
    auth_store = AnalyticsAuthStore(
        settings.snapshot_db_path,
        session_ttl_seconds=settings.session_ttl_seconds,
        max_login_attempts=settings.login_max_attempts,
        lock_seconds=settings.login_lock_seconds,
        password_min_length=settings.password_min_length,
    )
    auth_store.create_user(
        username="gainr-owner",
        password="company-password-2026",
        role=COMPANY_USER,
        company_id="gainr",
    )
    auth_store.create_user(
        username="analytics-admin",
        password="internal-password-2026",
        role=INTERNAL_ADMIN,
    )
    AnalyticsRefreshService(
        FakeSource(analytics_data()),
        store,
    ).refresh(company)
    app = create_app(
        settings=settings,
        registry=registry,
        store=store,
        auth_store=auth_store,
    )

    with TestClient(app) as client:
        missing = client.get("/api/v1/gainr/analytics/dashboard")
        wrong = client.get(
            "/api/v1/gainr/analytics/dashboard",
            headers={"X-API-Key": "wrong"},
        )
        search_product_key = client.get(
            "/api/v1/gainr/analytics/dashboard",
            headers={"X-API-Key": "gainr-search-secret"},
        )
        company_result = client.get(
            "/api/v1/gainr/analytics/dashboard",
            headers={"X-API-Key": "gainr-analytics-secret"},
        )
        company_queries = client.get(
            "/api/v1/gainr/analytics/queries?outcome=zero_result",
            headers={"X-API-Key": "gainr-analytics-secret"},
        )
        compact_page = client.get(
            "/api/v1/gainr/analytics/queries?include_facets=false",
            headers={"X-API-Key": "gainr-analytics-secret"},
        )
        facets = client.get(
            "/api/v1/gainr/analytics/query-facets",
            headers={"X-API-Key": "gainr-analytics-secret"},
        )
        facets_not_modified = client.get(
            "/api/v1/gainr/analytics/query-facets",
            headers={
                "X-API-Key": "gainr-analytics-secret",
                "If-None-Match": facets.headers["etag"],
            },
        )
    with TestClient(app) as company_client:
        login = company_client.post(
            "/api/v1/gainr/analytics/auth/login",
            json={
                "username": "gainr-owner",
                "password": "company-password-2026",
            },
        )
        session_company = company_client.get("/api/v1/gainr/analytics/dashboard")
        forbidden_other_company = company_client.get("/api/v1/acme/analytics/dashboard")
        forbidden_internal = company_client.get(
            "/api/v1/admin/analytics/gainr/queries",
        )
        me = company_client.get("/api/v1/analytics/company/auth/me")
        logout = company_client.post("/api/v1/analytics/company/auth/logout")
        after_logout = company_client.get("/api/v1/gainr/analytics/dashboard")

    with TestClient(app) as internal_client:
        internal_login = internal_client.post(
            "/api/v1/analytics/internal/auth/login",
            json={
                "username": "analytics-admin",
                "password": "internal-password-2026",
            },
        )
        companies = internal_client.get("/api/v1/admin/analytics/companies")
        internal = internal_client.get("/api/v1/admin/analytics/gainr/dashboard")
        overview = internal_client.get("/api/v1/admin/analytics/overview")
        internal_queries = internal_client.get("/api/v1/admin/analytics/gainr/queries")

    assert missing.status_code == 401
    assert wrong.status_code == 403
    assert search_product_key.status_code == 403
    assert company_result.status_code == 200
    assert company_result.headers["cache-control"] == "private, no-store"
    assert "api_performance" not in company_result.json()
    assert company_queries.json()["returned"] == 1
    assert company_queries.json()["items"][0]["outcome"] == "zero_result"
    assert "facets" in company_queries.json()
    assert "facets" not in compact_page.json()
    assert facets.status_code == 200
    assert facets.json()["snapshot_version"]
    assert facets.json()["facets"] == company_queries.json()["facets"]
    assert facets.headers["cache-control"] == ("private, max-age=300, must-revalidate")
    assert facets_not_modified.status_code == 304
    assert facets_not_modified.content == b""
    assert login.status_code == 200
    assert login.json()["user"]["company_id"] == "gainr"
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=lax" in login.headers["set-cookie"]
    assert session_company.status_code == 200
    assert forbidden_other_company.status_code == 403
    # Admin routes intentionally ignore the company cookie.
    assert forbidden_internal.status_code == 401
    assert me.json()["user"]["role"] == COMPANY_USER
    assert logout.status_code == 200
    assert after_logout.status_code == 401
    assert internal_login.status_code == 200
    assert companies.status_code == 200
    assert companies.json()["companies"][0]["company_id"] == "gainr"
    assert internal.status_code == 200
    assert "api_performance" in internal.json()
    assert overview.status_code == 404
    assert "attempts" in internal_queries.json()["items"][0]


def test_company_analytics_routes_apply_tenant_response_adapter(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        "GAINR_ANALYTICS_API_KEY",
        "gainr-analytics-secret",
    )
    company = analytics_company(tmp_path)
    registry = AnalyticsRegistry({"gainr": company})
    settings = AnalyticsSettings(
        host="127.0.0.1",
        port=8010,
        snapshot_db_path=tmp_path / "snapshots.sqlite3",
        tenant_config_dir=tmp_path,
        cors_origins=(),
        query_page_size=50,
        query_max_page_size=200,
        session_cookie_secure=False,
    )
    store = AnalyticsSnapshotStore(settings.snapshot_db_path)
    AnalyticsRefreshService(
        FakeSource(analytics_data()),
        store,
    ).refresh(company)
    app = create_app(
        settings=settings,
        registry=registry,
        store=store,
        analytics_adapter_factory=lambda active_company: WrappedAnalyticsAdapter(
            active_company.company_id
        ),
    )
    headers = {"X-API-Key": "gainr-analytics-secret"}

    with TestClient(app) as client:
        dashboard = client.get(
            "/api/v1/gainr/analytics/dashboard",
            headers=headers,
        )
        queries = client.get(
            "/api/v1/gainr/analytics/queries",
            headers=headers,
        )
        status = client.get(
            "/api/v1/gainr/analytics/status",
            headers=headers,
        )

    assert dashboard.status_code == 200
    assert dashboard.json()["contract"] == "gainr-dashboard"
    assert dashboard.json()["payload"]["metadata"]["company_id"] == "gainr"
    assert queries.status_code == 200
    assert queries.json()["contract"] == "gainr-queries"
    assert queries.json()["payload"]["returned"] == 2
    assert status.status_code == 200
    assert status.json()["contract"] == "gainr-status"
    assert status.json()["payload"]["company_id"] == "gainr"


def test_analytics_auth_hashes_passwords_locks_and_revokes_sessions(
    tmp_path,
):
    path = tmp_path / "auth.sqlite3"
    auth_store = AnalyticsAuthStore(
        path,
        session_ttl_seconds=3600,
        max_login_attempts=3,
        lock_seconds=60,
        password_min_length=15,
    )
    auth_store.create_user(
        username="Company.User",
        password="safe-company-password",
        role=COMPANY_USER,
        company_id="gainr",
    )

    with sqlite3.connect(path) as connection:
        password_hash = connection.execute(
            "SELECT password_hash FROM analytics_users"
        ).fetchone()[0]
    assert password_hash.startswith("scrypt$")
    assert "safe-company-password" not in password_hash

    authenticated = auth_store.authenticate(
        username="company.user",
        password="safe-company-password",
        remote_address="127.0.0.1",
    )
    assert authenticated is not None
    assert authenticated.principal.company_id == "gainr"
    assert auth_store.resolve_session(authenticated.token) is not None

    auth_store.revoke_session(authenticated.token)
    assert auth_store.resolve_session(authenticated.token) is None

    for _ in range(3):
        assert (
            auth_store.authenticate(
                username="company.user",
                password="wrong-password-value",
            )
            is None
        )
    assert (
        auth_store.authenticate(
            username="company.user",
            password="safe-company-password",
        )
        is None
    )


def test_company_usernames_and_authentication_are_tenant_scoped(tmp_path):
    auth_store = AnalyticsAuthStore(
        tmp_path / "scoped-auth.sqlite3",
        password_min_length=15,
    )
    auth_store.create_user(
        username="owner",
        password="gainr-owner-password",
        role=COMPANY_USER,
        company_id="gainr",
    )
    auth_store.create_user(
        username="owner",
        password="acme-owner-password",
        role=COMPANY_USER,
        company_id="acme",
    )

    gainr = auth_store.authenticate(
        username="owner",
        password="gainr-owner-password",
        required_role=COMPANY_USER,
        required_company_id="gainr",
    )
    wrong_company = auth_store.authenticate(
        username="owner",
        password="gainr-owner-password",
        required_role=COMPANY_USER,
        required_company_id="acme",
    )

    assert gainr is not None
    assert gainr.principal.company_id == "gainr"
    assert wrong_company is None
    assert (
        auth_store.authenticate(
            username="owner",
            password="gainr-owner-password",
            required_role=COMPANY_USER,
        )
        is None
    )
