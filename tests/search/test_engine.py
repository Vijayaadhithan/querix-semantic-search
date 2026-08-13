import json
import threading
import time
from concurrent.futures import Future

from search import engine as search_engine
from search import planner_catalog as query_planner_catalog
from search.bm25 import PersistentBM25Index
from search.engine import ProductSearchEngine
from search.planner import (
    QueryFilterCatalog,
    default_query_plan,
    deterministic_filter_query_plan,
    direct_semantic_query_plan,
    enrich_query_plan,
    extract_query_plan,
    extract_sort_order,
    normalize_transliterated_query,
    query_filter_value_index,
)
from tenants.gainr.policy import GainrSearchPolicy, contains_phrase


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


class PromptIdentityProvider(CountingQueryProvider):
    def __init__(self):
        super().__init__()
        self.system_prompt_ids = []

    def structured_chat(
        self,
        _model,
        system_prompt,
        _user_prompt,
        _schema,
        _temperature,
    ):
        self.calls += 1
        self.system_prompt_ids.append(id(system_prompt))
        return json.dumps(
            {
                "semantic_query": "camera",
                "keyword_query": "camera",
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
    wanted = deterministic_filter_query_plan("someone looking for bikes", value_index)
    personal_offer = deterministic_filter_query_plan("looking for a bike", value_index)

    assert bike["execution_path"] == "deterministic_filter"
    assert bike["filters"]["subcategory"] == "Bike"
    assert filtered["filters"]["subcategory"] == "Bike"
    assert filtered["filters"]["city"] == "Chennai"
    assert filtered["filters"]["state"] == "Tamil Nadu"
    assert filtered["filters"]["max_rental_fee"] == 1000
    assert wanted is None
    assert personal_offer is None
    index.close()


def test_direct_semantic_plan_accepts_objective_catalog_phrase(tmp_path):
    index = build_index(tmp_path / "direct-semantic.sqlite3")
    value_index = query_filter_value_index(index)

    plan, reason = direct_semantic_query_plan(
        "red bike with ABS",
        value_index,
    )

    assert reason == "objective_catalog_phrase"
    assert plan["execution_path"] == "direct_semantic"
    assert plan["route_reason"] == reason
    assert plan["semantic_query"] == "red bike with ABS"
    assert plan["keyword_query"] == "red bike with ABS"
    assert plan["filters"]["subcategory"] == "Bike"
    assert plan["filters"]["main_category"] == "Automobiles"
    assert plan["inferred_categories"]["subcategory"] is None
    assert plan["relaxed_categories"] == []
    index.close()


def test_natural_offer_and_buyer_demand_use_direct_semantic(tmp_path):
    index = build_index(tmp_path / "natural-demand-direct-semantic.sqlite3")
    value_index = query_filter_value_index(index)
    policy = GainrSearchPolicy()

    personal_offer, offer_reason = direct_semantic_query_plan(
        "looking for a car",
        value_index,
        search_policy=policy,
    )
    buyer_demand, demand_reason = direct_semantic_query_plan(
        "people need bikes",
        value_index,
        search_policy=policy,
    )
    broad_demand, broad_reason = direct_semantic_query_plan(
        "someone who wants equipment service",
        value_index,
        search_policy=policy,
    )
    interested_demand, interested_reason = direct_semantic_query_plan(
        "customers interested in bikes",
        value_index,
        search_policy=policy,
    )

    assert offer_reason == "descriptive_marketplace_offer"
    assert personal_offer["execution_path"] == "direct_semantic"
    assert personal_offer["target_ad_type"] == "offer"
    assert demand_reason == "buyer_demand_semantic"
    assert buyer_demand["execution_path"] == "direct_semantic"
    assert buyer_demand["target_ad_type"] == "wanted"
    assert buyer_demand["filters"]["subcategory"] == "Bike"
    assert broad_reason == "buyer_demand_semantic"
    assert broad_demand["execution_path"] == "direct_semantic"
    assert broad_demand["target_ad_type"] == "wanted"
    assert interested_reason == "buyer_demand_semantic"
    assert interested_demand["execution_path"] == "direct_semantic"
    assert interested_demand["target_ad_type"] == "wanted"
    assert interested_demand["filters"]["subcategory"] == "Bike"
    index.close()


def test_marketplace_interest_is_not_misread_as_location_language():
    assert query_planner_catalog.location_phrases("customers interested in cars") == []
    assert query_planner_catalog.location_phrases(
        "customers interested in cars in Chennai"
    ) == ["chennai"]


def test_conversational_catalog_requests_never_use_deterministic_path(tmp_path):
    index = build_index(tmp_path / "conversational-routing.sqlite3")
    value_index = query_filter_value_index(index)
    policy = GainrSearchPolicy()
    queries = (
        "I need a bike",
        "please show bikes",
        "can you find me a bike?",
        "do you have bikes",
        "people requiring bikes",
        "customers interested in bikes",
        "find buyers for bikes",
        "wanted bike",
        "bike wanted",
    )

    for query in queries:
        assert (
            deterministic_filter_query_plan(
                query,
                value_index,
                search_policy=policy,
            )
            is None
        ), query
    index.close()


def test_direct_semantic_preserves_head_category_and_compound_catalog_concept(
    tmp_path,
):
    index = PersistentBM25Index(tmp_path / "head-category.sqlite3")
    index.upsert(
        [
            product_row(
                "car",
                main_category_name="Automobiles",
                subcategory_name="Car",
            ),
            product_row(
                "wheelchair",
                main_category_name="Medical Equipments",
                subcategory_name="Wheelchair",
            ),
            product_row(
                "music-book",
                main_category_name="Books",
                subcategory_name="Music",
            ),
            product_row(
                "guitar",
                main_category_name="Musical Instruments",
                subcategory_name="Guitar",
            ),
        ]
    )
    value_index = query_filter_value_index(index)

    accessible_car, car_reason = direct_semantic_query_plan(
        "car with wheelchair access",
        value_index,
    )
    instrument, instrument_reason = direct_semantic_query_plan(
        "music instrument with protective case",
        value_index,
    )

    assert car_reason == "objective_catalog_phrase"
    assert accessible_car["filters"]["main_category"] == "Automobiles"
    assert accessible_car["filters"]["subcategory"] == "Car"
    assert instrument_reason == "objective_catalog_phrase"
    assert instrument["filters"]["main_category"] == "Musical Instruments"
    assert instrument["filters"]["subcategory"] is None
    assert instrument["inferred_categories"] == {
        "main_category": None,
        "subcategory": None,
    }
    index.close()


def test_attribute_prefixed_category_keeps_parent_hard_and_child_soft(tmp_path):
    index = PersistentBM25Index(tmp_path / "relaxed-child-category.sqlite3")
    index.upsert(
        [
            product_row(
                "bike",
                main_category_name="Automobiles",
                subcategory_name="Bike",
            ),
            product_row(
                "electric-scooter",
                main_category_name="Automobiles",
                subcategory_name="Electric Scooter",
            ),
        ]
    )
    value_index = query_filter_value_index(index)

    plan, reason = direct_semantic_query_plan(
        "electric bike with removable battery",
        value_index,
    )

    assert reason == "objective_catalog_phrase"
    assert plan["filters"]["main_category"] == "Automobiles"
    assert plan["filters"]["subcategory"] is None
    assert plan["inferred_categories"]["subcategory"] == "Bike"
    assert plan["relaxed_categories"] == ["subcategory"]
    index.close()


def test_direct_semantic_plan_rejects_queries_that_need_llm_reasoning(tmp_path):
    index = build_index(tmp_path / "direct-semantic-rejections.sqlite3")
    value_index = query_filter_value_index(index)
    cases = {
        "red bike in Chennai": "location_language",
        "bike under 1000": "numeric_constraint_or_model",
        "comfortable bike for a long trip": "complex_or_subjective_language",
        "someone looking for bikes": "ad_type_intent",
        "red bke with ABS": "query_requires_normalization",
        "சிவப்பு bike": "non_ascii_language",
        "red bike venum": "complex_or_subjective_language",
    }

    for query, expected_reason in cases.items():
        plan, reason = direct_semantic_query_plan(
            query,
            value_index,
            {"bke": "bike"},
        )
        assert plan is None, query
        assert reason == expected_reason, query
    index.close()


def test_gainr_descriptive_vehicle_offer_skips_hosted_planner(tmp_path):
    index = build_index(tmp_path / "gainr-descriptive-vehicle.sqlite3")
    value_index = query_filter_value_index(index)

    plan, reason = direct_semantic_query_plan(
        "comfortable car for long travel",
        value_index,
        search_policy=GainrSearchPolicy(),
    )

    assert reason == "descriptive_marketplace_offer"
    assert plan["execution_path"] == "direct_semantic"
    assert plan["filters"]["subcategory"] is None
    assert plan["inferred_categories"]["main_category"] == "Automobiles"
    assert "comfortable and safe long-distance travel" in plan["semantic_query"]
    index.close()


def test_engine_direct_semantic_route_skips_query_provider(tmp_path):
    index = build_index(tmp_path / "direct-semantic-engine.sqlite3")
    provider = CountingQueryProvider()
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        query_provider=provider,
    )

    result = engine.plan("outdoor bike")

    assert result["query_plan"]["execution_path"] == "direct_semantic"
    assert result["query_plan"]["route_reason"] == "objective_catalog_phrase"
    assert result["query_model_metrics"] == {}
    assert provider.calls == 0
    engine.close()
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
    assert "This tenant rents professional event equipment." in provider.system_prompt
    assert "Return the structured query plan." in provider.user_prompt
    assert '"semantic_query"' not in provider.user_prompt


def test_static_planner_prompt_is_reused_for_the_same_tenant_catalog():
    provider = PromptIdentityProvider()
    catalog = QueryFilterCatalog(
        {
            "main_category": ["Audio & Video"],
            "state": ["Tamil Nadu"],
        }
    )

    extract_query_plan(
        "camera for a wedding",
        catalog,
        query_provider=provider,
        prompt_context="Professional rentals.",
    )
    extract_query_plan(
        "camera for a conference",
        catalog,
        query_provider=provider,
        prompt_context="Professional rentals.",
    )

    assert provider.calls == 2
    assert len(set(provider.system_prompt_ids)) == 1


def test_transliterated_queries_receive_trusted_semantic_normalization(tmp_path):
    index = build_index(tmp_path / "transliterated-plan.sqlite3")
    provider = CapturingQueryProvider()
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        query_provider=provider,
        direct_semantic_fast_path=False,
    )
    try:
        result = engine.plan("veetu vela kaari in Chennai")
    finally:
        engine.close()
        index.close()

    assert result["query_plan"]["execution_path"] == "semantic"
    assert "romanized/transliterated" in provider.system_prompt
    assert "not a car" in provider.system_prompt
    assert "Original user query:\nveetu vela kaari in Chennai" in (provider.user_prompt)
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
    assert normalize_transliterated_query("Ford car for rent") == ("Ford car for rent")


