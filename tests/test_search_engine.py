import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import search_engine
from bm25_index import PersistentBM25Index
from query_planner import (
    deterministic_filter_query_plan,
    extract_sort_order,
    normalize_transliterated_query,
    query_filter_value_index,
)
from search_engine import ProductSearchEngine


class FakeCollection:
    pass


class CountingQueryProvider:
    def __init__(self):
        self.calls = 0

    def structured_chat(self, *_args):
        self.calls += 1
        return json.dumps(
            {
                "semantic_query": "red bike",
                "keyword_query": "red bike",
                "target_ad_type": "offer",
                "filters": {},
            }
        )


class CapturingQueryProvider(CountingQueryProvider):
    def __init__(self):
        super().__init__()
        self.system_prompt = ""
        self.user_prompt = ""

    def structured_chat(
        self,
        _model,
        system_prompt,
        user_prompt,
        _schema,
        _temperature,
    ):
        self.calls += 1
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return json.dumps(
            {
                "semantic_query": "portable recording equipment",
                "keyword_query": "camera recorder",
                "target_ad_type": "offer",
                "filters": {},
            }
        )


class DictSharedCache:
    def __init__(self):
        self.connected = True
        self.values = {}

    def get_json(self, namespace, key):
        return self.values.get((namespace, key))

    def set_json(self, namespace, key, value, _ttl_seconds):
        self.values[(namespace, key)] = value
        return True


def product_row(doc_id, **metadata):
    return {
        "doc_id": doc_id,
        "product_id": doc_id,
        "content": doc_id,
        **metadata,
    }


def build_index(path):
    index = PersistentBM25Index(path)
    index.upsert(
        [
            product_row(
                "bike-chennai",
                main_category_name="Automobiles",
                subcategory_name="Bike",
                state_name="Tamil Nadu",
                city_name="Chennai",
                rental_duration="Per Day",
                rental_fee=900,
            )
        ]
    )
    return index


def test_deterministic_filter_plan_accepts_simple_explicit_queries(tmp_path):
    index = build_index(tmp_path / "fast-plan.sqlite3")
    value_index = query_filter_value_index(index)

    bike = deterministic_filter_query_plan("bike", value_index)
    filtered = deterministic_filter_query_plan(
        "bikes in Chennai under 1000",
        value_index,
    )
    wanted = deterministic_filter_query_plan(
        "someone looking for bikes",
        value_index,
    )

    assert bike["execution_path"] == "deterministic_filter"
    assert bike["filters"]["subcategory"] == "Bike"
    assert filtered["filters"]["subcategory"] == "Bike"
    assert filtered["filters"]["city"] == "Chennai"
    assert filtered["filters"]["state"] == "Tamil Nadu"
    assert filtered["filters"]["max_rental_fee"] == 1000
    assert wanted["target_ad_type"] == "wanted"
    index.close()


def test_tenant_prompt_context_is_added_only_to_llm_planning(tmp_path):
    index = build_index(tmp_path / "tenant-prompt.sqlite3")
    provider = CapturingQueryProvider()
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        query_provider=provider,
        planner_prompt_context="This tenant rents professional event equipment.",
    )
    try:
        result = engine.plan("equipment for recording a distant wedding")
    finally:
        engine.close()
        index.close()

    assert result["query_plan"]["execution_path"] == "semantic"
    assert provider.calls == 1
    assert (
        "This tenant rents professional event equipment."
        in provider.system_prompt
    )


def test_transliterated_queries_receive_trusted_semantic_normalization(tmp_path):
    index = build_index(tmp_path / "transliterated-plan.sqlite3")
    provider = CapturingQueryProvider()
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        query_provider=provider,
    )
    try:
        result = engine.plan("veetu vela kaari in Chennai")
    finally:
        engine.close()
        index.close()

    assert result["query_plan"]["execution_path"] == "semantic"
    assert "romanized/transliterated" in provider.system_prompt
    assert "not a car" in provider.system_prompt
    assert "Original user query:\nveetu vela kaari in Chennai" in (
        provider.user_prompt
    )
    assert "house maid domestic worker in Chennai" in provider.user_prompt


def test_transliterated_query_normalization_is_narrow_and_spelling_tolerant():
    assert normalize_transliterated_query("veetu vela kaari") == (
        "house maid domestic worker"
    )
    assert normalize_transliterated_query("veettu velai kari Chennai") == (
        "house maid domestic worker Chennai"
    )
    assert normalize_transliterated_query("kaam wali bai") == (
        "house maid domestic worker"
    )
    assert normalize_transliterated_query("kalyanathuku camera venum") == (
        "for wedding camera venum"
    )
    assert normalize_transliterated_query("Ford car for rent") == (
        "Ford car for rent"
    )


