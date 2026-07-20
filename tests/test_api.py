import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import api
from api import (
    ExpiredCursorError,
    ProductSearchService,
    SearchCapacityError,
    SearchRequest,
    SearchSessionStore,
    TenantServicePool,
    create_app,
)
from gainr_compat import GainrFilterResultRequest
from mysql_store import MySQLRuntimeConfig
from postgres_store import PostgresRuntimeConfig
from rate_limit import TenantRateLimiter
from tenant_config import (
    TenantCompatibilityConfig,
    TenantPayloadConfig,
    TenantProfile,
    TenantRateLimit,
    TenantRegistry,
    TenantStorageConfig,
)
from usage_store import MonthlyUsageStore


class FakeBM25Index:
    def count(self):
        return 12


class FakeEngine:
    def __init__(self):
        self.bm25_index = FakeBM25Index()
        self.calls = []
        self.ranker = None
        self.reranker_loads = 0

    def ensure_reranker(self):
        if self.ranker is not None:
            return 0.0
        self.reranker_loads += 1
        self.ranker = object()
        return 0.123

    def search(self, query, limit=None):
        self.calls.append((query, limit))
        return {
            "query_plan": {
                "semantic_query": query,
                "keyword_query": query,
                "target_ad_type": "offer",
                "sort_order": "price_asc",
            },
            "resolved_filters": {"categorical": {"city_name": "Chennai"}},
            "unresolved_filters": {},
            "seconds": 0.01,
            "vector_seconds": 0.02,
            "bm25_seconds": 0.003,
            "reranker_load_seconds": 0.0,
            "reranker_seconds": 0.04,
            "products": [
                {
                    "id": number,
                    "title": f"Product {number}",
                    "mobile": "private",
                }
                for number in range(1, 6)
            ],
        }


def tenant_profile(
    tmp_path: Path,
    company_id: str,
    *,
    public_fields=("id", "title"),
    field_mapping=None,
    requests_per_minute=60,
    burst=10,
    request_mapping=None,
    endpoint_slug="",
    compatibility=None,
):
    return TenantProfile(
        company_id=company_id,
        database=MySQLRuntimeConfig(
            host="localhost",
            port=3306,
            database=f"db_{company_id}",
            user=company_id,
            password="secret",
            search_table="search_ready",
            content_column="embedding_content",
            bm25_column="bm25_content",
            search_id_column="id",
            result_table="products",
            result_id_column="id",
        ),
        storage=TenantStorageConfig(
            bm25_path=tmp_path / company_id / "bm25.sqlite3",
            pgvector_database=PostgresRuntimeConfig(
                host="localhost",
                port=5432,
                database="vectors",
                user="vectors",
                password="secret",
            ),
            pgvector_table=f"{company_id}_vectors",
        ),
        payload=TenantPayloadConfig(
            public_fields=tuple(public_fields),
            field_mapping=field_mapping or {},
            filter_schema={"category": "keyword"},
            request_mapping=request_mapping or {},
        ),
        rate_limit=TenantRateLimit(
            requests_per_minute=requests_per_minute,
            burst=burst,
        ),
        planner_adapter="gainr",
        api_key_envs=(f"{company_id.upper()}_API_KEY",),
        config_path=tmp_path / f"{company_id}.yaml",
        endpoint_slug=endpoint_slug,
        compatibility=compatibility or TenantCompatibilityConfig(),
    )


def test_cursor_pages_one_stable_search_without_repeating_engine_work():
    engine = FakeEngine()
    service = ProductSearchService(engine, max_results=20)

    first = service.search(SearchRequest(query="camera", page_size=2))
    second = service.search(
        SearchRequest(cursor=first.pagination.next_cursor, page_size=2)
    )
    third = service.search(
        SearchRequest(cursor=second.pagination.next_cursor, page_size=2)
    )

    assert [item["id"] for item in first.items] == [1, 2]
    assert "mobile" not in first.items[0]
    assert [item["id"] for item in second.items] == [3, 4]
    assert [item["id"] for item in third.items] == [5]
    assert first.search_id == second.search_id == third.search_id
    assert first.pagination.total_results == 5
    assert third.pagination.has_more is False
    assert third.pagination.next_cursor is None
    assert engine.calls == [("camera", 20)]


