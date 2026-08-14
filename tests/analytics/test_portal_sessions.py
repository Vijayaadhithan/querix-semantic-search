from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from analytics_service.api import create_app
from analytics_service.auth import (
    COMPANY_PORTAL,
    COMPANY_USER,
    INTERNAL_ADMIN,
    AnalyticsAuthStore,
)
from analytics_service.config import (
    AnalyticsRegistry,
    AnalyticsSettings,
    CompanyAnalyticsConfig,
    DatabaseTarget,
)

COMPANY_COOKIE = "__Host-querix_company_analytics"
INTERNAL_COOKIE = "__Host-querix_internal_analytics"
LEGACY_COOKIE = "querix_analytics_session"
ALLOWED_ORIGIN = "https://querix.co"
COMPANY_PASSWORD = "test-only-company-password"
INTERNAL_PASSWORD = "test-only-internal-password"


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class StubSnapshotStore:
    def __init__(self):
        self.last_query_kwargs = None

    def company_status(self, company_id: str):
        return {
            "company_id": company_id,
            "has_snapshot": True,
            "snapshot": {"generated_at": "2026-07-31T00:00:00+00:00"},
            "latest_run": None,
            "refresh_schedule": "every 2 hours at :30 Asia/Kolkata",
        }

    def dashboard(self, company_id: str, *, internal: bool):
        return {
            "metadata": {
                "company_id": company_id,
                "audience": "internal" if internal else "company",
            }
        }

    def dashboard_activity_records(self, company_id: str, *, internal: bool):
        return ()

    def query_records(self, company_id: str, **kwargs):
        self.last_query_kwargs = kwargs
        return {
            "company_id": company_id,
            "items": [],
            "returned": 0,
            "has_more": False,
            "next_cursor": None,
        }


def company_config(tmp_path: Path, company_id: str):
    database = DatabaseTarget(
        backend="mysql",
        host="database.test",
        port=3306,
        database="catalog",
        user="readonly",
        password="test-only-database-placeholder",
        tls_mode="disable",
    )
    return CompanyAnalyticsConfig(
        company_id=company_id,
        endpoint_slug=company_id,
        api_key_envs=(f"{company_id.upper()}_ANALYTICS_API_KEY",),
        database=database,
        telemetry_database=database,
        datasets={},
        config_path=tmp_path / f"{company_id}.yaml",
    )


@pytest.fixture
def portal_app(tmp_path):
    clock = MutableClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))
    settings = AnalyticsSettings(
        host="127.0.0.1",
        port=8010,
        snapshot_db_path=tmp_path / "analytics.sqlite3",
        tenant_config_dir=tmp_path,
        cors_origins=(ALLOWED_ORIGIN,),
        query_page_size=50,
        query_max_page_size=200,
        session_cookie_name=LEGACY_COOKIE,
        company_session_cookie_name=COMPANY_COOKIE,
        internal_session_cookie_name=INTERNAL_COOKIE,
        company_session_idle_seconds=86_400,
        company_session_absolute_seconds=604_800,
        internal_session_idle_seconds=28_800,
        internal_session_absolute_seconds=43_200,
        session_cookie_secure=True,
    )
    auth_store = AnalyticsAuthStore(
        settings.snapshot_db_path,
        session_ttl_seconds=settings.session_ttl_seconds,
        company_session_idle_seconds=settings.company_session_idle_seconds,
        company_session_absolute_seconds=(settings.company_session_absolute_seconds),
        internal_session_idle_seconds=settings.internal_session_idle_seconds,
        internal_session_absolute_seconds=(settings.internal_session_absolute_seconds),
        password_min_length=settings.password_min_length,
        clock=clock,
    )
    auth_store.create_user(
        username="test-company-user",
        password=COMPANY_PASSWORD,
        role=COMPANY_USER,
        company_id="gainr",
    )
    auth_store.create_user(
        username="test-internal-user",
        password=INTERNAL_PASSWORD,
        role=INTERNAL_ADMIN,
    )
    registry = AnalyticsRegistry(
        {
            "gainr": company_config(tmp_path, "gainr"),
            "acme": company_config(tmp_path, "acme"),
        }
    )
    snapshot_store = StubSnapshotStore()
    app = create_app(
        settings=settings,
        registry=registry,
        store=snapshot_store,
        auth_store=auth_store,
    )
    return app, auth_store, snapshot_store