def test_company_intent_is_not_rewritten_by_shared_normalization():
    assert normalize_transliterated_query("massage in Coimbatore") == (
        "massage in Coimbatore"
    )
    assert normalize_transliterated_query("body massage") == "body massage"
    assert normalize_transliterated_query("massage gun in Coimbatore") == (
        "massage gun in Coimbatore"
    )
    assert normalize_transliterated_query("body massager") == "body massager"


def test_reviewed_category_typos_are_semantically_normalized():
    gainr_aliases = {
        "bke": "bike",
        "techcician": "technician",
    }
    assert normalize_transliterated_query("bke in Chennai", gainr_aliases) == (
        "bike in Chennai"
    )
    assert (
        normalize_transliterated_query(
            "techcician in Coimbatore",
            gainr_aliases,
        )
        == "technician in Coimbatore"
    )
    assert normalize_transliterated_query("Ford in Coimbatore") == (
        "Ford in Coimbatore"
    )
    assert normalize_transliterated_query("bke in Chennai") == ("bke in Chennai")


def test_gainr_reviewed_tamil_search_terms_use_semantic_normalization(tmp_path):
    index = build_index(tmp_path / "gainr-tamil-aliases.sqlite3")
    index.upsert(
        [
            product_row(
                "room-coimbatore",
                main_category_name="Accommodation & Spaces",
                subcategory_name="Room",
                state_name="Tamil Nadu",
                city_name="Coimbatore",
            ),
            product_row(
                "astrologer-coimbatore",
                main_category_name="Pandits & Priests",
                subcategory_name="Astrologer",
                state_name="Tamil Nadu",
                city_name="Coimbatore",
            ),
            product_row(
                "maid-coimbatore",
                main_category_name="Personal & Home Services",
                subcategory_name="Maid",
                state_name="Tamil Nadu",
                city_name="Coimbatore",
            ),
            product_row(
                "house-keeper-coimbatore",
                main_category_name="Personal & Home Services",
                subcategory_name="House Keeper",
                state_name="Tamil Nadu",
                city_name="Coimbatore",
            ),
        ]
    )
    value_index = query_filter_value_index(index)
    policy = GainrSearchPolicy()
    housing_aliases = {
        "enaku veedu vadaiku venum": ("house home residential accommodation for rent")
    }
    astrology_aliases = {"josiyakar": "astrologer astrology service"}

    housing, housing_reason = direct_semantic_query_plan(
        "enaku veedu vadaiku venum",
        value_index,
        housing_aliases,
        search_policy=policy,
    )
    astrology, astrology_reason = direct_semantic_query_plan(
        "josiyakar",
        value_index,
        astrology_aliases,
        search_policy=policy,
    )

    assert housing is None
    assert housing_reason == "query_requires_normalization"
    assert astrology_reason == "reviewed_normalization_offer"
    assert astrology["execution_path"] == "direct_semantic"
    assert astrology["target_ad_type"] == "offer"
    assert astrology["filters"]["main_category"] == "Pandits & Priests"
    assert astrology["filters"]["subcategory"] is None
    assert astrology["inferred_categories"]["subcategory"] == "Astrologer"
    assert (
        normalize_transliterated_query(
            "enaku veedu vadaiku venum",
            housing_aliases,
        )
        == "house home residential accommodation for rent"
    )
    astrology_intent = policy.category_intent(
        normalize_transliterated_query("josiyakar", astrology_aliases),
        value_index["subcategory"],
    )
    assert astrology_intent is not None
    assert astrology_intent.subcategory == "Astrologer"
    assert (
        policy.infer_main_category(
            normalize_transliterated_query(
                "enaku veedu vadaiku venum",
                housing_aliases,
            ),
            value_index["main_category"],
        )
        == "Accommodation & Spaces"
    )

    maid_plan = enrich_query_plan(
        "veetu vela kaari",
        default_query_plan("veetu vela kaari"),
        value_index,
        search_policy=policy,
    )
    assert maid_plan["filters"]["main_category"] == "Personal & Home Services"
    assert maid_plan["filters"]["subcategory"] is None
    assert maid_plan["inferred_categories"]["subcategory"] == "Maid"
    assert "subcategory" in maid_plan["relaxed_categories"]

    maid, maid_reason = direct_semantic_query_plan(
        "veetu vela kaari",
        value_index,
        search_policy=policy,
    )
    assert maid_reason == "reviewed_normalization_offer"
    assert maid["execution_path"] == "direct_semantic"
    assert maid["target_ad_type"] == "offer"
    assert maid["filters"]["main_category"] == "Personal & Home Services"
    assert maid["filters"]["subcategory"] is None
    assert maid["inferred_categories"]["subcategory"] == "Maid"
    index.close()