def test_default_api_page_contains_twenty_products():
    assert SearchRequest(query="camera").page_size == 20


def test_ranked_results_fill_first_three_pages_before_related_tail():
    class TieredEngine(FakeEngine):
        def search(self, query, limit=None):
            result = super().search(query, limit)
            result["products"] = [
                {
                    "id": number,
                    "title": f"Product {number}",
                    "result_tier": "ranked" if number <= 60 else "related",
                }
                for number in range(1, 66)
            ]
            return result

    service = ProductSearchService(TieredEngine(), max_results=65)
    pages = []
    response = service.search(SearchRequest(query="bike", page_size=20))
    pages.append(response)
    while response.pagination.next_cursor:
        response = service.search(
            SearchRequest(
                cursor=response.pagination.next_cursor,
                page_size=20,
            )
        )
        pages.append(response)

    assert len(pages) == 4
    assert all(
        item["result_tier"] == "ranked"
        for page in pages[:3]
        for item in page.items
    )
    assert [item["id"] for item in pages[3].items] == [61, 62, 63, 64, 65]
    assert all(
        item["result_tier"] == "related"
        for item in pages[3].items
    )


def test_expired_cursor_requires_a_new_query():
    now = [100.0]
    sessions = SearchSessionStore(ttl_seconds=5, clock=lambda: now[0])
    service = ProductSearchService(FakeEngine(), sessions=sessions)
    first = service.search(SearchRequest(query="camera", page_size=2))

    now[0] = 106.0

    try:
        service.search(
            SearchRequest(cursor=first.pagination.next_cursor, page_size=2)
        )
    except ExpiredCursorError as exc:
        assert "expired" in str(exc)
    else:
        raise AssertionError("Expected the cursor to expire")


def test_http_contract_and_validation():
    service = ProductSearchService(FakeEngine(), max_results=20)
    with TestClient(create_app(service=service)) as client:
        ready = client.get("/api/v1/ready")
        cached_ready = client.get("/api/v1/ready")
        live = client.get("/api/v1/live")
        health = client.get("/api/v1/health")
        response = client.post(
            "/api/v1/search",
            json={"query": "camera in Chennai", "page_size": 2},
        )
        invalid = client.post(
            "/api/v1/search",
            json={"query": "camera", "cursor": "also-set"},
        )

    assert ready.status_code == 200
    assert ready.json()["cached"] is False
    assert cached_ready.status_code == 200
    assert cached_ready.json()["cached"] is True
    assert (
        cached_ready.json()["checked_at_utc"]
        == ready.json()["checked_at_utc"]
    )
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.json()["checks"]["legacy"]["components"]["bm25"] == {
        "ok": True,
        "indexed_products": 12,
    }
    assert health.status_code == 200
    assert health.json()["indexed_products"] == 12
    assert health.json()["reranker_loaded"] is True
    assert health.json()["reranker_load_ms"] == 123.0
    assert health.json()["redis_enabled"] is False
    assert health.json()["redis_connected"] is False
    assert health.json()["query_plan_cache_backend"] == "memory"
    assert health.json()["result_cache_enabled"] is False
    assert service.engine.reranker_loads == 1
    assert response.status_code == 200
    assert response.json()["pagination"]["returned"] == 2
    assert response.json()["pagination"]["has_more"] is True
    assert response.json()["interpreted_query"]["query_corrections"] == []
    assert response.json()["interpreted_query"]["sort_order"] == "price_asc"
    assert response.json()["interpreted_query"]["result_cache_hit"] is False
    assert invalid.status_code == 422