def test_transliterated_phrase_tokens_do_not_become_fuzzy_locations(tmp_path):
    index = build_index(tmp_path / "transliterated-location.sqlite3")
    index.upsert(
        [
            product_row(
                "wali-locality",
                main_category_name="Other Services",
                subcategory_name="Designer",
                state_name="Rajasthan",
                city_name="Udaipur",
                locality_name="Wali",
            )
        ]
    )
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        query_provider=CapturingQueryProvider(),
    )
    try:
        result = engine.plan("kaam wali bai")
    finally:
        engine.close()
        index.close()

    assert result["resolved_filters"] == {"categorical": {}}


def test_translated_concepts_are_not_promoted_to_hard_category_filters(tmp_path):
    index = build_index(tmp_path / "translated-category.sqlite3")
    index.upsert(
        [
            product_row(
                "worker-chennai",
                main_category_name="Personal & Home Services",
                subcategory_name="Worker",
                state_name="Tamil Nadu",
                city_name="Chennai",
            )
        ]
    )
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        query_provider=CapturingQueryProvider(),
    )
    try:
        result = engine.plan("veettu velai kari in Chennai")
    finally:
        engine.close()
        index.close()

    categorical = result["resolved_filters"]["categorical"]
    assert "subcategory_name" not in categorical
    assert categorical["city_name"] == "Chennai"
    assert categorical["state_name"] == "Tamil Nadu"


def test_disabled_tenant_llm_planner_keeps_semantic_retrieval_available(tmp_path):
    index = build_index(tmp_path / "disabled-planner.sqlite3")
    provider = CountingQueryProvider()
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        query_provider=provider,
        planner_enabled=False,
    )
    try:
        result = engine.plan("equipment for recording a distant wedding")
    finally:
        engine.close()
        index.close()

    assert result["query_plan"]["execution_path"] == "semantic"
    assert result["query_plan"]["semantic_query"] == (
        "equipment for recording a distant wedding"
    )
    assert provider.calls == 0


def test_deterministic_filter_plan_corrects_category_and_city_typos(tmp_path):
    index = build_index(tmp_path / "fuzzy-fast-plan.sqlite3")
    value_index = query_filter_value_index(index)

    category = deterministic_filter_query_plan("bke", value_index)
    combined = deterministic_filter_query_plan(
        "bkes in chni under 1000",
        value_index,
    )

    assert category["filters"]["subcategory"] == "Bike"
    assert category["query_corrections"] == [
        {"field": "subcategory", "input": "bke", "value": "Bike"}
    ]
    assert combined["execution_path"] == "deterministic_filter"
    assert combined["filters"]["subcategory"] == "Bike"
    assert combined["filters"]["city"] == "Chennai"
    assert combined["filters"]["state"] == "Tamil Nadu"
    assert combined["filters"]["max_rental_fee"] == 1000
    assert combined["query_corrections"] == [
        {"field": "city", "input": "chni", "value": "Chennai"},
        {"field": "subcategory", "input": "bkes", "value": "Bike"},
    ]
    index.close()


def test_deterministic_filter_plan_accepts_reordered_bare_budget_query(
    tmp_path,
):
    index = build_index(tmp_path / "reordered-fast-plan.sqlite3")
    index.upsert(
        [
            product_row(
                "car-chennai",
                main_category_name="Automobiles",
                subcategory_name="Car",
                state_name="Tamil Nadu",
                city_name="Chennai",
                rental_fee=800,
            )
        ]
    )
    value_index = query_filter_value_index(index)

    compact = deterministic_filter_query_plan(
        "1000 rent car",
        value_index,
    )
    reordered = deterministic_filter_query_plan(
        "car rent 1000 in Chennai",
        value_index,
    )
    typo = deterministic_filter_query_plan(
        "1000 bke rent in chni",
        value_index,
    )

    assert compact["execution_path"] == "deterministic_filter"
    assert compact["filters"]["subcategory"] == "Car"
    assert compact["filters"]["max_rental_fee"] == 1000
    assert reordered["filters"]["subcategory"] == "Car"
    assert reordered["filters"]["city"] == "Chennai"
    assert reordered["filters"]["max_rental_fee"] == 1000
    assert typo["filters"]["subcategory"] == "Bike"
    assert typo["filters"]["city"] == "Chennai"
    assert typo["filters"]["max_rental_fee"] == 1000
    index.close()


