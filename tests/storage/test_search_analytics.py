from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from storage.mysql import MySQLRuntimeConfig
from storage.search_analytics import (
    MySQLSearchAnalyticsStore,
    SearchAnalyticsEvent,
    SearchApiUsageEvent,
)
from storage.search_analytics_spool import (
    SQLiteSearchAnalyticsSpoolStore,
    deliver_search_analytics_spool,
    deserialize_search_analytics_event,
    search_analytics_spool_status,
    serialize_search_analytics_event,
)


def mysql_config():
    return MySQLRuntimeConfig(
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
    )


class FakeCursor:
    def __init__(self):
        self.lastrowid = 41
        self.execute_calls = []
        self.executemany_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.execute_calls.append((query, params))

    def executemany(self, query, params):
        self.executemany_calls.append((query, list(params)))


class FakeConnection:
    def __init__(self):
        self.open = True
        self.cursor_instance = FakeCursor()
        self.begins = 0
        self.commits = 0
        self.rollbacks = 0

    def ping(self, reconnect=False):
        assert reconnect is True

    def cursor(self):
        return self.cursor_instance

    def begin(self):
        self.begins += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.open = False


def test_search_analytics_aggregates_provider_calls_and_tokens():
    event = SearchAnalyticsEvent(
        company_id="gainr",
        query_text="family car",
        execution_path="semantic",
        duration_ms=1200,
        result_count=20,
        total_results=80,
        api_usage=(
            SearchApiUsageEvent(
                provider="groq",
                model="openai/gpt-oss-20b",
                operation="query_planning",
                input_tokens=100,
                output_tokens=20,
                total_tokens=120,
            ),
            SearchApiUsageEvent(
                provider="ollama",
                model="embeddinggemma",
                operation="embedding",
            ),
            SearchApiUsageEvent(
                provider="voyage",
                model="rerank-2.5",
                operation="reranking",
                input_tokens=500,
                total_tokens=500,
            ),
        ),
    )

    assert event.api_call_count == 3
    assert event.input_tokens == 600
    assert event.output_tokens == 20
    assert event.total_tokens == 620


def test_search_analytics_writer_inserts_parent_and_provider_rows(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(
        MySQLSearchAnalyticsStore,
        "_connect",
        lambda _self: connection,
    )
    store = MySQLSearchAnalyticsStore(
        mysql_config(),
        company_id="gainr",
        queue_capacity=2,
    )
    event = SearchAnalyticsEvent(
        request_id="a" * 32,
        company_id="gainr",
        user_id="user-7",
        query_text="  Family   Car  ",
        execution_path="semantic",
        route_reason="llm_required",
        duration_ms=1234.5,
        result_count=20,
        total_results=80,
        filters={"city_id": 456},
        created_at=datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc),
        api_usage=(
            SearchApiUsageEvent(
                provider="groq",
                model="openai/gpt-oss-20b",
                operation="query_planning",
                input_tokens=100,
                output_tokens=20,
                total_tokens=120,
            ),
        ),
    )

    assert store.submit(event) is True
    assert store.flush(timeout_seconds=1) is True
    status = store.status()
    store.close()

    assert status == {
        "submitted": 1,
        "written": 1,
        "failed": 0,
        "dropped": 0,
        "queued": 0,
    }
    assert connection.begins == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0
    parent_params = connection.cursor_instance.execute_calls[0][1]
    assert parent_params[0] == "a" * 32
    assert parent_params[2] == "user-7"
    assert parent_params[3] == "Family Car"
    assert parent_params[8] == '{"city_id":456}'
    usage_rows = connection.cursor_instance.executemany_calls[0][1]
    assert usage_rows[0][0] == 41
    assert usage_rows[0][2:5] == (
        "groq",
        "openai/gpt-oss-20b",
        "query_planning",
    )
    assert "ON DUPLICATE KEY UPDATE" in (
        connection.cursor_instance.execute_calls[0][0]
    )
    assert "ON DUPLICATE KEY UPDATE" in (
        connection.cursor_instance.executemany_calls[0][0]
    )


