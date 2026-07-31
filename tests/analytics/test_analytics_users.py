from __future__ import annotations

import stat

import pytest
from dotenv import dotenv_values

from analytics_service.auth import COMPANY_USER, AnalyticsAuthStore
from analytics_service.users import (
    CREDENTIAL_COMPANY_ENV,
    CREDENTIAL_PASSWORD_ENV,
    CREDENTIAL_ROLE_ENV,
    CREDENTIAL_USERNAME_ENV,
    CredentialRecord,
    _credential_record_from_env,
    _generate_credentials_file,
    _sync_credential_record,
    main,
)


def test_generate_credentials_creates_exclusive_mode_0600_file(
    tmp_path,
    capsys,
):
    path = tmp_path / ".env.analytics.gainr.credentials"
    result = main(
        [
            "generate-credentials",
            "--file",
            str(path),
            "--username",
            "generated-test-user",
            "--role",
            COMPANY_USER,
            "--company",
            "gainr",
        ]
    )
    output = capsys.readouterr().out
    values = dotenv_values(path)

    assert result == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert values[CREDENTIAL_USERNAME_ENV] == "generated-test-user"
    assert values[CREDENTIAL_ROLE_ENV] == COMPANY_USER
    assert values[CREDENTIAL_COMPANY_ENV] == "gainr"
    assert len(values[CREDENTIAL_PASSWORD_ENV] or "") >= 40
    assert values[CREDENTIAL_PASSWORD_ENV] not in output
    assert "generated-test-user" not in output

    with pytest.raises(FileExistsError):
        _generate_credentials_file(
            path,
            username="replacement-test-user",
            role=COMPANY_USER,
            company_id="gainr",
        )
    assert dotenv_values(path) == values

    _generate_credentials_file(
        path,
        username="generated-test-user",
        role=COMPANY_USER,
        company_id="gainr",
        replace=True,
    )
    rotated_values = dotenv_values(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert rotated_values[CREDENTIAL_PASSWORD_ENV] != (
        values[CREDENTIAL_PASSWORD_ENV]
    )


def test_credential_environment_validation(monkeypatch):
    monkeypatch.setenv(CREDENTIAL_USERNAME_ENV, "company-test-user")
    monkeypatch.setenv(
        CREDENTIAL_PASSWORD_ENV,
        "test-only-company-password-from-env",
    )
    monkeypatch.setenv(CREDENTIAL_ROLE_ENV, COMPANY_USER)
    monkeypatch.setenv(CREDENTIAL_COMPANY_ENV, "GaInR")

    record = _credential_record_from_env()
    assert record.username == "company-test-user"
    assert record.company_id == "gainr"

    monkeypatch.delenv(CREDENTIAL_PASSWORD_ENV)
    with pytest.raises(ValueError, match=CREDENTIAL_PASSWORD_ENV):
        _credential_record_from_env()


def test_sync_credentials_hashes_password_and_revokes_prior_session(tmp_path):
    store = AnalyticsAuthStore(
        tmp_path / "analytics.sqlite3",
        password_min_length=15,
    )
    initial = CredentialRecord(
        username="sync-test-user",
        password="test-only-initial-sync-password",
        role=COMPANY_USER,
        company_id="gainr",
    )
    assert _sync_credential_record(store, initial) == "created"
    session = store.authenticate(
        username=initial.username,
        password=initial.password,
        required_role=COMPANY_USER,
    )
    assert session is not None

    rotated = CredentialRecord(
        username=initial.username,
        password="test-only-rotated-sync-password",
        role=COMPANY_USER,
        company_id="gainr",
    )
    assert _sync_credential_record(store, rotated) == "updated"
    assert store.resolve_session(session.token) is None
    assert store.authenticate(
        username=rotated.username,
        password=initial.password,
        required_role=COMPANY_USER,
    ) is None
    assert store.authenticate(
        username=rotated.username,
        password=rotated.password,
        required_role=COMPANY_USER,
    ) is not None

    wrong_binding = CredentialRecord(
        username=initial.username,
        password="test-only-wrong-binding-password",
        role=COMPANY_USER,
        company_id="acme",
    )
    with pytest.raises(ValueError, match="binding"):
        _sync_credential_record(store, wrong_binding)