def test_lowest_price_query_uses_sorted_filter_path_and_corrects_retail_typo(
    tmp_path,
):
    index = build_index(tmp_path / "sorted-fast-plan.sqlite3")
    index.upsert(
        [
            product_row(
                "car-coimbatore",
                main_category_name="Automobiles",
                subcategory_name="Car",
                state_name="Tamil Nadu",
                city_name="Coimbatore",
                rental_fee=250,
            )
        ]
    )
    value_index = query_filter_value_index(index)

    plan = deterministic_filter_query_plan(
        "lowest price car retail in coimbatore",
        value_index,
    )

    assert plan["execution_path"] == "deterministic_filter"
    assert plan["sort_order"] == "price_asc"
    assert plan["filters"]["subcategory"] == "Car"
    assert plan["filters"]["city"] == "Coimbatore"
    assert plan["query_corrections"] == [
        {"field": "intent", "input": "retail", "value": "rental"}
    ]
    index.close()


def test_price_sort_fast_path_is_category_agnostic(tmp_path):
    index = PersistentBM25Index(tmp_path / "generic-price-sort.sqlite3")
    index.upsert(
        [
            product_row(
                "bike-chennai",
                main_category_name="Automobiles",
                subcategory_name="Bike",
                state_name="Tamil Nadu",
                city_name="Chennai",
                rental_duration="Per Day",
                rental_fee=100,
            ),
            product_row(
                "camera-chennai",
                main_category_name="Audio & Video Equipments",
                subcategory_name="Camera",
                state_name="Tamil Nadu",
                city_name="Chennai",
                rental_duration="Per Day",
                rental_fee=250,
            ),
            product_row(
                "room-chennai",
                main_category_name="Accommodation & Spaces",
                subcategory_name="Room",
                state_name="Tamil Nadu",
                city_name="Chennai",
                rental_duration="Per Month",
                rental_fee=5000,
            ),
        ]
    )
    value_index = query_filter_value_index(index)
    cases = (
        ("cheapest daily bike in Chennai", "Bike", "Per Day"),
        ("lowest price camera per day in Chennai", "Camera", "Per Day"),
        ("most affordable room per month in Chennai", "Room", "Per Month"),
    )

    for query, subcategory, duration in cases:
        plan = deterministic_filter_query_plan(query, value_index)
        assert plan["execution_path"] == "deterministic_filter"
        assert plan["sort_order"] == "price_asc"
        assert plan["filters"]["subcategory"] == subcategory
        assert plan["filters"]["rental_duration"] == duration

    index.close()


def test_price_sort_wording_is_extracted_deterministically():
    ascending = (
        "cheapest car",
        "lowest priced bike",
        "low rental rate camera",
        "affordable car rental",
        "budget-friendly bike",
        "price low to high",
        "low to high rental fees",
        "sort by price ascending",
        "rental rate ascending",
    )
    descending = (
        "most expensive car",
        "highest price bike",
        "rental fee high to low",
        "high to low price",
        "order by rate desc",
        "price descending",
    )

    assert all(extract_sort_order(query) == "price_asc" for query in ascending)
    assert all(extract_sort_order(query) == "price_desc" for query in descending)
    assert extract_sort_order("car under 1000") is None


def test_browse_orders_the_complete_filtered_window_by_rental_fee(tmp_path):
    index = PersistentBM25Index(tmp_path / "price-browse.sqlite3")
    index.upsert(
        [
            product_row("car-5000", city_name="Coimbatore", rental_fee=5000),
            product_row("car-250", city_name="Coimbatore", rental_fee=250),
            product_row("car-null", city_name="Coimbatore", rental_fee=None),
            product_row("car-900", city_name="Coimbatore", rental_fee=900),
            product_row("car-zero", city_name="Coimbatore", rental_fee=0),
            product_row("car-one", city_name="Coimbatore", rental_fee=1),
        ]
    )
    filters = {"categorical": {"city_name": "Coimbatore"}}

    ascending = index.browse(filters, 10, sort_order="price_asc")
    descending = index.browse(filters, 10, sort_order="price_desc")

    assert [row["doc_id"] for row in ascending] == [
        "car-250",
        "car-900",
        "car-5000",
        "car-zero",
        "car-one",
        "car-null",
    ]
    assert [row["doc_id"] for row in descending] == [
        "car-5000",
        "car-900",
        "car-250",
        "car-one",
        "car-zero",
        "car-null",
    ]
    under_1000 = index.browse(
        {
            "categorical": {"city_name": "Coimbatore"},
            "max_rental_fee": 1000,
        },
        10,
        sort_order="price_asc",
    )
    assert [row["doc_id"] for row in under_1000] == [
        "car-250",
        "car-900",
    ]
    wanted_under_1000 = index.browse(
        {
            "categorical": {"city_name": "Coimbatore"},
            "max_rental_fee": 1000,
        },
        10,
        sort_order="price_asc",
        include_unpriced=True,
    )
    assert [row["doc_id"] for row in wanted_under_1000] == [
        "car-250",
        "car-900",
        "car-zero",
        "car-one",
        "car-null",
    ]
    index.close()