def login(
    client: TestClient,
    portal: str,
    username: str,
    password: str,
):
    return client.post(
        f"/api/v1/analytics/{portal}/auth/login",
        headers={"Origin": ALLOWED_ORIGIN},
        json={"username": username, "password": password},
    )


def test_role_logins_set_only_their_secure_host_cookie(portal_app):
    app, _, _ = portal_app
    with TestClient(app, base_url="https://api.test") as company_client:
        company = login(
            company_client,
            "company",
            "test-company-user",
            COMPANY_PASSWORD,
        )
        set_cookies = company.headers.get_list("set-cookie")
        assert company.status_code == 200
        assert company.json()["user"] == {
            "username": "test-company-user",
            "role": COMPANY_USER,
            "company_id": "gainr",
        }
        assert len(set_cookies) == 1
        assert set_cookies[0].startswith(f"{COMPANY_COOKIE}=")
        assert "Secure" in set_cookies[0]
        assert "HttpOnly" in set_cookies[0]
        assert "SameSite=lax" in set_cookies[0]
        assert "Path=/" in set_cookies[0]
        assert "Max-Age=86400" in set_cookies[0]
        assert "Domain=" not in set_cookies[0]
        assert INTERNAL_COOKIE not in set_cookies[0]
        assert LEGACY_COOKIE not in set_cookies[0]

    with TestClient(app, base_url="https://api.test") as internal_client:
        internal = login(
            internal_client,
            "internal",
            "test-internal-user",
            INTERNAL_PASSWORD,
        )
        set_cookies = internal.headers.get_list("set-cookie")
        assert internal.status_code == 200
        assert internal.json()["user"] == {
            "username": "test-internal-user",
            "role": INTERNAL_ADMIN,
            "company_id": None,
        }
        assert len(set_cookies) == 1
        assert set_cookies[0].startswith(f"{INTERNAL_COOKIE}=")
        assert "Max-Age=28800" in set_cookies[0]
        assert COMPANY_COOKIE not in set_cookies[0]


def test_session_environment_policy_is_validated(monkeypatch):
    monkeypatch.setenv(
        "ANALYTICS_COMPANY_SESSION_COOKIE_NAME",
        COMPANY_COOKIE,
    )
    monkeypatch.setenv(
        "ANALYTICS_INTERNAL_SESSION_COOKIE_NAME",
        INTERNAL_COOKIE,
    )
    monkeypatch.setenv("ANALYTICS_COMPANY_SESSION_IDLE_SECONDS", "86400")
    monkeypatch.setenv(
        "ANALYTICS_COMPANY_SESSION_ABSOLUTE_SECONDS",
        "604800",
    )
    monkeypatch.setenv("ANALYTICS_INTERNAL_SESSION_IDLE_SECONDS", "28800")
    monkeypatch.setenv(
        "ANALYTICS_INTERNAL_SESSION_ABSOLUTE_SECONDS",
        "43200",
    )
    settings = AnalyticsSettings.from_env()
    assert settings.company_session_cookie_name == COMPANY_COOKIE
    assert settings.internal_session_cookie_name == INTERNAL_COOKIE
    assert settings.company_session_idle_seconds == 86_400
    assert settings.company_session_absolute_seconds == 604_800
    assert settings.internal_session_idle_seconds == 28_800
    assert settings.internal_session_absolute_seconds == 43_200

    monkeypatch.setenv("ANALYTICS_INTERNAL_SESSION_IDLE_SECONDS", "50000")
    with pytest.raises(ValueError, match="internal analytics absolute"):
        AnalyticsSettings.from_env()