def test_readiness_fails_when_a_critical_index_is_empty():
    class EmptyCollection:
        indexed_products = 0

        def count(self):
            return self.indexed_products

    engine = FakeEngine()
    collection = EmptyCollection()
    engine.collection = collection
    service = ProductSearchService(engine, max_results=20)

    with TestClient(create_app(service=service)) as client:
        response = client.get("/api/v1/ready")
        collection.indexed_products = 12
        recovered = client.get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["legacy"]["components"]["pgvector"] == {
        "ok": False,
        "indexed_products": 0,
    }
    assert recovered.status_code == 200
    assert recovered.json()["cached"] is False


def test_repeated_query_result_cache_is_reported_by_api():
    class ResultCachingEngine(FakeEngine):
        def search(self, query, limit=None):
            result = super().search(query, limit)
            result["result_cache_hit"] = len(self.calls) > 1
            result["result_cache_seconds"] = 0.002
            return result

    service = ProductSearchService(ResultCachingEngine(), max_results=20)

    first = service.search(SearchRequest(query="camera", page_size=2))
    repeated = service.search(SearchRequest(query="camera", page_size=2))

    assert first.cached is False
    assert first.interpreted_query["result_cache_hit"] is False
    assert repeated.cached is True
    assert repeated.interpreted_query["result_cache_hit"] is True
    assert repeated.timings_ms["result_cache"] == 2.0


def test_concurrent_identical_queries_wait_for_first_search():
    class CoalescingEngine(FakeEngine):
        def __init__(self):
            super().__init__()
            self.lock = threading.Lock()
            self.first_entered = threading.Event()
            self.release_first = threading.Event()
            self.first_finished = threading.Event()

        def search(self, query, limit=None):
            with self.lock:
                call_number = len(self.calls) + 1
                self.calls.append((query, limit))
            if call_number == 1:
                self.first_entered.set()
                self.release_first.wait(timeout=2)
                self.first_finished.set()
                return self._result(query, result_cache_hit=False)
            assert self.first_finished.is_set()
            return self._result(query, result_cache_hit=True)

        @staticmethod
        def _result(query, *, result_cache_hit):
            return {
                "query_plan": {
                    "semantic_query": query,
                    "keyword_query": query,
                    "target_ad_type": "offer",
                },
                "resolved_filters": {"categorical": {}},
                "unresolved_filters": {},
                "seconds": 0.0,
                "vector_seconds": 0.0,
                "bm25_seconds": 0.0,
                "reranker_load_seconds": 0.0,
                "reranker_seconds": 0.0,
                "related_tail_seconds": 0.0,
                "result_cache_seconds": 0.001 if result_cache_hit else 0.0,
                "result_cache_hit": result_cache_hit,
                "products": [{"id": 1, "title": "Product 1"}],
            }

    engine = CoalescingEngine()
    service = ProductSearchService(engine, max_results=20)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            service.search,
            SearchRequest(query="camera in Chennai", page_size=2),
        )
        assert engine.first_entered.wait(timeout=1)
        second = executor.submit(
            service.search,
            SearchRequest(query="camera in Chennai", page_size=2),
        )
        assert len(engine.calls) == 1
        engine.release_first.set()
        futures = [first, second]
        responses = [future.result() for future in futures]

    assert len(engine.calls) == 2
    assert any(response.cached for response in responses)


