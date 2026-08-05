from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .adapters import supported_analytics_adapters
from .metrics import validate_metric_profile

PROJECT_ROOT = Path(__file__).resolve().parents[2]
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT / ".env.keys", override=True)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _safe_identifier(value: Any, *, label: str) -> str:
    normalized = str(value or "").strip()
    if not IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{label} must be a safe SQL identifier")
    return normalized


@dataclass(frozen=True, slots=True)
class AnalyticsSettings:
    host: str
    port: int
    snapshot_db_path: Path
    tenant_config_dir: Path
    cors_origins: tuple[str, ...]
    query_page_size: int
    query_max_page_size: int
    session_cookie_name: str = "querix_analytics_session"
    session_ttl_seconds: int = 28_800
    company_session_cookie_name: str = (
        "__Host-querix_company_analytics"
    )
    internal_session_cookie_name: str = (
        "__Host-querix_internal_analytics"
    )
    company_session_idle_seconds: int = 86_400
    company_session_absolute_seconds: int = 604_800
    internal_session_idle_seconds: int = 28_800
    internal_session_absolute_seconds: int = 43_200
    session_cookie_secure: bool = True
    login_max_attempts: int = 5
    login_lock_seconds: int = 900
    password_min_length: int = 15

    @classmethod
    def from_env(cls) -> "AnalyticsSettings":
        raw_db_path = Path(
            os.getenv(
                "ANALYTICS_SNAPSHOT_DB_PATH",
                "storage/analytics/snapshots.sqlite3",
            )
        )
        snapshot_db_path = (
            raw_db_path
            if raw_db_path.is_absolute()
            else PROJECT_ROOT / raw_db_path
        )
        raw_config_dir = Path(
            os.getenv("ANALYTICS_TENANT_CONFIG_DIR", "configs/tenants")
        )
        tenant_config_dir = (
            raw_config_dir
            if raw_config_dir.is_absolute()
            else PROJECT_ROOT / raw_config_dir
        )
        port = int(os.getenv("ANALYTICS_API_PORT", "8010"))
        page_size = int(os.getenv("ANALYTICS_QUERY_PAGE_SIZE", "50"))
        max_page_size = int(os.getenv("ANALYTICS_QUERY_MAX_PAGE_SIZE", "200"))
        if not 1 <= port <= 65535:
            raise ValueError("ANALYTICS_API_PORT must be between 1 and 65535")
        if page_size <= 0 or max_page_size < page_size:
            raise ValueError("Invalid analytics query page-size configuration")
        cors_origins = tuple(
            origin.strip()
            for origin in os.getenv(
                "ANALYTICS_CORS_ORIGINS",
                "",
            ).split(",")
            if origin.strip()
        )
        if "*" in cors_origins:
            raise ValueError(
                "ANALYTICS_CORS_ORIGINS cannot contain '*' when "
                "credentialed sessions are enabled"
            )
        cookie_names = {
            "session_cookie_name": os.getenv(
                "ANALYTICS_SESSION_COOKIE_NAME",
                "querix_analytics_session",
            ).strip(),
            "company_session_cookie_name": os.getenv(
                "ANALYTICS_COMPANY_SESSION_COOKIE_NAME",
                "__Host-querix_company_analytics",
            ).strip(),
            "internal_session_cookie_name": os.getenv(
                "ANALYTICS_INTERNAL_SESSION_COOKIE_NAME",
                "__Host-querix_internal_analytics",
            ).strip(),
        }
        for field_name, cookie_name in cookie_names.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cookie_name):
                raise ValueError(f"{field_name} is invalid")
        if len(set(cookie_names.values())) != len(cookie_names):
            raise ValueError(
                "Analytics legacy, company, and internal cookie names "
                "must be distinct"
            )
        session_ttl = int(
            os.getenv("ANALYTICS_SESSION_TTL_SECONDS", "28800")
        )
        company_idle = int(
            os.getenv(
                "ANALYTICS_COMPANY_SESSION_IDLE_SECONDS",
                "86400",
            )
        )
        company_absolute = int(
            os.getenv(
                "ANALYTICS_COMPANY_SESSION_ABSOLUTE_SECONDS",
                "604800",
            )
        )
        internal_idle = int(
            os.getenv(
                "ANALYTICS_INTERNAL_SESSION_IDLE_SECONDS",
                "28800",
            )
        )
        internal_absolute = int(
            os.getenv(
                "ANALYTICS_INTERNAL_SESSION_ABSOLUTE_SECONDS",
                "43200",
            )
        )
        login_max_attempts = int(
            os.getenv("ANALYTICS_LOGIN_MAX_ATTEMPTS", "5")
        )
        login_lock_seconds = int(
            os.getenv("ANALYTICS_LOGIN_LOCK_SECONDS", "900")
        )
        password_min_length = int(
            os.getenv("ANALYTICS_PASSWORD_MIN_LENGTH", "15")
        )
        if not 300 <= session_ttl <= 86_400:
            raise ValueError(
                "ANALYTICS_SESSION_TTL_SECONDS must be between 300 and 86400"
            )
        for portal, idle_seconds, absolute_seconds in (
            ("company", company_idle, company_absolute),
            ("internal", internal_idle, internal_absolute),
        ):
            if not 300 <= idle_seconds <= 2_592_000:
                raise ValueError(
                    f"Invalid {portal} analytics idle expiration"
                )
            if not idle_seconds <= absolute_seconds <= 31_536_000:
                raise ValueError(
                    f"Invalid {portal} analytics absolute expiration"
                )
        if not 3 <= login_max_attempts <= 20:
            raise ValueError(
                "ANALYTICS_LOGIN_MAX_ATTEMPTS must be between 3 and 20"
            )
        if not 60 <= login_lock_seconds <= 86_400:
            raise ValueError(
                "ANALYTICS_LOGIN_LOCK_SECONDS must be between 60 and 86400"
            )
        if not 12 <= password_min_length <= 128:
            raise ValueError(
                "ANALYTICS_PASSWORD_MIN_LENGTH must be between 12 and 128"
            )
        return cls(
            host=os.getenv("ANALYTICS_API_HOST", "0.0.0.0"),
            port=port,
            snapshot_db_path=snapshot_db_path,
            tenant_config_dir=tenant_config_dir,
            cors_origins=cors_origins,
            query_page_size=page_size,
            query_max_page_size=max_page_size,
            session_cookie_name=cookie_names["session_cookie_name"],
            session_ttl_seconds=session_ttl,
            company_session_cookie_name=cookie_names[
                "company_session_cookie_name"
            ],
            internal_session_cookie_name=cookie_names[
                "internal_session_cookie_name"
            ],
            company_session_idle_seconds=company_idle,
            company_session_absolute_seconds=company_absolute,
            internal_session_idle_seconds=internal_idle,
            internal_session_absolute_seconds=internal_absolute,
            session_cookie_secure=_env_bool(
                "ANALYTICS_SESSION_COOKIE_SECURE",
                True,
            ),
            login_max_attempts=login_max_attempts,
            login_lock_seconds=login_lock_seconds,
            password_min_length=password_min_length,
        )


