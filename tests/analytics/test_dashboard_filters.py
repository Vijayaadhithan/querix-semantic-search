from datetime import UTC, datetime

import pytest

from analytics_service.dashboard_filters import (
    DashboardFilters,
    build_dashboard_overview,
)
from analytics_service.domain.search.classification import detect_language


def _record(
    request_id,
    created_at,
    *,
    outcome="fulfilled",
    city="Chennai",
    category="Vehicles",
    path="semantic",
    attempts=(),
    request_kind="text_search",
):
    return {
        "request_id": request_id,
        "created_at": created_at,
        "normalized_query": "bike" if request_kind == "text_search" else "",
        "request_kind": request_kind,
        "outcome": outcome,
        "language": "English",
        "categories": [category],
        "filters": {
            "city": city,
            "city_id": 456 if city == "Chennai" else 789,
            "target_ad_type": "offer",
        },
        "search": {"result_count": 20, "total_results": 50},
        "performance": {
            "execution_path": path,
            "total_server_duration_ms": 1000,
            "downstream_api_calls": len(attempts),
            "cache": {"plan_hit": False, "result_hit": False},
            "stages_ms": {"planning_ms": 300, "retrieval_ms": 400},
        },
        "api": {"status": "success"},
        "attempts": list(attempts),
    }


def test_company_dashboard_filters_actual_city_and_period():
    records = [
        _record("one", "2026-08-07T10:00:00+00:00"),
        _record(
            "two",
            "2026-08-01T10:00:00+00:00",
            city="Coimbatore",
        ),
    ]

    payload = build_dashboard_overview(
        records,
        internal=False,
        filters=DashboardFilters(period="24h", city_id=456),
        timezone_name="Asia/Kolkata",
        now=datetime(2026, 8, 7, 12, tzinfo=UTC),
    )

    assert payload["filtering"]["matched_records"] == 1
    assert payload["filtering"]["available"]["city_options"] == [
        {"id": 456, "label": "Chennai"},
        {"id": 789, "label": "Coimbatore"},
    ]
    assert payload["filtered_overview"]["summary"]["searches"] == 1
    assert payload["filtered_overview"]["main_graph"]["timezone"] == ("Asia/Kolkata")


def test_company_overview_separates_text_searches_from_catalogue_browsing():
    records = [
        _record("text", "2026-08-07T10:00:00+00:00", outcome="zero_result"),
        _record(
            "browse",
            "2026-08-07T10:01:00+00:00",
            outcome="fulfilled",
            request_kind="filtered_browse",
        ),
    ]

    payload = build_dashboard_overview(
        records,
        internal=False,
        filters=DashboardFilters(),
        timezone_name="UTC",
    )

    overview = payload["filtered_overview"]
    assert overview["summary"]["all_requests"] == 2
    assert overview["summary"]["searches"] == 1
    assert overview["summary"]["browse_requests"] == 1
    assert overview["summary"]["zero_results"] == 1
    assert overview["summary"]["browse_fulfilled"] == 1
    assert overview["breakdowns"]["outcomes"]["labels"] == ["zero_result"]
    series = {item["name"]: item["values"] for item in overview["main_graph"]["series"]}
    assert series["Text searches"] == [1]
    assert series["Catalogue/filter browse"] == [1]


@pytest.mark.parametrize(
    ("request_scope", "searches", "browse_requests"),
    (("text_search", 1, 0), ("browse", 0, 1)),
)
def test_dashboard_request_scope_selects_text_or_browse_activity(
    request_scope,
    searches,
    browse_requests,
):
    records = [
        _record("text", "2026-08-07T10:00:00+00:00"),
        _record(
            "browse",
            "2026-08-07T10:01:00+00:00",
            request_kind="catalogue_browse",
        ),
    ]

    payload = build_dashboard_overview(
        records,
        internal=False,
        filters=DashboardFilters(request_scope=request_scope),
        timezone_name="UTC",
    )

    assert payload["filtering"]["applied"]["request_scope"] == request_scope
    assert payload["filtering"]["matched_records"] == 1
    assert payload["filtered_overview"]["summary"]["searches"] == searches
    assert payload["filtered_overview"]["summary"]["browse_requests"] == browse_requests


def test_dashboard_rejects_unknown_request_scope():
    with pytest.raises(ValueError, match="request scope"):
        build_dashboard_overview(
            [],
            internal=False,
            filters=DashboardFilters(request_scope="unknown"),
            timezone_name="UTC",
        )


def test_internal_dashboard_splits_llm_and_reranker_tokens():
    records = [
        _record(
            "one",
            "2026-08-07T10:00:00+00:00",
            attempts=(
                {
                    "provider": "gemini",
                    "operation": "query_planning",
                    "api_calls": 1,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
                {
                    "provider": "voyage",
                    "operation": "reranking",
                    "api_calls": 1,
                    "input_tokens": 400,
                    "total_tokens": 400,
                },
            ),
        )
    ]

    payload = build_dashboard_overview(
        records,
        internal=True,
        filters=DashboardFilters(provider="voyage"),
        timezone_name="UTC",
    )
    usage = payload["filtered_overview"]["token_usage_by_operation"]["data"]

    assert payload["filtering"]["matched_records"] == 1
    assert usage["llm"]["total_tokens"] == 0
    assert usage["reranker"]["total_tokens"] == 400
    assert usage["reranker"]["api_calls"] == 1


def test_custom_period_requires_boundary():
    with pytest.raises(ValueError, match="requires from and/or to"):
        build_dashboard_overview(
            [],
            internal=False,
            filters=DashboardFilters(period="custom"),
            timezone_name="UTC",
        )


def test_language_detection_uses_cautious_latin_tamil_label():
    assert detect_language("enakku bike venum") == "Likely Tamil (Latin script)"
    assert detect_language("house in anna nagar") == "English"
