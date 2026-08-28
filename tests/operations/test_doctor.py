import pytest

from scripts import doctor
from scripts.doctor import production_database_tls_status


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_production_database_tls_accepts_encrypted_modes(mode):
    ok, detail = production_database_tls_status(mode)

    assert ok is True
    assert "encrypted" in detail


def test_production_database_tls_rejects_disabled_mode():
    ok, detail = production_database_tls_status("disable")

    assert ok is False
    assert "expected require or stronger" in detail


def test_daily_spool_doctor_does_not_need_remote_telemetry_access(monkeypatch):
    class Analytics:
        enabled = True

    class Profile:
        company_id = "tenant-a"
        analytics = Analytics()

    monkeypatch.setattr(doctor, "SEARCH_ANALYTICS_DELIVERY_MODE", "daily_spool")
    monkeypatch.setattr(
        doctor,
        "search_analytics_spool_status",
        lambda *_args, **_kwargs: {"pending": 2, "spool_bytes": 512},
    )
    monkeypatch.setattr(
        doctor,
        "search_analytics_schema_status",
        lambda *_args, **_kwargs: pytest.fail("remote schema should not be queried"),
    )

    assert doctor.check_search_analytics(Profile()) is True