def test_search_monitor_reports_safe_success_and_failure_summaries():
    service = ProductSearchService(FakeEngine(), max_results=20)

    service.search(SearchRequest(query="private customer query", page_size=2))

    status = service.monitor_status()
    assert status["active"] == 0
    assert status["started"] == 1
    assert status["completed"] == 1
    assert status["failed"] == 0
    assert status["recent"][0]["status"] == "success"
    assert status["recent"][0]["query_chars"] == len(
        "private customer query"
    )
    assert "private customer query" not in str(status)
    assert status["recent"][0]["timings_ms"]["vector_search"] == 20
    assert [
        item["step"] for item in status["recent"][0]["timeline"]
    ] == [
        "plan",
        "retrieve",
        "rerank",
        "related_tail",
        "database_map",
        "search",
    ]

    class FailingEngine(FakeEngine):
        def search(self, query, limit=None):
            raise ValueError("private failure details")

    failing_service = ProductSearchService(FailingEngine(), max_results=20)
    try:
        failing_service.search(SearchRequest(query="secret", page_size=2))
    except ValueError:
        pass
    else:
        raise AssertionError("Expected the fake search to fail")

    failed = failing_service.monitor_status()
    assert failed["active"] == 0
    assert failed["started"] == 1
    assert failed["completed"] == 0
    assert failed["failed"] == 1
    assert failed["recent"][0]["error_type"] == "ValueError"
    assert failed["recent"][0]["timeline"][0]["status"] == "failed"
    assert "private failure details" not in str(failed)


def test_api_keeps_wanted_status_two_and_excludes_soft_deleted_products():
    engine = FakeEngine()
    original_search = engine.search

    def search_with_hidden_products(query, limit=None):
        result = original_search(query, limit)
        result["products"] = [
            {
                "id": 1,
                "type": "1",
                "title": "Offer",
                "status": "1",
                "deleted_at": None,
            },
            {
                "id": 2,
                "type": "2",
                "title": "Wanted",
                "status": "2",
                "deleted_at": None,
            },
            {
                "id": 3,
                "type": "2",
                "title": "Deleted",
                "status": "2",
                "deleted_at": "2025-01-01",
            },
        ]
        return result

    engine.search = search_with_hidden_products
    service = ProductSearchService(engine)

    response = service.search(SearchRequest(query="camera"))

    assert response.items == [
        {"id": 1, "type": "1", "title": "Offer"},
        {"id": 2, "type": "2", "title": "Wanted"},
    ]


def test_api_key_routes_to_isolated_company_service_and_binds_cursor(tmp_path):
    alpha = tenant_profile(tmp_path, "alpha")
    beta = tenant_profile(
        tmp_path,
        "beta",
        public_fields=("id", "name"),
        field_mapping={"name": "title"},
    )
    registry = TenantRegistry(
        {"alpha": alpha, "beta": beta},
        api_keys={"alpha": ["alpha-key"], "beta": ["beta-key"]},
    )

    def engine_factory(profile, _cache, _shared_reranker):
        engine = FakeEngine()
        original_search = engine.search

        def company_search(query, limit=None):
            result = original_search(query, limit)
            result["products"] = [
                {
                    "id": number,
                    "title": f"{profile.company_id}-{number}",
                }
                for number in range(1, 4)
            ]
            return result

        engine.search = company_search
        return engine

    app = create_app(
        tenant_registry=registry,
        tenant_engine_factory=engine_factory,
        preload_models=False,
    )
    with TestClient(app) as client:
        missing = client.post("/api/v1/search", json={"query": "camera"})
        invalid = client.post(
            "/api/v1/search",
            headers={"X-API-Key": "wrong"},
            json={"query": "camera"},
        )
        alpha_result = client.post(
            "/api/v1/search",
            headers={"X-API-Key": "alpha-key"},
            json={"query": "camera", "page_size": 1},
        )
        beta_result = client.post(
            "/api/v1/search",
            headers={"X-API-Key": "beta-key"},
            json={"query": "camera", "page_size": 1},
        )
        cross_tenant_cursor = client.post(
            "/api/v1/search",
            headers={"X-API-Key": "beta-key"},
            json={
                "cursor": alpha_result.json()["pagination"]["next_cursor"],
                "page_size": 1,
            },
        )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert alpha_result.status_code == 200
    assert alpha_result.json()["company_id"] == "alpha"
    assert alpha_result.json()["items"] == [{"id": 1, "title": "alpha-1"}]
    assert beta_result.status_code == 200
    assert beta_result.json()["company_id"] == "beta"
    assert beta_result.json()["items"] == [{"id": 1, "name": "beta-1"}]
    assert cross_tenant_cursor.status_code == 400