def test_query_aliases_are_scoped_to_the_engine_instance(tmp_path):
    index = build_index(tmp_path / "tenant-alias-plan.sqlite3")
    provider = CapturingQueryProvider()
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        query_provider=provider,
        planner_query_aliases={"bke": "bike"},
    )
    try:
        result = engine.plan("bke in Chennai")
    finally:
        engine.close()
        index.close()

    assert result["query_plan"]["execution_path"] == "semantic"
    assert "bike in Chennai" in provider.user_prompt


def test_query_aliases_support_semantic_fallback_without_planner(tmp_path):
    index = build_index(tmp_path / "tenant-alias-fallback.sqlite3")
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        planner_enabled=False,
        planner_query_aliases={"bke": "bike"},
    )
    try:
        result = engine.plan("red bke with ABS")
    finally:
        engine.close()
        index.close()

    assert result["query_plan"]["execution_path"] == "semantic"
    assert result["query_plan"]["semantic_query"] == "red bike with ABS"
    assert result["query_plan"]["keyword_query"] == "red bike with ABS"


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


def test_category_typos_stay_semantic_while_location_typos_are_corrected(tmp_path):
    index = build_index(tmp_path / "fuzzy-fast-plan.sqlite3")
    value_index = query_filter_value_index(index)

    category = deterministic_filter_query_plan("bke", value_index)
    combined = deterministic_filter_query_plan(
        "bikes in chni under 1000",
        value_index,
    )

    assert category is None
    assert combined["execution_path"] == "deterministic_filter"
    assert combined["filters"]["subcategory"] == "Bike"
    assert combined["filters"]["city"] == "Chennai"
    assert combined["filters"]["state"] == "Tamil Nadu"
    assert combined["filters"]["max_rental_fee"] == 1000
    assert combined["query_corrections"] == [
        {"field": "city", "input": "chni", "value": "Chennai"},
    ]
    index.close()