def test_bare_number_is_not_budget_when_it_looks_like_quantity_or_model_year(
    tmp_path,
):
    index = build_index(tmp_path / "guarded-budget-plan.sqlite3")
    index.upsert(
        [
            product_row(
                "car-chennai",
                main_category_name="Automobiles",
                subcategory_name="Car",
                state_name="Tamil Nadu",
                city_name="Chennai",
            )
        ]
    )
    value_index = query_filter_value_index(index)

    assert deterministic_filter_query_plan("2 rent car", value_index) is None
    assert deterministic_filter_query_plan("2020 rent car", value_index) is None
    assert deterministic_filter_query_plan("1000 cc car", value_index) is None
    index.close()


def test_ambiguous_category_typo_is_not_forced(tmp_path):
    index = build_index(tmp_path / "ambiguous-fuzzy-plan.sqlite3")
    index.upsert(
        [
            product_row(
                "bake-chennai",
                main_category_name="Services",
                subcategory_name="Bake",
                state_name="Tamil Nadu",
                city_name="Chennai",
            )
        ]
    )
    value_index = query_filter_value_index(index)

    assert deterministic_filter_query_plan("bke", value_index) is None
    index.close()


def test_deterministic_filter_plan_rejects_descriptive_queries(tmp_path):
    index = build_index(tmp_path / "semantic-plan.sqlite3")
    value_index = query_filter_value_index(index)

    assert deterministic_filter_query_plan(
        "red bike with ABS in Chennai",
        value_index,
    ) is None
    assert deterministic_filter_query_plan(
        "red bke with ABS in chni",
        value_index,
    ) is None
    assert deterministic_filter_query_plan(
        "vehicle for recreational driving on rough terrain",
        value_index,
    ) is None
    assert deterministic_filter_query_plan(
        "wanted bike",
        value_index,
    ) is None
    index.close()


def test_normalized_query_plan_cache_skips_repeated_planner_work(
    tmp_path,
    monkeypatch,
):
    index = build_index(tmp_path / "plan-cache.sqlite3")
    provider = CountingQueryProvider()
    deterministic_calls = []
    original_deterministic_plan = (
        search_engine.deterministic_filter_query_plan
    )

    def deterministic_plan(*args, **kwargs):
        deterministic_calls.append(True)
        return original_deterministic_plan(*args, **kwargs)

    monkeypatch.setattr(
        search_engine,
        "deterministic_filter_query_plan",
        deterministic_plan,
    )
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        query_provider=provider,
    )

    first = engine.plan("red bike")
    second = engine.plan("  RED   BIKE ")

    assert provider.calls == 1
    assert len(deterministic_calls) == 1
    assert first["plan_cache_hit"] is False
    assert second["plan_cache_hit"] is True
    assert second["query_model_metrics"] == {}
    index.close()


def test_shared_plan_cache_survives_engine_restart(tmp_path):
    index = build_index(tmp_path / "shared-plan-cache.sqlite3")
    cache = DictSharedCache()
    first_provider = CountingQueryProvider()
    first_engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        query_provider=first_provider,
        shared_plan_cache=cache,
    )

    first = first_engine.plan("red bike")

    second_provider = CountingQueryProvider()
    second_engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        query_provider=second_provider,
        shared_plan_cache=cache,
    )
    second = second_engine.plan(" RED   BIKE ")

    assert first["plan_cache_hit"] is False
    assert second["plan_cache_hit"] is True
    assert first_provider.calls == 1
    assert second_provider.calls == 0
    assert all(
        "red" not in key
        for _namespace, key in cache.values
    )
    assert second_engine.plan_cache_health() == {
        "redis_enabled": True,
        "redis_connected": True,
        "query_plan_cache_backend": "redis+memory",
        "result_cache_enabled": True,
        "result_cache_ttl_seconds": 300,
    }
    index.close()