def test_role_mismatch_invalid_credentials_origin_and_preflight(portal_app):
    app, _, _ = portal_app
    with TestClient(app, base_url="https://api.test") as client:
        wrong_password = login(
            client,
            "company",
            "test-company-user",
            "not-the-test-password",
        )
        unknown_user = login(
            client,
            "company",
            "missing-test-user",
            "not-the-test-password",
        )
        wrong_role_company = login(
            client,
            "company",
            "test-internal-user",
            INTERNAL_PASSWORD,
        )
        wrong_role_internal = login(
            client,
            "internal",
            "test-company-user",
            COMPANY_PASSWORD,
        )
        disallowed_origin = client.post(
            "/api/v1/analytics/company/auth/login",
            headers={"Origin": "https://untrusted.test"},
            json={
                "username": "test-company-user",
                "password": COMPANY_PASSWORD,
            },
        )
        preflight = client.options(
            "/api/v1/analytics/company/auth/login",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        api_key_is_not_browser_auth = client.get(
            "/api/v1/analytics/company/auth/me",
            headers={"X-API-Key": "test-only-placeholder"},
        )

    expected = {"detail": "Invalid username or password."}
    assert wrong_password.status_code == 401
    assert unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json() == expected
    assert wrong_role_company.status_code == 401
    assert wrong_role_company.json() == expected
    assert wrong_role_internal.status_code == 401
    assert wrong_role_internal.json() == expected
    assert disallowed_origin.status_code == 403
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert preflight.headers["access-control-allow-credentials"] == "true"
    assert api_key_is_not_browser_auth.status_code == 401


def test_both_sessions_coexist_and_logout_is_portal_specific(portal_app):
    app, _, _ = portal_app
    with TestClient(app, base_url="https://api.test") as client:
        assert (
            login(
                client,
                "company",
                "test-company-user",
                COMPANY_PASSWORD,
            ).status_code
            == 200
        )
        assert (
            login(
                client,
                "internal",
                "test-internal-user",
                INTERNAL_PASSWORD,
            ).status_code
            == 200
        )
        assert client.cookies.get(COMPANY_COOKIE)
        assert client.cookies.get(INTERNAL_COOKIE)

        assert client.get("/api/v1/analytics/company/auth/me").status_code == 200
        assert client.get("/api/v1/analytics/internal/auth/me").status_code == 200
        assert client.get("/api/v1/gainr/analytics/dashboard").status_code == 200
        assert client.get("/api/v1/admin/analytics/gainr/dashboard").status_code == 200

        company_logout = client.post(
            "/api/v1/analytics/company/auth/logout",
            headers={"Origin": ALLOWED_ORIGIN},
        )
        assert company_logout.status_code == 200
        assert client.get("/api/v1/analytics/company/auth/me").status_code == 401
        assert client.get("/api/v1/analytics/internal/auth/me").status_code == 200
        assert (
            client.post(
                "/api/v1/analytics/company/auth/logout",
                headers={"Origin": ALLOWED_ORIGIN},
            ).status_code
            == 200
        )

        assert (
            login(
                client,
                "company",
                "test-company-user",
                COMPANY_PASSWORD,
            ).status_code
            == 200
        )
        assert client.get("/api/v1/analytics/internal/auth/me").status_code == 200
        internal_logout = client.post(
            "/api/v1/analytics/internal/auth/logout",
            headers={"Origin": ALLOWED_ORIGIN},
        )
        assert internal_logout.status_code == 200
        assert client.get("/api/v1/analytics/company/auth/me").status_code == 200
        assert client.get("/api/v1/analytics/internal/auth/me").status_code == 401


def test_each_route_reads_only_its_portal_cookie_and_enforces_tenant(portal_app):
    app, _, snapshot_store = portal_app
    with TestClient(app, base_url="https://api.test") as company_client:
        assert (
            login(
                company_client,
                "company",
                "test-company-user",
                COMPANY_PASSWORD,
            ).status_code
            == 200
        )
        company_token = company_client.cookies.get(COMPANY_COOKIE)
        assert company_client.get("/api/v1/acme/analytics/dashboard").status_code == 403
        assert (
            company_client.get("/api/v1/admin/analytics/gainr/dashboard").status_code
            == 401
        )

    with TestClient(app, base_url="https://api.test") as internal_client:
        assert (
            login(
                internal_client,
                "internal",
                "test-internal-user",
                INTERNAL_PASSWORD,
            ).status_code
            == 200
        )
        internal_token = internal_client.cookies.get(INTERNAL_COOKIE)
        assert (
            internal_client.get("/api/v1/admin/analytics/companies").status_code == 200
        )
        assert (
            internal_client.get(
                "/api/v1/admin/analytics/gainr/queries",
                params={"execution_path": "direct_semantic"},
            ).status_code
            == 200
        )
        assert snapshot_store.last_query_kwargs["execution_path"] == "direct_semantic"
        assert (
            internal_client.get("/api/v1/gainr/analytics/dashboard").status_code == 401
        )

    with TestClient(app, base_url="https://api.test") as isolated_client:
        internal_only = isolated_client.get(
            "/api/v1/analytics/company/auth/me",
            headers={"Cookie": f"{INTERNAL_COOKIE}={internal_token}"},
        )
        company_only = isolated_client.get(
            "/api/v1/analytics/internal/auth/me",
            headers={"Cookie": f"{COMPANY_COOKIE}={company_token}"},
        )
        assert internal_only.status_code == 401
        assert company_only.status_code == 401


def test_idle_expiration_slides_but_never_past_absolute_timeout(tmp_path):
    clock = MutableClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))
    store = AnalyticsAuthStore(
        tmp_path / "sessions.sqlite3",
        company_session_idle_seconds=100,
        company_session_absolute_seconds=250,
        internal_session_idle_seconds=80,
        internal_session_absolute_seconds=120,
        password_min_length=15,
        clock=clock,
    )
    store.create_user(
        username="sliding-test-user",
        password="test-only-sliding-password",
        role=COMPANY_USER,
        company_id="gainr",
    )
    authenticated = store.authenticate(
        username="sliding-test-user",
        password="test-only-sliding-password",
        required_role=COMPANY_USER,
    )
    assert authenticated is not None
    assert (
        authenticated.principal.session_expires_at
        == (clock.value + timedelta(seconds=100)).isoformat()
    )

    clock.advance(50)
    first_slide = store.resolve_session(
        authenticated.token,
        portal_type=COMPANY_PORTAL,
    )
    assert first_slide is not None
    assert (
        first_slide.session_expires_at
        == (clock.value + timedelta(seconds=100)).isoformat()
    )

    clock.advance(99)
    second_slide = store.resolve_session(
        authenticated.token,
        portal_type=COMPANY_PORTAL,
    )
    assert second_slide is not None
    clock.advance(99)
    absolute_slide = store.resolve_session(
        authenticated.token,
        portal_type=COMPANY_PORTAL,
    )
    assert absolute_slide is not None
    absolute_expiration = datetime(2026, 7, 31, 12, 0, tzinfo=UTC) + (
        timedelta(seconds=250)
    )
    assert absolute_slide.session_expires_at == absolute_expiration.isoformat()

    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM analytics_sessions").fetchone()
    assert row is not None
    assert row["portal_type"] == COMPANY_PORTAL
    assert row["role"] == COMPANY_USER
    assert row["company_id"] == "gainr"
    assert row["created_at"]
    assert row["last_seen_at"]
    assert row["idle_expires_at"] == absolute_expiration.isoformat()
    assert row["absolute_expires_at"] == absolute_expiration.isoformat()
    assert authenticated.token not in tuple(str(value) for value in row)

    clock.advance(3)
    assert (
        store.resolve_session(
            authenticated.token,
            portal_type=COMPANY_PORTAL,
        )
        is None
    )

    revoked = store.authenticate(
        username="sliding-test-user",
        password="test-only-sliding-password",
        required_role=COMPANY_USER,
    )
    assert revoked is not None
    store.revoke_session(revoked.token, portal_type=COMPANY_PORTAL)
    assert (
        store.resolve_session(
            revoked.token,
            portal_type=COMPANY_PORTAL,
        )
        is None
    )