def test_company_endpoint_routes_and_normalizes_company_payload(tmp_path):
    alpha = tenant_profile(tmp_path, "alpha")
    beta = tenant_profile(
        tmp_path,
        "beta",
        public_fields=("id", "name"),
        field_mapping={"name": "title"},
        request_mapping={
            "query": "search_text",
            "cursor": "continuation_token",
            "page_size": "limit",
        },
        endpoint_slug="beta-catalog",
    )
    registry = TenantRegistry(
        {"alpha": alpha, "beta": beta},
        api_keys={"alpha": ["alpha-key"], "beta": ["beta-key"]},
    )

    def engine_factory(profile, _cache, _shared_reranker):
        engine = FakeEngine()
        original_search = engine.search

        def company_search(query, limit=None):
            result = original_search(query, limit)
            result["products"] = [
                {"id": 1, "title": f"{profile.company_id}-1"}
            ]
            return result

        engine.search = company_search
        return engine

    app = create_app(
        tenant_registry=registry,
        tenant_engine_factory=engine_factory,
        preload_models=False,
    )
    with TestClient(app) as client:
        alpha_result = client.post(
            "/api/v1/alpha/search",
            headers={"X-API-Key": "alpha-key"},
            json={"query": "camera", "page_size": 1},
        )
        beta_result = client.post(
            "/api/v1/beta-catalog/search",
            headers={"X-API-Key": "beta-key"},
            json={"search_text": "camera", "limit": 1},
        )
        wrong_company = client.post(
            "/api/v1/beta-catalog/search",
            headers={"X-API-Key": "alpha-key"},
            json={"search_text": "camera"},
        )
        wrong_payload = client.post(
            "/api/v1/beta-catalog/search",
            headers={"X-API-Key": "beta-key"},
            json={"query": "camera"},
        )
        verified = client.get(
            "/api/v1/beta-catalog/auth/verify",
            headers={"X-API-Key": "beta-key"},
        )
        verify_mismatch = client.get(
            "/api/v1/beta-catalog/auth/verify",
            headers={"X-API-Key": "alpha-key"},
        )

    assert alpha_result.status_code == 200
    assert alpha_result.json()["items"] == [{"id": 1, "title": "alpha-1"}]
    assert beta_result.status_code == 200
    assert beta_result.json()["items"] == [{"id": 1, "name": "beta-1"}]
    assert wrong_company.status_code == 403
    assert wrong_payload.status_code == 422
    assert verified.json() == {
        "authorized": True,
        "company_id": "beta",
        "endpoint_slug": "beta-catalog",
    }
    assert verify_mismatch.status_code == 403


def test_different_company_engines_can_search_concurrently(tmp_path):
    alpha = tenant_profile(tmp_path, "alpha")
    beta = tenant_profile(tmp_path, "beta")
    registry = TenantRegistry(
        {"alpha": alpha, "beta": beta},
        api_keys={"alpha": ["alpha-key"], "beta": ["beta-key"]},
    )
    barrier = threading.Barrier(2)

    class ConcurrentEngine(FakeEngine):
        def __init__(self, company_id):
            super().__init__()
            self.company_id = company_id

        def search(self, query, limit=None):
            barrier.wait(timeout=2)
            result = super().search(query, limit)
            result["products"] = [
                {"id": 1, "title": f"{self.company_id}-1"}
            ]
            return result

    pool = TenantServicePool(
        registry,
        max_services=2,
        engine_factory=lambda profile, *_args: ConcurrentEngine(
            profile.company_id
        ),
    )
    alpha_service = pool.get("alpha")
    beta_service = pool.get("beta")
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            alpha_future = executor.submit(
                alpha_service.search,
                SearchRequest(query="camera"),
            )
            beta_future = executor.submit(
                beta_service.search,
                SearchRequest(query="camera"),
            )
            alpha_result = alpha_future.result(timeout=3)
            beta_result = beta_future.result(timeout=3)
    finally:
        pool.close()

    assert alpha_result.company_id == "alpha"
    assert alpha_result.items == [{"id": 1, "title": "alpha-1"}]
    assert beta_result.company_id == "beta"
    assert beta_result.items == [{"id": 1, "title": "beta-1"}]