def test_result_id_cache_skips_repeated_search_and_invalidates_on_index_change(
    tmp_path,
    monkeypatch,
):
    index = build_index(tmp_path / "result-cache.sqlite3")
    cache = DictSharedCache()
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        shared_plan_cache=cache,
    )
    browse_calls = []

    def browse(*_args, **_kwargs):
        browse_calls.append(True)
        return [101, 102]

    monkeypatch.setattr(search_engine, "related_tail_product_ids", browse)
    monkeypatch.setattr(
        search_engine,
        "fetch_products_by_ids",
        lambda ids: [{"id": product_id} for product_id in ids],
    )

    first = engine.search("bike", limit=20)
    restarted_engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        shared_plan_cache=cache,
    )
    second = restarted_engine.search("  BIKE ", limit=20)

    assert first["result_cache_hit"] is False
    assert second["result_cache_hit"] is True
    assert len(browse_calls) == 1
    assert [str(product["id"]) for product in second["products"]] == [
        "101",
        "102",
    ]
    assert all(
        product["result_tier"] == "filtered"
        for product in second["products"]
    )
    assert any(
        namespace == "search_result"
        for namespace, _key in cache.values
    )

    index.upsert(
        [
            product_row(
                "second-bike",
                main_category_name="Automobiles",
                subcategory_name="Bike",
            )
        ]
    )
    third = restarted_engine.search("bike", limit=20)

    assert third["result_cache_hit"] is False
    assert len(browse_calls) == 2
    index.close()


def test_simple_query_skips_model_retrieval_and_reranking(tmp_path, monkeypatch):
    index = build_index(tmp_path / "fast-search.sqlite3")

    class FailingProvider:
        def structured_chat(self, *_args):
            raise AssertionError("The hosted query model must not be called.")

    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        query_provider=FailingProvider(),
    )
    monkeypatch.setattr(
        search_engine,
        "related_tail_product_ids",
        lambda *_args, **_kwargs: [101, 102],
    )
    monkeypatch.setattr(
        search_engine,
        "fetch_products_by_ids",
        lambda ids: [{"id": product_id} for product_id in ids],
    )
    monkeypatch.setattr(
        engine,
        "retrieve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Semantic retrieval must not run.")
        ),
    )
    monkeypatch.setattr(
        engine,
        "rank",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("The reranker must not run.")
        ),
    )

    result = engine.search("bike", limit=20)

    assert result["query_plan"]["execution_path"] == "deterministic_filter"
    assert result["vector_results"] == []
    assert result["reranked"] == []
    assert [product["result_tier"] for product in result["products"]] == [
        "filtered",
        "filtered",
    ]
    index.close()


def test_semantic_vector_and_bm25_retrieval_start_in_parallel(
    tmp_path,
    monkeypatch,
):
    index = build_index(tmp_path / "parallel-retrieval.sqlite3")
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
    )
    barrier = threading.Barrier(2, timeout=2)

    def vector(*_args, **_kwargs):
        barrier.wait()
        return []

    def bm25(*_args, **_kwargs):
        barrier.wait()
        return []

    monkeypatch.setattr(search_engine, "vector_search", vector)
    monkeypatch.setattr(search_engine, "bm25_search", bm25)
    monkeypatch.setattr(
        search_engine,
        "filter_candidates_by_ad_type",
        lambda candidates, *_args, **_kwargs: candidates,
    )

    result = engine.retrieve(
        {
            "semantic_query": "red bike",
            "keyword_query": "red bike",
            "target_ad_type": "offer",
            "inferred_categories": {},
        },
        {"categorical": {}},
    )

    assert result["vector_results"] == []
    assert result["bm25_results"] == []
    index.close()


def test_vector_failure_fails_open_to_standalone_bm25(tmp_path, monkeypatch):
    index = build_index(tmp_path / "bm25-fail-open.sqlite3")
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        company_id="gainr",
    )
    monkeypatch.setattr(
        search_engine,
        "vector_search",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("embedding unavailable")
        ),
    )
    monkeypatch.setattr(
        search_engine,
        "filter_candidates_by_ad_type",
        lambda candidates, *_args, **_kwargs: candidates,
    )

    result = engine.retrieve(
        {
            "semantic_query": "bike",
            "keyword_query": "bike",
            "target_ad_type": "offer",
            "inferred_categories": {},
        },
        {"categorical": {"city_name": "Chennai"}},
    )

    assert result["vector_results"] == []
    assert result["bm25_results"][0]["text"] == "bike-chennai"
    assert result["bm25_results"][0]["metadata"]["id"] == "bike-chennai"
    assert result["retrieval_degraded"] is True
    assert result["degraded_stages"] == ["vector"]
    index.close()


def test_unchanged_bm25_upsert_does_not_advance_revision(tmp_path):
    index = PersistentBM25Index(tmp_path / "stable-revision.sqlite3")
    row = product_row(
        "bike-chennai",
        city_name="Chennai",
        subcategory_name="Bike",
    )
    index.upsert([row])
    revision = index.revision()

    index.upsert([row])

    assert index.revision() == revision
    index.close()