def test_existing_session_table_is_migrated_additively(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE analytics_users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                username_normalized TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                company_id TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                password_changed_at TEXT NOT NULL,
                last_login_at TEXT
            );
            CREATE TABLE analytics_sessions (
                session_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                revoked_at TEXT
            );
            """
        )
        connection.execute(
            """
            INSERT INTO analytics_users (
                user_id, username, username_normalized, password_hash,
                role, company_id, created_at, updated_at,
                password_changed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-user-id",
                "legacy-test-user",
                "legacy-test-user",
                "not-a-real-password-hash",
                COMPANY_USER,
                "gainr",
                "2026-07-01T00:00:00+00:00",
                "2026-07-01T00:00:00+00:00",
                "2026-07-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO analytics_sessions (
                session_hash, user_id, created_at, expires_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "legacy-session-digest",
                "legacy-user-id",
                "2026-07-31T00:00:00+00:00",
                "2026-08-01T00:00:00+00:00",
                "2026-07-31T00:00:00+00:00",
            ),
        )

    AnalyticsAuthStore(path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(analytics_sessions)")
        }
        row = connection.execute("SELECT * FROM analytics_sessions").fetchone()
    assert {
        "portal_type",
        "role",
        "company_id",
        "idle_expires_at",
        "absolute_expires_at",
    } <= columns
    assert row is not None
    assert row["portal_type"] == COMPANY_PORTAL
    assert row["role"] == COMPANY_USER
    assert row["company_id"] == "gainr"
    assert row["idle_expires_at"] == row["expires_at"]
    assert row["absolute_expires_at"] == row["expires_at"]