def test_bare_massage_is_not_corrected_to_massager_product(tmp_path):
    index = build_index(tmp_path / "massage-intent.sqlite3")
    index.upsert(
        [
            product_row(
                "massager",
                main_category_name="Life Style Products",
                subcategory_name="Massager",
                city_name="Coimbatore",
            ),
            product_row(
                "massage-therapist",
                main_category_name="Health & Wellness",
                subcategory_name="Massage Therapist",
                city_name="Coimbatore",
            ),
        ]
    )
    value_index = query_filter_value_index(index)

    assert deterministic_filter_query_plan("massage", value_index) is None
    equipment = deterministic_filter_query_plan("massager", value_index)
    assert equipment["execution_path"] == "deterministic_filter"
    assert equipment["filters"]["subcategory"] == "Massager"
    index.close()


def test_separate_word_is_not_corrected_to_similar_category(tmp_path):
    index = build_index(tmp_path / "escort-intent.sqlite3")
    index.upsert(
        [
            product_row(
                "resort",
                main_category_name="Accommodation & Spaces",
                subcategory_name="Resort",
                city_name="Coimbatore",
            ),
            product_row(
                "security-escort",
                main_category_name="Security Services",
                subcategory_name="Security Escort",
                city_name="Coimbatore",
            ),
            product_row(
                "technician",
                main_category_name="Repair & Technical Services",
                subcategory_name="Technician",
                city_name="Coimbatore",
            ),
            product_row(
                "energy",
                main_category_name="Other Services",
                subcategory_name="Energy",
                city_name="Coimbatore",
            ),
            product_row(
                "mask",
                main_category_name="Costumes",
                subcategory_name="Mask",
                city_name="Coimbatore",
            ),
            product_row(
                "driving",
                main_category_name="Transport Services",
                subcategory_name="Driving",
                city_name="Coimbatore",
            ),
            product_row(
                "food",
                main_category_name="Food Services",
                subcategory_name="Food",
                city_name="Coimbatore",
            ),
            product_row(
                "doll",
                main_category_name="Toys",
                subcategory_name="Doll",
                city_name="Coimbatore",
            ),
            product_row(
                "doctor",
                main_category_name="Health Services",
                subcategory_name="Doctor",
                city_name="Coimbatore",
            ),
        ]
    )
    value_index = query_filter_value_index(index)

    for separate_word in (
        "escort",
        "entry",
        "mark",
        "drawing",
        "draping",
        "drilling",
        "ford",
        "dell",
        "door",
    ):
        assert deterministic_filter_query_plan(separate_word, value_index) is None
    resort = deterministic_filter_query_plan("resort", value_index)
    assert resort["execution_path"] == "deterministic_filter"
    assert resort["filters"]["subcategory"] == "Resort"
    technician_typo = deterministic_filter_query_plan(
        "techcician",
        value_index,
    )
    assert technician_typo is None
    assert (
        normalize_transliterated_query(
            "techcician",
            {"techcician": "technician"},
        )
        == "technician"
    )
    index.close()


def test_bare_massage_stays_semantic_without_a_hard_product_filter(tmp_path):
    index = build_index(tmp_path / "massage-semantic-plan.sqlite3")
    index.upsert(
        [
            product_row(
                "massager",
                main_category_name="Life Style Products",
                subcategory_name="Massager",
                state_name="Tamil Nadu",
                city_name="Coimbatore",
            ),
            product_row(
                "massage-therapist",
                main_category_name="Health & Wellness",
                subcategory_name="Massage Therapist",
                state_name="Tamil Nadu",
                city_name="Coimbatore",
            ),
        ]
    )
    provider = CapturingQueryProvider()
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        query_provider=provider,
    )
    try:
        result = engine.plan("massage in Coimbatore")
    finally:
        engine.close()
        index.close()

    assert provider.calls == 1
    assert result["query_plan"]["execution_path"] == "semantic"
    assert "Original user query:\nmassage in Coimbatore" in provider.user_prompt
    assert result["resolved_filters"]["categorical"] == {
        "state_name": "Tamil Nadu",
        "city_name": "Coimbatore",
    }
    assert "subcategory_name" not in result["resolved_filters"]["categorical"]


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
    assert typo is None
    assert normalize_transliterated_query(
        "1000 bke rent in chni",
        {"bke": "bike"},
    ) == ("1000 bike rent in chni")
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
        ("cheapest daily bike", "Bike", "Per Day"),
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

    for query, subcategory, sort_order in (
        ("pocket-friendly camera", "Camera", "price_asc"),
        ("reasonably priced room", "Room", "price_asc"),
        ("price l2h bike", "Bike", "price_asc"),
        ("priciest bike", "Bike", "price_desc"),
        ("price h2l camera", "Camera", "price_desc"),
    ):
        plan = deterministic_filter_query_plan(query, value_index)
        assert plan["execution_path"] == "deterministic_filter"
        assert plan["filters"]["subcategory"] == subcategory
        assert plan["sort_order"] == sort_order

    index.close()