def test_users_in_the_same_company_can_search_concurrently():
    barrier = threading.Barrier(2)

    class ConcurrentEngine(FakeEngine):
        def search(self, query, limit=None):
            barrier.wait(timeout=2)
            return super().search(query, limit)

    service = ProductSearchService(
        ConcurrentEngine(),
        company_id="alpha",
        max_concurrent_searches=2,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            service.search,
            SearchRequest(query="camera"),
        )
        second = executor.submit(
            service.search,
            SearchRequest(query="bike"),
        )
        first_result = first.result(timeout=3)
        second_result = second.result(timeout=3)

    assert first_result.company_id == "alpha"
    assert second_result.company_id == "alpha"


def test_search_capacity_wait_is_bounded_and_reported():
    class BlockingEngine(FakeEngine):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def search(self, query, limit=None):
            self.entered.set()
            self.release.wait(timeout=2)
            return super().search(query, limit)

    engine = BlockingEngine()
    service = ProductSearchService(
        engine,
        max_concurrent_searches=1,
        search_slot_timeout_seconds=0.01,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            service.search,
            SearchRequest(query="camera"),
        )
        assert engine.entered.wait(timeout=1)
        second = executor.submit(
            service.search,
            SearchRequest(query="bike"),
        )
        try:
            second.result(timeout=1)
        except SearchCapacityError:
            pass
        else:
            raise AssertionError("Expected a bounded capacity rejection")
        engine.release.set()
        first.result(timeout=1)

    monitor = service.monitor_status()
    assert monitor["rejected"] == 1
    assert monitor["failed"] == 1


def test_company_rate_limit_is_enforced_before_search(tmp_path):
    alpha = tenant_profile(
        tmp_path,
        "alpha",
        requests_per_minute=1,
        burst=1,
    )
    registry = TenantRegistry(
        {"alpha": alpha},
        api_keys={"alpha": ["alpha-key"]},
    )
    limiter = TenantRateLimiter(redis_cache=None, clock=lambda: 100.0)
    app = create_app(
        tenant_registry=registry,
        tenant_engine_factory=lambda *_args: FakeEngine(),
        rate_limiter=limiter,
        preload_models=False,
    )
    with TestClient(app) as client:
        first = client.post(
            "/api/v1/search",
            headers={"X-API-Key": "alpha-key"},
            json={"query": "camera"},
        )
        limited = client.post(
            "/api/v1/search",
            headers={"X-API-Key": "alpha-key"},
            json={"query": "camera"},
        )

    assert first.status_code == 200
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "1"


def test_company_usage_endpoint_returns_only_that_company_totals(tmp_path):
    alpha = tenant_profile(tmp_path, "alpha")
    beta = tenant_profile(tmp_path, "beta")
    registry = TenantRegistry(
        {"alpha": alpha, "beta": beta},
        api_keys={"alpha": ["alpha-key"], "beta": ["beta-key"]},
    )
    usage_store = MonthlyUsageStore(tmp_path / "usage.sqlite3")

    class UsageEngine(FakeEngine):
        def search(self, query, limit=None):
            result = super().search(query, limit)
            result["query_model_metrics"] = {
                "model": "gemini-test",
                "attempts": [
                    {
                        "model": "gemini-test",
                        "status": "success",
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "total_tokens": 120,
                    }
                ],
            }
            result["reranker_attempts"] = [
                {
                    "provider": "voyage",
                    "model": "rerank-2.5",
                    "status": "success",
                    "usage": {
                        "input_tokens": 500,
                        "output_tokens": 0,
                        "total_tokens": 500,
                    },
                }
            ]
            return result

    app = create_app(
        tenant_registry=registry,
        tenant_engine_factory=lambda *_args: UsageEngine(),
        preload_models=False,
        usage_store=usage_store,
    )
    try:
        with TestClient(app) as client:
            search = client.post(
                "/api/v1/alpha/search",
                headers={"X-API-Key": "alpha-key"},
                json={"query": "camera"},
            )
            alpha_usage = client.get(
                "/api/v1/alpha/usage",
                headers={"X-API-Key": "alpha-key"},
            )
            beta_usage = client.get(
                "/api/v1/beta/usage",
                headers={"X-API-Key": "beta-key"},
            )
            cross_company = client.get(
                "/api/v1/beta/usage",
                headers={"X-API-Key": "alpha-key"},
            )
    finally:
        usage_store.close()

    assert search.status_code == 200
    assert search.json()["usage"]["total_tokens"] == 620
    assert alpha_usage.status_code == 200
    assert alpha_usage.json()["total_tokens"] == 620
    assert alpha_usage.json()["searches"] == 1
    assert beta_usage.status_code == 200
    assert beta_usage.json()["total_tokens"] == 0
    assert cross_company.status_code == 403