def test_auth_store_failure_fails_closed(portal_app):
    app, _, _ = portal_app

    class UnavailableAuthStore:
        def authenticate(self, **_kwargs):
            raise RuntimeError("test session store unavailable")

        def resolve_session(self, *_args, **_kwargs):
            raise RuntimeError("test session store unavailable")

        def revoke_session(self, *_args, **_kwargs):
            raise RuntimeError("test session store unavailable")

    settings = app.state.settings
    unavailable_app = create_app(
        settings=settings,
        registry=app.state.registry,
        store=app.state.store,
        auth_store=UnavailableAuthStore(),
    )
    with TestClient(
        unavailable_app,
        base_url="https://api.test",
    ) as client:
        failed_login = login(
            client,
            "company",
            "test-company-user",
            COMPANY_PASSWORD,
        )
        failed_resolution = client.get(
            "/api/v1/analytics/company/auth/me",
            headers={"Cookie": f"{COMPANY_COOKIE}=opaque-test-value"},
        )
    assert failed_login.status_code == 503
    assert failed_resolution.status_code == 503


def test_legacy_auth_endpoints_remain_compatible(portal_app):
    app, _, _ = portal_app
    with TestClient(app, base_url="https://api.test") as client:
        legacy_login = client.post(
            "/api/v1/analytics/auth/login",
            headers={"Origin": ALLOWED_ORIGIN},
            json={
                "username": "test-company-user",
                "password": COMPANY_PASSWORD,
            },
        )
        cookies = legacy_login.headers.get_list("set-cookie")
        assert legacy_login.status_code == 200
        assert any(value.startswith(f"{LEGACY_COOKIE}=") for value in cookies)
        assert any(value.startswith(f"{COMPANY_COOKIE}=") for value in cookies)
        assert client.get("/api/v1/analytics/auth/me").status_code == 200
        assert client.get("/api/v1/gainr/analytics/dashboard").status_code == 200
        assert (
            client.post(
                "/api/v1/analytics/auth/logout",
                headers={"Origin": ALLOWED_ORIGIN},
            ).status_code
            == 200
        )
        assert client.get("/api/v1/analytics/auth/me").status_code == 401
        assert client.get("/api/v1/gainr/analytics/dashboard").status_code == 401