def test_price_sort_wording_is_extracted_deterministically():
    ascending = (
        "cheapest car",
        "lowest priced bike",
        "low rental rate camera",
        "affordable car rental",
        "budget-friendly bike",
        "economical car",
        "inexpensive bike",
        "pocket-friendly camera",
        "low-cost room",
        "bargain-priced car",
        "reasonably priced room",
        "price l2h",
        "price low2high",
        "price lo to hi",
        "price low to high",
        "low to high rental fees",
        "sort by price ascending",
        "rental rate ascending",
    )
    descending = (
        "most expensive car",
        "highest price bike",
        "costliest car",
        "dearest car",
        "higher priced bike",
        "priciest bike",
        "top-priced camera",
        "price h2l",
        "price high2low",
        "price hi to lo",
        "rental fee high to low",
        "high to low price",
        "order by rate desc",
        "price descending",
    )

    assert all(extract_sort_order(query) == "price_asc" for query in ascending)
    assert all(extract_sort_order(query) == "price_desc" for query in descending)
    assert extract_sort_order("car under 1000") is None
    assert extract_sort_order("medium price car") is None


def test_gainr_service_intents_are_semantic_hard_category_boundaries(tmp_path):
    index = PersistentBM25Index(tmp_path / "gainr-service-intents.sqlite3")
    index.upsert(
        [
            product_row(
                "massage-therapist",
                main_category_name="Personal & Home Services",
                subcategory_name="Massage Therapist",
            ),
            product_row(
                "massage-chair",
                main_category_name="Health & Wellness",
                subcategory_name="Massage Chair",
            ),
            product_row(
                "massager",
                main_category_name="Health & Wellness",
                subcategory_name="Massager",
            ),
            product_row(
                "plumber",
                main_category_name="Personal & Home Services",
                subcategory_name="Plumber",
            ),
            product_row(
                "electrician",
                main_category_name="Personal & Home Services",
                subcategory_name="Electrician",
            ),
            product_row(
                "mathematics-book",
                main_category_name="Books",
                subcategory_name="Mathematics",
            ),
            product_row(
                "mathematics-teacher",
                main_category_name="Education Field",
                subcategory_name="Teacher",
            ),
        ]
    )
    value_index = query_filter_value_index(index)
    policy = GainrSearchPolicy()

    massage, massage_reason = direct_semantic_query_plan(
        "low cost body massage near me",
        value_index,
        search_policy=policy,
    )
    pipes, pipes_reason = direct_semantic_query_plan(
        "someone who can repair leaking pipes",
        value_index,
        search_policy=policy,
    )
    wiring, wiring_reason = direct_semantic_query_plan(
        "repair electrical wiring",
        value_index,
        search_policy=policy,
    )
    equipment = deterministic_filter_query_plan(
        "massage chair",
        value_index,
        search_policy=policy,
    )
    teacher, teacher_reason = direct_semantic_query_plan(
        "mathematics teacher",
        value_index,
        search_policy=policy,
    )

    assert (
        deterministic_filter_query_plan(
            "low cost body massage near me",
            value_index,
            search_policy=policy,
        )
        is None
    )
    assert (
        deterministic_filter_query_plan(
            "someone who can repair leaking pipes",
            value_index,
            search_policy=policy,
        )
        is None
    )
    assert (
        deterministic_filter_query_plan(
            "repair electrical wiring",
            value_index,
            search_policy=policy,
        )
        is None
    )
    assert (
        deterministic_filter_query_plan(
            "mathematics teacher",
            value_index,
            search_policy=policy,
        )
        is None
    )
    assert massage is None
    assert massage_reason == "sort_language"
    assert pipes["execution_path"] == "direct_semantic"
    assert pipes_reason == "descriptive_marketplace_offer"
    assert wiring["execution_path"] == "direct_semantic"
    assert wiring_reason == "objective_catalog_phrase"
    assert teacher["execution_path"] == "direct_semantic"
    assert teacher_reason == "objective_catalog_phrase"
    massage = enrich_query_plan(
        "low cost body massage near me",
        default_query_plan("low cost body massage near me"),
        value_index,
        search_policy=policy,
    )
    assert massage["sort_order"] == "price_asc"
    assert massage["filters"]["subcategory"] == "Massage Therapist"
    assert massage["filters"]["main_category"] == "Personal & Home Services"
    assert massage["inferred_categories"]["subcategory"] is None
    assert pipes["filters"]["subcategory"] == "Plumber"
    assert wiring["filters"]["subcategory"] == "Electrician"
    assert equipment["filters"]["subcategory"] == "Massage Chair"
    assert teacher["filters"]["subcategory"] == "Teacher"
    assert teacher["filters"]["main_category"] == "Education Field"
    index.close()