def spool_event(request_id="b" * 32, query="family bike"):
    return SearchAnalyticsEvent(
        request_id=request_id,
        company_id="gainr",
        user_id="user-7",
        query_text=query,
        execution_path="semantic",
        duration_ms=1234.5,
        result_count=20,
        total_results=80,
        filters={"city_id": 456},
        created_at=datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc),
        api_usage=(
            SearchApiUsageEvent(
                provider="groq",
                model="openai/gpt-oss-20b",
                operation="query_planning",
                input_tokens=100,
                output_tokens=20,
                total_tokens=120,
            ),
        ),
    )


def test_search_analytics_spool_round_trip_preserves_request_and_usage():
    original = spool_event()

    restored = deserialize_search_analytics_event(
        serialize_search_analytics_event(original)
    )

    assert restored == original
    assert restored.api_call_count == 1
    assert restored.total_tokens == 120


def test_daily_spool_persists_pending_event_off_request_path(tmp_path):
    path = tmp_path / "analytics.sqlite3"
    store = SQLiteSearchAnalyticsSpoolStore(
        path,
        company_id="gainr",
        queue_capacity=2,
    )

    assert store.submit(spool_event()) is True
    assert store.flush(timeout_seconds=1) is True
    status = store.status()
    store.close()

    assert status["mode"] == "daily_spool"
    assert status["spooled"] == 1
    assert status["failed"] == 0
    assert status["pending"] == 1
    assert status["spool_bytes"] > 0


def test_daily_spool_rejects_cross_tenant_event(tmp_path):
    store = SQLiteSearchAnalyticsSpoolStore(
        tmp_path / "analytics.sqlite3",
        company_id="gainr",
    )
    event = SearchAnalyticsEvent(
        company_id="another-company",
        query_text="private query",
        execution_path="semantic",
        duration_ms=1,
        result_count=0,
        total_results=0,
    )

    assert store.submit(event) is False
    store.close()
    assert store.status()["pending"] == 0


class FakeDestination:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def patch_spool_destination(monkeypatch, upload):
    monkeypatch.setattr(
        "storage.search_analytics_spool.require_pymysql",
        lambda: SimpleNamespace(
            cursors=SimpleNamespace(DictCursor=object),
        ),
    )
    monkeypatch.setattr(
        "storage.search_analytics_spool.mysql_connection",
        lambda **_kwargs: FakeDestination(),
    )
    monkeypatch.setattr(
        "storage.search_analytics_spool.write_search_analytics_events",
        upload,
    )


def test_daily_delivery_deletes_only_mysql_committed_snapshot(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "analytics.sqlite3"
    initial = SQLiteSearchAnalyticsSpoolStore(
        path,
        company_id="gainr",
    )
    initial.submit(spool_event())
    initial.close()
    uploaded = []

    def upload(_connection, events, **_kwargs):
        uploaded.extend(events)
        later = SQLiteSearchAnalyticsSpoolStore(
            path,
            company_id="gainr",
        )
        later.submit(spool_event("c" * 32, "later query"))
        later.close()
        return len(events)

    patch_spool_destination(monkeypatch, upload)
    result = deliver_search_analytics_spool(
        path,
        mysql_config(),
        company_id="gainr",
        search_history_table="semantic_search_history",
        api_usage_table="semantic_search_api_usage",
        batch_size=10,
    )

    assert [event.request_id for event in uploaded] == ["b" * 32]
    assert result["uploaded"] == 1
    assert result["deleted"] == 1
    assert result["pending"] == 1


def test_failed_daily_delivery_retains_local_rows(tmp_path, monkeypatch):
    path = tmp_path / "analytics.sqlite3"
    store = SQLiteSearchAnalyticsSpoolStore(path, company_id="gainr")
    store.submit(spool_event())
    store.close()

    def fail_upload(*_args, **_kwargs):
        raise RuntimeError("destination unavailable")

    patch_spool_destination(monkeypatch, fail_upload)
    with pytest.raises(RuntimeError, match="destination unavailable"):
        deliver_search_analytics_spool(
            path,
            mysql_config(),
            company_id="gainr",
            search_history_table="semantic_search_history",
            api_usage_table="semantic_search_api_usage",
        )

    assert search_analytics_spool_status(
        path,
        company_id="gainr",
    )["pending"] == 1