def test_small_rerank_window_preserves_deep_gainr_recall(
    tmp_path,
    monkeypatch,
):
    index = build_index(tmp_path / "gainr-deep-recall.sqlite3")
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        company_id="gainr",
    )
    captured = {}

    def vector(_query, _collection, top_k, **kwargs):
        captured["vector_top_k"] = top_k
        captured["vector_candidate_k"] = kwargs["candidate_k"]
        results = [
            {
                "id": f"safety-{position}",
                "text": "Safety officer for daily hire",
                "metadata": {
                    "main_category_name": "Services",
                    "subcategory_name": "Safety Officer",
                },
                "score": 1.0,
                "source": "vector",
            }
            for position in range(top_k - 1)
        ]
        results.append(
            {
                "id": "driver",
                "text": "Light Motor Vehicle Acting Driver for Daily Hire",
                "metadata": {
                    "main_category_name": "Automobiles",
                    "subcategory_name": "Acting Driver",
                },
                "score": 0.1,
                "source": "vector",
            }
        )
        return results

    def bm25(_query, _index, _collection, _filters, top_k, **_kwargs):
        captured["bm25_top_k"] = top_k
        return []

    monkeypatch.setattr(search_engine, "vector_search", vector)
    monkeypatch.setattr(search_engine, "bm25_search", bm25)
    monkeypatch.setattr(
        search_engine,
        "filter_candidates_by_ad_type",
        lambda candidates, *_args, **_kwargs: candidates,
    )

    result = engine.retrieve(
        {
            "semantic_query": (
                "vehicle for long distance with comfort and safety"
            ),
            "keyword_query": "vehicle long distance comfort safety",
            "target_ad_type": "offer",
            "inferred_categories": {},
        },
        {"categorical": {"rental_duration": "Per Day"}},
        candidate_limit=20,
        strict_candidate_limit=True,
    )

    assert captured["vector_top_k"] == 80
    assert captured["bm25_top_k"] == 80
    assert captured["vector_candidate_k"] >= 100
    assert len(result["candidates"]) == 20
    assert result["candidates"][0]["id"] == "driver"
    assert len(result["hybrid_tail_candidates"]) == 60
    index.close()


def test_gainr_vehicle_travel_intent_demotes_vehicle_services():
    query_plan = {
        "semantic_query": "vehicle for long distance with comfort and safety",
        "keyword_query": "vehicle long distance comfort safety",
    }
    candidates = [
        {
            "id": "detailer",
            "text": "Car Detailer for Daily Hire",
            "metadata": {
                "main_category_name": "Services",
                "subcategory_name": "Car Detailer",
            },
            "fusion_score": 0.05,
        },
        {
            "id": "driver",
            "text": "Light Motor Vehicle Acting Driver for Daily Hire",
            "metadata": {
                "main_category_name": "Automobiles",
                "subcategory_name": "Acting Driver",
            },
            "fusion_score": 0.03,
        },
        {
            "id": "safety-auditor",
            "text": "Food Safety Auditor for Daily Hire",
            "metadata": {
                "main_category_name": "Services",
                "subcategory_name": "Food Safety Auditor",
            },
            "fusion_score": 0.07,
        },
    ]

    adjusted = search_engine._apply_gainr_domain_intent_adjustments(
        query_plan,
        candidates,
        "gainr",
    )

    assert [candidate["id"] for candidate in adjusted] == [
        "driver",
        "safety-auditor",
        "detailer",
    ]


def test_gainr_vehicle_phrases_require_word_boundaries():
    assert search_engine._contains_phrase("car for rent", {"car"})
    assert not search_engine._contains_phrase("carpet cleaning", {"car"})
    assert not search_engine._contains_phrase("advanced service", {"van"})


def test_gainr_vehicle_service_query_is_not_demoted():
    query_plan = {
        "semantic_query": "car detailer in Mumbai",
        "keyword_query": "car detailer Mumbai",
    }
    candidates = [
        {
            "id": "detailer",
            "text": "Car Detailer for Daily Hire",
            "metadata": {"main_category_name": "Services"},
            "fusion_score": 0.05,
        },
        {
            "id": "driver",
            "text": "Light Motor Vehicle Acting Driver for Daily Hire",
            "metadata": {"main_category_name": "Automobiles"},
            "fusion_score": 0.03,
        },
    ]

    adjusted = search_engine._apply_gainr_domain_intent_adjustments(
        query_plan,
        candidates,
        "gainr",
    )

    assert adjusted == candidates