def test_gainr_body_massage_attributes_stay_semantic_with_hard_category(tmp_path):
    index = PersistentBM25Index(tmp_path / "gainr-massage-attributes.sqlite3")
    index.upsert(
        [
            product_row(
                "massage-therapist",
                main_category_name="Personal & Home Services",
                subcategory_name="Massage Therapist",
            ),
            product_row(
                "massager",
                main_category_name="Health & Wellness",
                subcategory_name="Massager",
            ),
        ]
    )
    value_index = query_filter_value_index(index)
    policy = GainrSearchPolicy()

    assert (
        deterministic_filter_query_plan(
            "body massage with oil",
            value_index,
            search_policy=policy,
        )
        is None
    )
    plan, reason = direct_semantic_query_plan(
        "body massage with oil",
        value_index,
        search_policy=policy,
    )

    assert reason == "objective_catalog_phrase"
    assert plan["execution_path"] == "direct_semantic"
    assert plan["filters"]["subcategory"] == "Massage Therapist"
    assert plan["filters"]["main_category"] == "Personal & Home Services"
    index.close()


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

    descriptive_queries = (
        "red bike with ABS in Chennai",
        "red bke with ABS in chni",
        "vehicle for recreational driving on rough terrain",
        "wanted bike",
        "comfortable car for long travel",
        "car with airbags and good mileage",
        "bike suitable for mountain roads",
        "washing machine for a large family",
        "table and chairs for a wedding reception",
        "camera with excellent low light performance",
        "affordable room near office with parking",
        "need hindi speaking driver",
        "vehicle for 12 people",
        "fridge not working",
    )

    for query in descriptive_queries:
        assert deterministic_filter_query_plan(query, value_index) is None, query
    index.close()


def test_normalized_query_plan_cache_skips_repeated_planner_work(
    tmp_path,
    monkeypatch,
):
    index = build_index(tmp_path / "plan-cache.sqlite3")
    provider = CountingQueryProvider()
    deterministic_calls = []
    original_deterministic_plan = search_engine.deterministic_filter_query_plan

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
        direct_semantic_fast_path=False,
    )

    first = engine.plan("red bike")
    second = engine.plan("  RED   BIKE ")

    assert provider.calls == 1
    assert len(deterministic_calls) == 1
    assert first["plan_cache_hit"] is False
    assert second["plan_cache_hit"] is True
    assert second["query_model_metrics"] == {}
    index.close()


def test_semantic_planner_reuses_exact_query_analysis_between_passes(
    tmp_path,
    monkeypatch,
):
    index = build_index(tmp_path / "analysis-reuse.sqlite3")
    provider = CountingQueryProvider()
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        query_provider=provider,
        direct_semantic_fast_path=False,
    )
    calls = []
    original = query_planner_catalog.find_catalog_value

    def counted(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(
        query_planner_catalog,
        "find_catalog_value",
        counted,
    )

    result = engine.plan("red bike")

    assert result["query_plan"]["execution_path"] == "semantic"
    assert provider.calls == 1
    # Category concepts are resolved by the category matcher; the reusable
    # analysis still performs the remaining catalog lookups only once.
    assert calls == ["red bike"] * 4
    engine.close()
    index.close()


def test_plan_cache_fingerprint_changes_with_catalog(tmp_path):
    first_index = build_index(tmp_path / "first-catalog.sqlite3")
    second_index = build_index(tmp_path / "second-catalog.sqlite3")
    second_index.upsert(
        [
            product_row(
                "camera-chennai",
                main_category_name="Audio & Video",
                subcategory_name="Camera",
                state_name="Tamil Nadu",
                city_name="Chennai",
            )
        ]
    )
    cache = DictSharedCache()
    first_provider = CountingQueryProvider()
    second_provider = CountingQueryProvider()
    first_engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=first_index,
        query_provider=first_provider,
        shared_plan_cache=cache,
        direct_semantic_fast_path=False,
    )
    second_engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=second_index,
        query_provider=second_provider,
        shared_plan_cache=cache,
        direct_semantic_fast_path=False,
    )

    first_engine.plan("red bike")
    second = second_engine.plan("red bike")

    assert first_provider.calls == 1
    assert second_provider.calls == 1
    assert second["plan_cache_hit"] is False
    first_engine.close()
    second_engine.close()
    first_index.close()
    second_index.close()


def test_shared_plan_cache_survives_engine_restart(tmp_path):
    index = build_index(tmp_path / "shared-plan-cache.sqlite3")
    cache = DictSharedCache()
    first_provider = CountingQueryProvider()
    first_engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        query_provider=first_provider,
        shared_plan_cache=cache,
        direct_semantic_fast_path=False,
    )

    first = first_engine.plan("red bike")

    second_provider = CountingQueryProvider()
    second_engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        query_provider=second_provider,
        shared_plan_cache=cache,
        direct_semantic_fast_path=False,
    )
    second = second_engine.plan(" RED   BIKE ")

    assert first["plan_cache_hit"] is False
    assert second["plan_cache_hit"] is True
    assert first_provider.calls == 1
    assert second_provider.calls == 0
    assert all("red" not in key for _namespace, key in cache.values)
    assert second_engine.plan_cache_health() == {
        "redis_enabled": True,
        "redis_connected": True,
        "query_plan_cache_backend": "redis+memory",
        "result_cache_enabled": True,
        "result_cache_ttl_seconds": 300,
    }
    index.close()


def test_shared_plan_cache_is_namespaced_by_company(tmp_path):
    alpha_index = build_index(tmp_path / "alpha-plan-cache.sqlite3")
    beta_index = build_index(tmp_path / "beta-plan-cache.sqlite3")
    cache = DictSharedCache()
    alpha_provider = CountingQueryProvider()
    beta_provider = CountingQueryProvider()
    alpha_engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=alpha_index,
        query_provider=alpha_provider,
        shared_plan_cache=cache,
        company_id="alpha",
        direct_semantic_fast_path=False,
    )
    beta_engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=beta_index,
        query_provider=beta_provider,
        shared_plan_cache=cache,
        company_id="beta",
        direct_semantic_fast_path=False,
    )

    alpha_engine.plan("red bike")
    beta_engine.plan("red bike")

    assert alpha_provider.calls == 1
    assert beta_provider.calls == 1
    assert {namespace for namespace, _key in cache.values} == {
        "alpha:query_plan",
        "beta:query_plan",
    }
    alpha_index.close()
    beta_index.close()


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
    assert all(product["result_tier"] == "filtered" for product in second["products"])
    assert any(namespace == "search_result" for namespace, _key in cache.values)

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