def test_gainr_compatibility_routes_are_enabled_only_by_tenant_config(
    tmp_path,
):
    gainr = tenant_profile(
        tmp_path,
        "gainr",
        compatibility=TenantCompatibilityConfig(
            adapter="gainr_legacy",
        ),
    )
    alpha = tenant_profile(tmp_path, "alpha")
    registry = TenantRegistry(
        {"gainr": gainr, "alpha": alpha},
        api_keys={"gainr": ["gainr-key"], "alpha": ["alpha-key"]},
    )

    class FakeCompatibility:
        def search_suggestions(self, request):
            return {"status": True, "data": [{"value": request.term}]}

        def filter_data(self, request):
            return {"data": {"city_id": request.city_id}}

        def parse_filter_result(self, payload):
            return GainrFilterResultRequest.model_validate(payload)

        def filter_results(self, request, *, user_id=None):
            return {
                "status": True,
                "message": "",
                "data": [],
                "current_page": request.page,
                "last_page": 1,
                "user_id": user_id,
            }

        def recent_searches(self, user_id):
            return {
                "status": True,
                "data": [{"id": 1, "value": user_id, "is_prosper": 0}],
            }

    app = create_app(
        tenant_registry=registry,
        tenant_engine_factory=lambda *_args: FakeEngine(),
        compatibility_factory=lambda *_args: FakeCompatibility(),
        preload_models=False,
    )
    with TestClient(app) as client:
        suggestions = client.post(
            "/api/v1/gainr/search-suggestions",
            headers={"X-API-Key": "gainr-key"},
            json={"term": "bike"},
        )
        filter_data = client.post(
            "/api/v1/gainr/filter-data",
            headers={"X-API-Key": "gainr-key"},
            json={"city_id": 456},
        )
        results = client.post(
            "/api/v1/gainr/filter-result",
            headers={
                "X-API-Key": "gainr-key",
                "X-User-ID": "user-7",
            },
            json={"searchTerm": "bike", "filter": {}, "page": 2},
        )
        recent = client.get(
            "/api/v1/gainr/recent-search",
            headers={
                "X-API-Key": "gainr-key",
                "X-User-ID": "user-7",
            },
        )
        disabled = client.post(
            "/api/v1/alpha/search-suggestions",
            headers={"X-API-Key": "alpha-key"},
            json={"term": "bike"},
        )
        mismatched = client.post(
            "/api/v1/gainr/filter-data",
            headers={"X-API-Key": "alpha-key"},
            json={"city_id": 456},
        )

    assert suggestions.json()["data"] == [{"value": "bike"}]
    assert filter_data.json() == {"data": {"city_id": 456}}
    assert results.json()["current_page"] == 2
    assert results.json()["user_id"] == "user-7"
    assert recent.json()["data"][0]["value"] == "user-7"
    assert disabled.status_code == 404
    assert mismatched.status_code == 403