@dataclass(frozen=True, slots=True)
class DatabaseTarget:
    backend: str
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    schema: str = "public"
    connect_timeout_seconds: int = 10
    read_timeout_seconds: int = 600
    tls_mode: str = "prefer"
    tls_ca_file: str = ""
    tls_cert_file: str = ""
    tls_key_file: str = ""

    def __post_init__(self) -> None:
        if self.backend not in {"mysql", "postgres"}:
            raise ValueError(f"Unsupported analytics database {self.backend!r}")
        if not 1 <= self.port <= 65535:
            raise ValueError("Analytics database port is invalid")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("Analytics database timeouts must be positive")
        _safe_identifier(self.schema, label="Analytics database schema")
        allowed_tls_modes = (
            {"disable", "prefer", "require", "verify-ca", "verify-full"}
            if self.backend == "mysql"
            else {
                "disable",
                "allow",
                "prefer",
                "require",
                "verify-ca",
                "verify-full",
            }
        )
        if self.tls_mode not in allowed_tls_modes:
            raise ValueError(
                f"Unsupported {self.backend} TLS mode {self.tls_mode!r}"
            )

    @property
    def configured(self) -> bool:
        return bool(self.host and self.database and self.user)


@dataclass(frozen=True, slots=True)
class DatasetMapping:
    table: str
    columns: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _safe_identifier(self.table, label="Analytics table")
        for canonical, source in self.columns.items():
            _safe_identifier(canonical, label="Analytics canonical column")
            _safe_identifier(source, label="Analytics source column")