def test_speculative_embedding_is_reused_only_for_exact_semantic_query(
    tmp_path,
    monkeypatch,
):
    index = build_index(tmp_path / "embedding-prefetch.sqlite3")
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        semantic_related_tail_enabled=False,
    )
    captured = []

    def retrieve(*_args, **kwargs):
        captured.append(kwargs.get("query_embedding"))
        return {
            "vector_results": [],
            "bm25_results": [],
            "candidates": [],
            "hybrid_tail_candidates": [],
            "vector_seconds": 0.0,
            "bm25_seconds": 0.0,
            "retrieval_seconds": 0.0,
            "parallel_retrieval_seconds": 0.0,
            "fusion_seconds": 0.0,
            "type_lookup_seconds": 0.0,
            "vector_query_metrics": {},
            "embedding_model_metrics": {},
            "retrieval_degraded": False,
            "retrieval_error_type": None,
            "degraded_stages": [],
        }

    monkeypatch.setattr(engine, "retrieve", retrieve)
    exact_future = Future()
    exact_future.set_result(
        {
            "query": "red bike",
            "embedding": [1.0, 2.0],
            "metrics": {},
            "seconds": 0.01,
        }
    )
    rewritten_future = Future()
    rewritten_future.set_result(
        {
            "query": "camera for wedding",
            "embedding": [3.0, 4.0],
            "metrics": {},
            "seconds": 0.01,
        }
    )

    def planned(semantic_query):
        return {
            "query_plan": {
                "semantic_query": semantic_query,
                "keyword_query": semantic_query,
                "target_ad_type": "offer",
                "execution_path": "semantic",
                "sort_order": None,
                "filters": {},
                "inferred_categories": {},
            },
            "resolved_filters": {"categorical": {}},
            "unresolved_filters": {},
            "query_model_metrics": {},
            "seconds": 0.0,
            "plan_cache_hit": False,
        }

    engine.search(
        "red bike",
        limit=1,
        planned_result=planned("red bike"),
        hydrate_products=False,
        speculative_embedding_future=exact_future,
    )
    engine.search(
        "camera for wedding",
        limit=1,
        planned_result=planned("wedding photography camera"),
        hydrate_products=False,
        speculative_embedding_future=rewritten_future,
    )

    assert captured == [[1.0, 2.0], None]
    engine.close()
    index.close()


def test_speculative_embedding_can_be_cancelled_before_local_work_starts(
    tmp_path,
):
    index = build_index(tmp_path / "embedding-prefetch-cancel.sqlite3")

    class CountingEmbeddingProvider:
        def __init__(self):
            self.calls = 0

        def embed_text(self, _query):
            self.calls += 1
            return [1.0, 2.0]

    provider = CountingEmbeddingProvider()
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        embedding_provider=provider,
    )

    future = engine.start_speculative_embedding("camera")

    assert future.cancel() is True
    time.sleep(0.05)
    assert provider.calls == 0
    engine.close()
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
    assert result["parallel_retrieval_seconds"] >= 0
    assert result["fusion_seconds"] >= 0
    assert result["type_lookup_seconds"] >= 0
    assert result["retrieval_seconds"] >= max(
        result["vector_seconds"],
        result["bm25_seconds"],
    )
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
        search_policy=GainrSearchPolicy(),
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

    def bm25(_query, _index, _filters, top_k, **_kwargs):
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
            "semantic_query": ("vehicle for long distance with comfort and safety"),
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

    adjusted = GainrSearchPolicy().adjust_candidates(
        query_plan,
        candidates,
    )

    assert [candidate["id"] for candidate in adjusted] == [
        "driver",
        "safety-auditor",
        "detailer",
    ]


def test_gainr_tamil_load_queries_are_rewritten_as_goods_transport():
    policy = GainrSearchPolicy()
    queries = (
        "சன்னை டு சலம் பாஸ்ட் லாடு இருந்தால் அழைக்கவும்",
        "சன்னையில் இருந்து சலத்துக்கு லாடு இருந்தால் அழைக்கவும்",
    )

    for query in queries:
        assert policy.rewrite_semantic_query(query, "passenger car") == (
            "goods load transport truck mini truck cargo vehicle"
        )
        assert policy.rewrite_keyword_query(query, "car driver") == (
            "goods load transport truck mini truck cargo vehicle tata ace"
        )


def test_gainr_goods_transport_prefers_cargo_over_passenger_vehicles():
    query_plan = {
        "semantic_query": "goods load transport truck mini truck cargo vehicle",
        "keyword_query": "goods load transport truck mini truck cargo vehicle tata ace",
    }
    candidates = [
        {
            "id": "car",
            "text": "Nissan Sunny Car for Daily Rent",
            "metadata": {"main_category_name": "Automobiles"},
            "fusion_score": 0.10,
        },
        {
            "id": "driver",
            "text": "Light Motor Vehicle Acting Driver for Daily Hire",
            "metadata": {"main_category_name": "Automobiles"},
            "fusion_score": 0.09,
        },
        {
            "id": "truck",
            "text": "Tata Ace Mini Truck for Daily Rent",
            "metadata": {"main_category_name": "Automobiles"},
            "fusion_score": 0.04,
        },
    ]

    policy = GainrSearchPolicy()
    adjusted = policy.adjust_candidates(query_plan, candidates)

    assert [candidate["id"] for candidate in adjusted] == [
        "truck",
        "car",
        "driver",
    ]
    assert "goods/load transport" in policy.rerank_context(query_plan)


