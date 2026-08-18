import pytest

from search.policy import DefaultSearchPolicy
from search.policy_registry import build_search_policy
from tenants.gainr.policy import GainrSearchPolicy

QUERY_PLAN = {
    "semantic_query": "vehicle for long distance with comfort and safety",
    "keyword_query": "vehicle long distance comfort safety",
}
CANDIDATES = [
    {
        "id": "service",
        "text": "Car detailing service",
        "metadata": {"main_category_name": "Services"},
        "fusion_score": 0.05,
    },
    {
        "id": "driver",
        "text": "Acting driver with car",
        "metadata": {"main_category_name": "Automobiles"},
        "fusion_score": 0.03,
    },
]


def test_default_policy_does_not_apply_gainr_behavior():
    policy = DefaultSearchPolicy()

    assert policy.category_intent("body massage", {}) is None
    assert policy.adjust_candidates(QUERY_PLAN, CANDIDATES) is CANDIDATES
    assert policy.rerank_context(QUERY_PLAN) is None
    assert (
        policy.rewrite_keyword_query(
            QUERY_PLAN["semantic_query"],
            QUERY_PLAN["keyword_query"],
        )
        == QUERY_PLAN["keyword_query"]
    )


def test_gainr_policy_is_selected_explicitly():
    policy = build_search_policy("gainr")

    assert isinstance(policy, GainrSearchPolicy)
    assert policy.adjust_candidates(QUERY_PLAN, CANDIDATES)[0]["id"] == "driver"
    assert policy.rerank_context(QUERY_PLAN)


def test_gainr_academic_teacher_overrides_subject_product_category():
    policy = GainrSearchPolicy()

    intent = policy.category_intent(
        "mathematics teacher",
        {"mathematics": "Mathematics", "teacher": "Teacher"},
    )

    assert intent is not None
    assert intent.subcategory == "Teacher"
    assert intent.override_explicit_conflict is True


@pytest.mark.parametrize(
    "query",
    [
        "Biryani master for mutton biryani",
        "Briyani master for chicken briyani",
        "Mutton biriyani master for home event",
    ],
)
def test_gainr_biriyani_master_variants_keep_the_specific_role(query):
    intent = GainrSearchPolicy().category_intent(
        query,
        {"master": "Master", "biriyani master": "Biriyani Master"},
    )

    assert intent is not None
    assert intent.subcategory == "Biriyani Master"
    assert intent.override_explicit_conflict is True


@pytest.mark.parametrize(
    "query",
    [
        "Car mechanic for road side assistance",
        "car repair service at home",
        "mechanic for road side assitance",
    ],
)
def test_gainr_automotive_mechanic_language_overrides_the_car_product(query):
    intent = GainrSearchPolicy().category_intent(
        query,
        {"car": "Car", "mechanic": "Mechanic"},
    )

    assert intent is not None
    assert intent.subcategory == "Mechanic"
    assert intent.main_category == "Automotive Professionals"
    assert intent.override_explicit_conflict is True


def test_unknown_policy_is_rejected_without_falling_back():
    with pytest.raises(ValueError, match="Unsupported search policy"):
        build_search_policy("unknown")