@dataclass(frozen=True, slots=True)
class CompanyAnalyticsConfig:
    company_id: str
    endpoint_slug: str
    api_key_envs: tuple[str, ...]
    database: DatabaseTarget
    telemetry_database: DatabaseTarget
    datasets: dict[str, DatasetMapping]
    config_path: Path
    adapter: str = "default"
    history_days: int = 90
    company_metric_profile: dict[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    internal_metric_profile: dict[str, tuple[str, ...]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        if not TENANT_ID_RE.fullmatch(self.company_id):
            raise ValueError(f"Unsafe company id {self.company_id!r}")
        if not TENANT_ID_RE.fullmatch(self.endpoint_slug):
            raise ValueError(f"Unsafe endpoint slug {self.endpoint_slug!r}")
        if not 1 <= self.history_days <= 3650:
            raise ValueError(
                "Analytics history_days must be between 1 and 3650"
            )
        if self.adapter not in supported_analytics_adapters():
            supported = ", ".join(supported_analytics_adapters())
            raise ValueError(
                f"Unsupported analytics adapter {self.adapter!r}; "
                f"supported adapters: {supported}"
            )


DEFAULT_TABLES = {
    "search_history": "semantic_search_history",
    "api_usage": "semantic_search_api_usage",
    "categories": "categories",
    "sub_categories": "sub_categories",
    "states": "states",
    "location": "location",
    "attributes": "attributes",
    "attribute_values": "attribute_values",
    "ads_attributes": "ads_attributes",
    "ads": "ads",
    "users": "users",
}


def _env_value(section: dict[str, Any], name: str, default_env: str) -> str:
    env_name = str(section.get(name, default_env)).strip()
    if not env_name:
        raise ValueError(f"Environment-variable name {name!r} is empty")
    return os.getenv(env_name, "").strip()


def _database_target(section: dict[str, Any], *, prefix: str) -> DatabaseTarget:
    backend = str(section.get("backend", "mysql")).strip().casefold()
    default_port = 3306 if backend == "mysql" else 5432
    tls = dict(section.get("tls", {}))
    tls_mode_env = str(tls.get("mode_env", "")).strip()
    tls_mode = (
        os.getenv(tls_mode_env, str(tls.get("mode", "prefer")))
        if tls_mode_env
        else str(tls.get("mode", "prefer"))
    )
    return DatabaseTarget(
        backend=backend,
        host=_env_value(section, "host_env", f"{prefix}_HOST"),
        port=int(
            _env_value(section, "port_env", f"{prefix}_PORT")
            or section.get("port", default_port)
        ),
        database=_env_value(
            section,
            "database_env",
            f"{prefix}_DATABASE",
        ),
        user=_env_value(section, "user_env", f"{prefix}_USER"),
        password=_env_value(
            section,
            "password_env",
            f"{prefix}_PASSWORD",
        ),
        schema=_safe_identifier(
            section.get("schema", "public"),
            label="Analytics database schema",
        ),
        connect_timeout_seconds=int(
            dict(section.get("timeouts", {})).get("connect_seconds", 10)
        ),
        read_timeout_seconds=int(
            dict(section.get("timeouts", {})).get("read_seconds", 600)
        ),
        tls_mode=tls_mode.strip().casefold(),
        tls_ca_file=_env_value(
            tls,
            "ca_file_env",
            f"{prefix}_TLS_CA_FILE",
        ),
        tls_cert_file=_env_value(
            tls,
            "cert_file_env",
            f"{prefix}_TLS_CERT_FILE",
        ),
        tls_key_file=_env_value(
            tls,
            "key_file_env",
            f"{prefix}_TLS_KEY_FILE",
        ),
    )


def load_company_analytics_config(path: Path) -> CompanyAnalyticsConfig | None:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Tenant configuration must be an object: {path}")
    analytics = dict(raw.get("analytics", {}))
    if not bool(analytics.get("enabled", False)):
        return None
    company = dict(raw.get("company", {}))
    company_id = str(company.get("id", path.stem)).strip().casefold()
    endpoint_slug = str(
        analytics.get("endpoint_slug", company_id)
    ).strip().casefold()
    api_key_envs = tuple(
        str(name).strip()
        for name in analytics.get("api_key_envs", ())
        if str(name).strip()
    )
    database_section = dict(raw.get("database", {}))
    database = _database_target(database_section, prefix=company_id.upper())

    telemetry = dict(analytics.get("telemetry", {}))
    use_company_database = bool(
        telemetry.get("use_company_database", True)
    )
    telemetry_database = (
        database
        if use_company_database
        else _database_target(
            dict(telemetry.get("database", {})),
            prefix=f"{company_id.upper()}_ANALYTICS",
        )
    )
    raw_metric_profiles = analytics.get("metrics", {})
    if raw_metric_profiles is None:
        raw_metric_profiles = {}
    if not isinstance(raw_metric_profiles, dict):
        raise ValueError("Analytics metrics configuration must be an object")
    unsupported_audiences = set(raw_metric_profiles) - {"company", "internal"}
    if unsupported_audiences:
        raise ValueError(
            "Analytics metrics configuration has unsupported audiences: "
            + ", ".join(sorted(str(name) for name in unsupported_audiences))
        )
    company_metric_profile = validate_metric_profile(
        raw_metric_profiles.get("company"),
        audience="company",
    )
    internal_metric_profile = validate_metric_profile(
        raw_metric_profiles.get("internal"),
        audience="internal",
    )

    raw_tables = dict(analytics.get("tables", {}))
    raw_columns = dict(analytics.get("columns", {}))
    tables = {
        **DEFAULT_TABLES,
        "search_history": str(
            analytics.get(
                "search_history_table",
                DEFAULT_TABLES["search_history"],
            )
        ),
        "api_usage": str(
            analytics.get(
                "api_usage_table",
                DEFAULT_TABLES["api_usage"],
            )
        ),
        **{str(key): str(value) for key, value in raw_tables.items()},
    }
    datasets = {
        name: DatasetMapping(
            table=_safe_identifier(table, label=f"Analytics table {name}"),
            columns={
                str(canonical): str(source)
                for canonical, source in dict(
                    raw_columns.get(name, {})
                ).items()
            },
        )
        for name, table in tables.items()
    }
    return CompanyAnalyticsConfig(
        company_id=company_id,
        endpoint_slug=endpoint_slug,
        api_key_envs=api_key_envs,
        database=database,
        telemetry_database=telemetry_database,
        datasets=datasets,
        config_path=path,
        adapter=str(analytics.get("adapter", "default")).strip().casefold(),
        history_days=int(analytics.get("history_days", 90)),
        company_metric_profile=company_metric_profile,
        internal_metric_profile=internal_metric_profile,
    )


class AnalyticsRegistry:
    def __init__(self, companies: dict[str, CompanyAnalyticsConfig]):
        self._companies = dict(companies)
        self._endpoints = {
            company.endpoint_slug: company
            for company in self._companies.values()
        }
        if len(self._endpoints) != len(self._companies):
            raise ValueError("Analytics company endpoint slugs must be unique")
        self._key_owners: dict[str, str] = {}
        for company in self._companies.values():
            for env_name in company.api_key_envs:
                key = os.getenv(env_name, "").strip()
                if not key:
                    continue
                digest = hashlib.sha256(key.encode()).hexdigest()
                existing = self._key_owners.get(digest)
                if existing is not None:
                    raise ValueError(
                        f"Companies {existing!r} and "
                        f"{company.company_id!r} share an analytics API key"
                    )
                self._key_owners[digest] = company.company_id

    @property
    def companies(self) -> dict[str, CompanyAnalyticsConfig]:
        return dict(self._companies)

    def resolve_endpoint(self, endpoint_slug: str) -> CompanyAnalyticsConfig | None:
        return self._endpoints.get(endpoint_slug.strip().casefold())

    def resolve_company(self, company_id: str) -> CompanyAnalyticsConfig | None:
        return self._companies.get(company_id.strip().casefold())

    def authenticate(
        self,
        endpoint_slug: str,
        api_key: str,
    ) -> CompanyAnalyticsConfig | None:
        company = self.resolve_endpoint(endpoint_slug)
        if company is None or not api_key:
            return None
        owner = self._key_owners.get(
            hashlib.sha256(api_key.encode()).hexdigest()
        )
        if owner is None or not hmac.compare_digest(owner, company.company_id):
            return None
        return company


def load_analytics_registry(directory: Path) -> AnalyticsRegistry:
    companies: dict[str, CompanyAnalyticsConfig] = {}
    if not directory.exists():
        return AnalyticsRegistry({})
    for path in sorted(directory.glob("*.yaml")):
        config = load_company_analytics_config(path)
        if config is None:
            continue
        if config.company_id in companies:
            raise ValueError(
                f"Duplicate analytics company {config.company_id!r}"
            )
        companies[config.company_id] = config
    return AnalyticsRegistry(companies)
