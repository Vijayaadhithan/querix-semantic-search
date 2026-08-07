from datetime import UTC, datetime

import pytest

from analytics_service.dashboard_filters import (
    DashboardFilters,
    build_dashboard_overview,
)


def _record(
    request_id,
    created_at,
    *,
    outcome="fulfilled",
    city="Chennai",
    category="Vehicles",
    path="semantic",
    attempts=(),
):
    return {
        "request_id": request_id,
        "created_at": created_at,
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
