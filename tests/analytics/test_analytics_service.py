from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from analytics_service.api import create_app
from analytics_service.auth import (
    COMPANY_USER,
    INTERNAL_ADMIN,
    AnalyticsAuthStore,
)
from analytics_service.config import (
    DEFAULT_TABLES,
    AnalyticsRegistry,
    AnalyticsSettings,
    CompanyAnalyticsConfig,
    DatabaseTarget,
    DatasetMapping,
    load_company_analytics_config,
)
from analytics_service.domain import process_part_a, process_part_b
from analytics_service.domain.search.records import build_query_records
from analytics_service.metrics import (
    COMPANY_DEEP_METRICS,
    COMPANY_MARKET_METRICS,
    COMPANY_SEARCH_METRICS,
    INTERNAL_API_METRICS,
)
from analytics_service.service import AnalyticsRefreshService
from analytics_service.source import SqlAnalyticsDataSource, _validate_frame
from analytics_service.source_schema import DATASET_SPECS
from analytics_service.store import AnalyticsSnapshotStore


def analytics_company(tmp_path: Path, company_id: str = "gainr"):
    database = DatabaseTarget(
        backend="mysql",
        host="db",
        port=3306,
        database="catalog",
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
            name: DatasetMapping(table=table) for name, table in DEFAULT_TABLES.items()
        },
        config_path=tmp_path / f"{company_id}.yaml",
    )


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


class FakeSource:
    def __init__(self, data):
        self.data = data

    def load(self, company):
        del company
        if isinstance(self.data, Exception):
            raise self.data
        return {name: frame.copy() for name, frame in self.data.items()}


class WrappedAnalyticsAdapter:
    def __init__(self, company_id: str):
        self.company_id = company_id

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
  adapter: default
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
    assert config.adapter == "default"
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
        DATASET_SPECS["ads"],
    )
    assert "`listing_id` AS `id`" in sql
    assert "`listing_title` AS `title`" in sql
    assert "FROM `listings`" in sql
    history_sql = SqlAnalyticsDataSource._select_sql(
        config.database,
        config.datasets["search_history"],
        DATASET_SPECS["search_history"],
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
        DATASET_SPECS["ads"],
    )

    assert normalized["rental_fee"].iloc[0] == 1000.5
    assert pd.isna(normalized["rental_fee"].iloc[1])
    assert pd.isna(normalized["rental_fee"].iloc[2])
    assert normalized["actual_view_count"].iloc[0] == 10


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
    assert company_dashboard["metadata"]["schema_version"] == "2.0"

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
    )
    assert (
        second_page["items"][0]["request_id"]
        != (company_queries["items"][0]["request_id"])
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
    semantic_queries = store.query_records(
        "gainr",
        internal=True,
        limit=10,
        execution_path="SEMANTIC",
    )
    assert semantic_queries["returned"] == 1
    assert semantic_queries["items"][0]["performance"]["execution_path"] == "semantic"
    assert "performance" not in company_queries["items"][0]
    assert "token_usage" not in company_queries["items"][0]


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


def test_filter_only_browse_is_labeled_and_excluded_from_text_search_metrics():
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
    with TestClient(app) as company_client:
        login = company_client.post(
            "/api/v1/analytics/auth/login",
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
        me = company_client.get("/api/v1/analytics/auth/me")
        logout = company_client.post("/api/v1/analytics/auth/logout")
        after_logout = company_client.get("/api/v1/gainr/analytics/dashboard")

    with TestClient(app) as internal_client:
        internal_login = internal_client.post(
            "/api/v1/analytics/auth/login",
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
    assert login.status_code == 200
    assert login.json()["user"]["company_id"] == "gainr"
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=strict" in login.headers["set-cookie"]
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
