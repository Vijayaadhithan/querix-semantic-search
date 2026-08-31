from types import SimpleNamespace

import verticals.marketplace.analytics.adapter as marketplace_adapter
from analytics_service.adapters import build_analytics_adapter
from tenants.gainr.analytics import GainrAnalyticsAdapter
from tests.analytics.test_analytics_service import analytics_data


def test_gainr_analytics_adapter_is_loaded_from_gainr_plugin():
    company = SimpleNamespace(company_id="gainr")
    adapter = build_analytics_adapter("gainr", company)
    payload = {"company_id": "gainr", "overview": {"searches": 3}}

    assert isinstance(adapter, GainrAnalyticsAdapter)
    assert adapter.dashboard_response(payload) is payload


def test_gainr_adapter_builds_only_selected_metric_modules(monkeypatch):
    company = SimpleNamespace(company_id="gainr")
    adapter = build_analytics_adapter("gainr", company)

    def unexpected(*args, **kwargs):
        del args, kwargs
        raise AssertionError("unselected marketplace module was invoked")

    monkeypatch.setattr(marketplace_adapter, "process_part_a", unexpected)
    monkeypatch.setattr(marketplace_adapter, "process_part_c", unexpected)
    monkeypatch.setattr(marketplace_adapter, "process_part_d", unexpected)

    computation = adapter.build_computation(
        analytics_data(),
        frozenset({"api_performance"}),
    )

    assert tuple(computation.reports) == ("api_performance",)