def test_gainr_vehicle_intent_context_is_passed_to_reranker(tmp_path):
    index = build_index(tmp_path / "gainr-rerank-context.sqlite3")

    class CapturingRanker:
        model_label = "test-reranker"
        last_provider = "local"
        last_attempts = []

        def __init__(self):
            self.queries = []

        def compute_score(self, pairs, **_kwargs):
            self.queries.extend(pair[0] for pair in pairs)
            return [1.0 for _pair in pairs]

    ranker = CapturingRanker()
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        ranker=ranker,
        company_id="gainr",
    )
    query_plan = {
        "semantic_query": "vehicle for long distance with comfort and safety",
        "keyword_query": "vehicle long distance comfort safety",
        "inferred_categories": {},
    }
    candidates = [
        {
            "id": "driver",
            "text": "Light Motor Vehicle Acting Driver for Daily Hire",
            "metadata": {"content_title": "Driver"},
        }
    ]

    engine.rank(
        "vehicle for long distance with comfort and safety",
        candidates,
        query_plan=query_plan,
        top_k=1,
    )

    assert "Gainr domain intent" in ranker.queries[0]
    assert "generic safety officers" in ranker.queries[0]
    assert "Demote services about vehicles" in ranker.queries[0]
    index.close()


def test_tenant_reranker_policy_prunes_weak_semantic_results(tmp_path):
    index = build_index(tmp_path / "relevance-floor.sqlite3")

    class ScoredRanker:
        model_label = "test-reranker"
        last_provider = "voyage-2.5"
        last_attempts = []

        def compute_score(self, _pairs, **_kwargs):
            return [1.0, 0.29, 0.06]

    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        ranker=ScoredRanker(),
        reranker_relative_score_floor=0.30,
        reranker_min_score_by_provider={"voyage-2.5": 0.05},
    )
    candidates = [
        {
            "id": str(number),
            "text": f"candidate {number}",
            "metadata": {"content_title": f"Candidate {number}"},
        }
        for number in range(3)
    ]

    result = engine.rank("test query", candidates, top_k=3)

    assert [item["id"] for item in result["results"]] == ["0"]
    index.close()


def test_reranker_failure_falls_back_to_fusion_order(tmp_path):
    index = build_index(tmp_path / "fusion-fallback.sqlite3")

    class FailingRanker:
        model_label = "hosted-rerankers"
        last_provider = ""
        last_attempts = [
            {
                "provider": "voyage-2.5",
                "status": "fallback",
                "reason": "ReadTimeout",
            }
        ]

        def compute_score(self, _pairs, **_kwargs):
            raise RuntimeError(
                "All reranker providers failed: voyage-2.5=ReadTimeout"
            )

    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        ranker=FailingRanker(),
    )
    candidates = [
        {
            "id": "first",
            "text": "best hybrid match",
            "metadata": {"content_title": "Best"},
            "fusion_score": 0.42,
        },
        {
            "id": "second",
            "text": "second hybrid match",
            "metadata": {"content_title": "Second"},
            "fusion_score": 0.21,
        },
    ]

    result = engine.rank("test query", candidates, top_k=2)

    assert result["provider"] == "fusion_fallback"
    assert result["degraded"] is True
    assert result["error_type"] == "RuntimeError"
    assert result["attempts"] == FailingRanker.last_attempts
    assert [item["id"] for item in result["results"]] == [
        "first",
        "second",
    ]
    assert [item["score"] for item in result["results"]] == [0.42, 0.21]
    index.close()


def test_tenant_can_disable_unscored_semantic_tail(tmp_path, monkeypatch):
    index = build_index(tmp_path / "no-related-tail.sqlite3")
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        semantic_related_tail_enabled=False,
    )
    candidate = {
        "id": "doc-1",
        "text": "relevant bike",
        "metadata": {
            "source_type": "mysql",
            "source_table": engine.search_table,
            engine.search_id_column: 101,
        },
    }
    planned = {
        "query_plan": {
            "semantic_query": "red bike",
            "keyword_query": "red bike",
            "target_ad_type": "offer",
            "inferred_categories": {},
            "execution_path": "semantic",
            "sort_order": None,
        },
        "resolved_filters": {"categorical": {}},
        "unresolved_filters": {},
    }
    monkeypatch.setattr(
        engine,
        "retrieve",
        lambda *_args, **_kwargs: {
            "vector_results": [candidate],
            "bm25_results": [],
            "candidates": [candidate],
            "vector_seconds": 0.0,
            "bm25_seconds": 0.0,
            "embedding_model_metrics": {},
        },
    )
    monkeypatch.setattr(
        engine,
        "rank",
        lambda *_args, **_kwargs: {
            "results": [candidate],
            "load_seconds": 0.0,
            "seconds": 0.0,
            "provider": "test",
            "attempts": [],
        },
    )
    monkeypatch.setattr(
        search_engine,
        "related_tail_product_ids",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Semantic related tail must be disabled.")
        ),
    )
    monkeypatch.setattr(
        engine,
        "_fetch_products",
        lambda ids: [{"id": product_id} for product_id in ids],
    )

    result = engine.search(
        "red bike",
        limit=20,
        planned_result=planned,
    )

    assert result["primary_product_ids"] == [101]
    assert result["related_product_ids"] == []
    assert result["product_ids"] == [101]
    index.close()


