from types import SimpleNamespace

from analytics_service.adapters import build_analytics_adapter
from tenants.gainr.analytics import GainrAnalyticsAdapter


def test_gainr_analytics_adapter_is_loaded_from_gainr_plugin():
    company = SimpleNamespace(company_id="gainr")
    adapter = build_analytics_adapter("gainr", company)
    payload = {"company_id": "gainr", "overview": {"searches": 3}}

    assert isinstance(adapter, GainrAnalyticsAdapter)
    assert adapter.dashboard_response(payload) is payload
