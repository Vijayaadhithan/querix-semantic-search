from datetime import datetime, timezone

from storage.mysql import MySQLRuntimeConfig
from storage.search_analytics import (
    MySQLSearchAnalyticsStore,
    SearchAnalyticsEvent,
    SearchApiUsageEvent,
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