def test_gainr_vehicle_phrases_require_word_boundaries():
    assert contains_phrase("car for rent", {"car"})
    assert not contains_phrase("carpet cleaning", {"car"})
    assert not contains_phrase("advanced service", {"van"})


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

    adjusted = GainrSearchPolicy().adjust_candidates(
        query_plan,
        candidates,
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
        search_policy=GainrSearchPolicy(),
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

    assert "Tenant domain intent" in ranker.queries[0]
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
            raise RuntimeError("All reranker providers failed: voyage-2.5=ReadTimeout")

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
                "main_category_name": "Automobiles",
                "subcategory_name": "Car",
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
            "inferred_categories": {
                "main_category": "Automobiles",
                "subcategory": "Car",
            },
            "execution_path": "semantic",
            "sort_order": None,
        },
        "resolved_filters": {"categorical": {"city_name": "Chennai"}},
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


def test_semantic_tail_anchors_to_top_ranked_category_before_broad_candidates(
    tmp_path,
    monkeypatch,
):
    index = build_index(tmp_path / "dynamic-category-tail.sqlite3")
    engine = ProductSearchEngine(
        collection=FakeCollection(),
        bm25_index=index,
        semantic_related_tail_enabled=True,
    )

    def candidate(doc_id, product_id, subcategory):
        return {
            "id": doc_id,
            "text": f"candidate {product_id}",
            "metadata": {
                "source_type": "mysql",
                "source_table": engine.search_table,
                engine.search_id_column: product_id,
                "main_category_name": "Personal & Home Services",
                "subcategory_name": subcategory,
            },
        }

    plumber = candidate("plumber-ranked", 101, "Plumber")
    plumber_tail = candidate("plumber-tail", 102, "Plumber")
    appliance_tail = candidate(
        "appliance-tail",
        199,
        "Refrigerator Repair and Servicemen",
    )
    planned = {
        "query_plan": {
            "semantic_query": "repair leaking pipes",
            "keyword_query": "repair leaking pipes",
            "target_ad_type": "offer",
            "inferred_categories": {
                "main_category": None,
                "subcategory": None,
            },
            "relaxed_categories": [],
            "execution_path": "semantic",
            "sort_order": None,
        },
        "resolved_filters": {"categorical": {"city_name": "Chennai"}},
        "unresolved_filters": {},
    }
    monkeypatch.setattr(
        engine,
        "retrieve",
        lambda *_args, **_kwargs: {
            "vector_results": [],
            "bm25_results": [],
            "candidates": [plumber, appliance_tail],
            "hybrid_tail_candidates": [plumber_tail, appliance_tail],
            "vector_seconds": 0.0,
            "bm25_seconds": 0.0,
            "embedding_model_metrics": {},
        },
    )
    monkeypatch.setattr(
        engine,
        "rank",
        lambda *_args, **_kwargs: {
            "results": [plumber],
            "load_seconds": 0.0,
            "seconds": 0.0,
            "provider": "test",
            "attempts": [],
        },
    )
    captured = {}

    def catalogue_tail(*args, **_kwargs):
        captured["inferred_categories"] = args[2]
        captured["limit"] = args[4]
        return [103, 104]

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
        "someone who can repair leaking pipes at my house",
        limit=4,
        planned_result=planned,
        ranking_window=20,
    )

    assert result["product_ids"] == [101, 102, 103, 104]
    assert 199 not in result["product_ids"]
    assert captured["inferred_categories"] == {
        "main_category": "Personal & Home Services",
        "subcategory": "Plumber",
    }
    assert captured["limit"] == 2
    index.close()


def test_semantic_price_sort_keeps_ranked_results_before_related_tail(
    tmp_path,
    monkeypatch,
):
    index = build_index(tmp_path / "tier-aware-price-sort.sqlite3")
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

    ranked_expensive = candidate("ranked-expensive", 101)
    ranked_cheap = candidate("ranked-cheap", 102)
    unrelated_cheapest = candidate("related-cheapest", 199)
    planned = {
        "query_plan": {
            "semantic_query": "body massage",
            "keyword_query": "body massage",
            "target_ad_type": "offer",
            "inferred_categories": {},
            "relaxed_categories": [],
            "execution_path": "semantic",
            "sort_order": "price_asc",
        },
        "resolved_filters": {"categorical": {"city_name": "Chennai"}},
        "unresolved_filters": {},
    }
    monkeypatch.setattr(
        engine,
        "retrieve",
        lambda *_args, **_kwargs: {
            "vector_results": [],
            "bm25_results": [],
            "candidates": [ranked_expensive, ranked_cheap],
            "hybrid_tail_candidates": [unrelated_cheapest],
            "vector_seconds": 0.0,
            "bm25_seconds": 0.0,
            "embedding_model_metrics": {},
        },
    )
    monkeypatch.setattr(
        engine,
        "rank",
        lambda *_args, **_kwargs: {
            "results": [ranked_expensive, ranked_cheap],
            "load_seconds": 0.0,
            "seconds": 0.0,
            "provider": "test",
            "attempts": [],
        },
    )
    monkeypatch.setattr(
        engine,
        "_fetch_products",
        lambda ids: [
            {"id": product_id, "rental_fee": {101: 500, 102: 100, 199: 10}[product_id]}
            for product_id in ids
        ],
    )

    result = engine.search(
        "low cost body massage near me",
        limit=3,
        planned_result=planned,
        ranking_window=20,
    )

    assert result["product_ids"] == [102, 101, 199]
    assert [product["result_tier"] for product in result["products"]] == [
        "ranked",
        "ranked",
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