def test_admin_status_requires_separate_key_and_hides_queries(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(api, "API_ADMIN_KEY", "admin-key-with-at-least-24-chars")
    gainr = tenant_profile(tmp_path, "gainr")
    registry = TenantRegistry(
        {"gainr": gainr},
        api_keys={"gainr": ["customer-key"]},
    )
    app = create_app(
        tenant_registry=registry,
        tenant_engine_factory=lambda *_args: FakeEngine(),
        preload_models=False,
    )

    with TestClient(app) as client:
        global_missing = client.get("/api/v1/admin/status")
        missing = client.get("/api/v1/gainr/admin/status")
        customer_key = client.get(
            "/api/v1/gainr/admin/status",
            headers={"X-Admin-Key": "customer-key"},
        )
        client.post(
            "/api/v1/gainr/search",
            headers={"X-API-Key": "customer-key"},
            json={"query": "private customer query", "page_size": 2},
        )
        status = client.get(
            "/api/v1/gainr/admin/status",
            headers={
                "X-Admin-Key": "admin-key-with-at-least-24-chars",
            },
        )
        global_status = client.get(
            "/api/v1/admin/status",
            headers={
                "X-Admin-Key": "admin-key-with-at-least-24-chars",
            },
        )
        logs_missing = client.get("/api/v1/admin/logs")
        api.LOGGER.warning(
            "admin_log_test status=warning api_key=test-secret-value"
        )
        logs = client.get(
            "/api/v1/admin/logs?limit=5&level=WARNING",
            headers={
                "X-Admin-Key": "admin-key-with-at-least-24-chars",
            },
        )
        logs_cursor = logs.json()["next_after_id"]
        api.LOGGER.error("admin_log_test status=error")
        incremental_logs = client.get(
            f"/api/v1/admin/logs?level=WARNING&after_id={logs_cursor}",
            headers={
                "X-Admin-Key": "admin-key-with-at-least-24-chars",
            },
        )
        events = client.get(
            "/api/v1/gainr/admin/search-events?limit=5",
            headers={
                "X-Admin-Key": "admin-key-with-at-least-24-chars",
            },
        )
        failed_events = client.get(
            "/api/v1/gainr/admin/search-events?status=failed",
            headers={
                "X-Admin-Key": "admin-key-with-at-least-24-chars",
            },
        )

    assert global_missing.status_code == 401
    assert missing.status_code == 401
    assert customer_key.status_code == 401
    assert status.status_code == 200
    payload = status.json()
    assert payload["status"] == "ok"
    assert payload["company_id"] == "gainr"
    assert payload["process"]["cpu_count"] >= 1
    assert payload["health"]["indexed_products"] == 12
    assert payload["searches"]["completed"] == 1
    assert "private customer query" not in status.text
    assert global_status.status_code == 200
    global_payload = global_status.json()
    assert global_payload["configured_companies"] == 1
    assert global_payload["loaded_companies"] == 1
    assert global_payload["companies"][0]["company_id"] == "gainr"
    assert global_payload["companies"][0]["loaded"] is True
    assert "private customer query" not in global_status.text
    assert logs_missing.status_code == 401
    assert logs.status_code == 200
    assert logs.headers["cache-control"] == "no-store"
    logs_payload = logs.json()
    assert logs_payload["retained"] >= 1
    assert logs_payload["events"][-1]["level"] == "WARNING"
    assert "test-secret-value" not in logs.text
    assert "[REDACTED]" in logs.text
    assert incremental_logs.status_code == 200
    assert incremental_logs.json()["events"][-1]["level"] == "ERROR"
    assert incremental_logs.json()["events"][-1]["id"] > logs_cursor
    assert events.status_code == 200
    events_payload = events.json()
    assert events_payload["company_id"] == "gainr"
    assert events_payload["retained"] == 1
    assert events_payload["events"][0]["timeline"][-1]["step"] == "search"
    assert "private customer query" not in events.text
    assert failed_events.status_code == 200
    assert failed_events.json()["events"] == []