def test_semantic_search_uses_hybrid_continuation_before_catalogue_tail(
    tmp_path,
    monkeypatch,
):
    index = build_index(tmp_path / "hybrid-before-catalogue.sqlite3")
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        semantic_related_tail_enabled=True,
    )

    def candidate(doc_id, product_id):
        return {
            "id": doc_id,
            "text": f"candidate {product_id}",
            "metadata": {
                "source_type": "mysql",
                "source_table": engine.search_table,
                engine.search_id_column: product_id,
            },
        }

    ranked_candidate = candidate("ranked-doc", 101)
    rejected_candidate = candidate("rejected-doc", 199)
    hybrid_candidates = [
        candidate("hybrid-doc-1", 102),
        candidate("hybrid-doc-2", 103),
    ]
    planned = {
        "query_plan": {
            "semantic_query": "comfortable wedding transport",
            "keyword_query": "wedding car driver",
            "target_ad_type": "offer",
            "inferred_categories": {},
            "execution_path": "semantic",
            "sort_order": None,
        },
        "resolved_filters": {
            "categorical": {"city_name": "Chennai"}
        },
        "unresolved_filters": {},
    }
    monkeypatch.setattr(
        engine,
        "retrieve",
        lambda *_args, **_kwargs: {
            "vector_results": [],
            "bm25_results": [],
            "candidates": [ranked_candidate, rejected_candidate],
            "hybrid_tail_candidates": hybrid_candidates,
            "vector_seconds": 0.0,
            "bm25_seconds": 0.0,
            "embedding_model_metrics": {},
        },
    )
    monkeypatch.setattr(
        engine,
        "rank",
        lambda *_args, **_kwargs: {
            "results": [ranked_candidate],
            "load_seconds": 0.0,
            "seconds": 0.0,
            "provider": "test",
            "attempts": [],
        },
    )
    captured = {}

    def catalogue_tail(*args, **kwargs):
        captured["limit"] = args[4]
        captured["exclude_doc_ids"] = kwargs["exclude_doc_ids"]
        captured["exclude_product_ids"] = kwargs["exclude_product_ids"]
        return [104, 105]

    monkeypatch.setattr(
        search_engine,
        "related_tail_product_ids",
        catalogue_tail,
    )
    monkeypatch.setattr(
        engine,
        "_fetch_products",
        lambda ids: [{"id": product_id} for product_id in ids],
    )

    result = engine.search(
        "comfortable wedding transport",
        limit=5,
        planned_result=planned,
        ranking_window=20,
    )

    assert result["primary_product_ids"] == [101]
    assert result["hybrid_product_ids"] == [102, 103]
    assert result["related_product_ids"] == [104, 105]
    assert result["product_ids"] == [101, 102, 103, 104, 105]
    assert captured["limit"] == 2
    assert captured["exclude_doc_ids"] == {
        "ranked-doc",
        "rejected-doc",
        "hybrid-doc-1",
        "hybrid-doc-2",
    }
    assert captured["exclude_product_ids"] == {101, 102, 103}
    assert [product["result_tier"] for product in result["products"]] == [
        "ranked",
        "related",
        "related",
        "related",
        "related",
    ]
    index.close()


def test_semantic_tail_can_require_an_explicit_category(tmp_path):
    index = build_index(tmp_path / "conditional-related-tail.sqlite3")
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        semantic_related_tail_enabled=True,
        semantic_related_tail_requires_explicit_category=True,
    )

    assert not engine._semantic_related_tail_allowed(
        {"categorical": {"city_name": "Chennai"}}
    )
    assert not engine._semantic_related_tail_allowed(
        {"categorical": {}, "max_rental_fee": 1000}
    )
    assert engine._semantic_related_tail_allowed(
        {"categorical": {"main_category_name": "Automobiles"}}
    )
    assert engine._semantic_related_tail_allowed(
        {"categorical": {"subcategory_name": "Bike"}}
    )
    index.close()
