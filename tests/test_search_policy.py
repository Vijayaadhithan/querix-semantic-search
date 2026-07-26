import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gainr_search_policy import GainrSearchPolicy
from search_policy import DefaultSearchPolicy
from tenant_search_policies import build_search_policy


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


def test_unknown_policy_is_rejected_without_falling_back():
    with pytest.raises(ValueError, match="Unsupported search policy"):
        build_search_policy("unknown")
