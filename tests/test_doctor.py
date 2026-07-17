import pytest

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
