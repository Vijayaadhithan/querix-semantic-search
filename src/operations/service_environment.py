"""Select least-privilege environment values for each workload and tenant."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EnvValue:
    name: str
    raw_value: str

    @property
    def plain_value(self) -> str:
        value = self.raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value.strip()

    @property
    def configured(self) -> bool:
        return bool(self.plain_value)


KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ROLE_SOURCES = {
    "MYSQL_SEARCH_USER",
    "MYSQL_SEARCH_PASSWORD",
    "MYSQL_INGEST_USER",
    "MYSQL_INGEST_PASSWORD",
    "MYSQL_TELEMETRY_USER",
    "MYSQL_TELEMETRY_PASSWORD",
    "MYSQL_ANALYTICS_USER",
    "MYSQL_ANALYTICS_PASSWORD",
    "MYSQL_ADMIN_USER",
    "MYSQL_ADMIN_PASSWORD",
    "MYSQL_ROLE_HOST",
    "PGVECTOR_SEARCH_USER",
    "PGVECTOR_SEARCH_PASSWORD",
    "PGVECTOR_INGEST_USER",
    "PGVECTOR_INGEST_PASSWORD",
}

PROVIDER_KEYS = {
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "JINA_API_KEY",
    "OPENROUTER_API_KEY",
    "VOYAGE_API_KEY",
}

MYSQL_WORKLOAD_CREDENTIAL_MODES = {"dedicated", "shared"}


def _is_secret_or_role_source(name: str) -> bool:
    if name == "MYSQL_WORKLOAD_CREDENTIAL_MODE":
        return False
    secret_segments = {
        "CREDENTIAL",
        "CREDENTIALS",
        "KEY",
        "PASSWORD",
        "SECRET",
        "TOKEN",
    }
    return (
        name in ROLE_SOURCES
        or name in PROVIDER_KEYS
        or name in {"API_ADMIN_KEY", "POSTGRES_USER", "POSTGRES_PASSWORD"}
        or bool(set(name.split("_")) & secret_segments)
    )


def _nonsecret_values(
    values: dict[str, EnvValue],
    *,
    prefixes: tuple[str, ...] | None = None,
    excluded_prefixes: tuple[str, ...] = (),
) -> dict[str, EnvValue]:
    selected = {}
    for name, value in values.items():
        if _is_secret_or_role_source(name) or name.startswith("DOCKER_"):
            continue
        if prefixes is not None and not name.startswith(prefixes):
            continue
        if name.startswith(excluded_prefixes):
            continue
        selected[name] = value
    return selected


def _copy(
    selected: dict[str, EnvValue],
    values: dict[str, EnvValue],
    name: str,
) -> None:
    if name in values:
        selected[name] = values[name]


def _alias(
    selected: dict[str, EnvValue],
    values: dict[str, EnvValue],
    target: str,
    preferred: str,
    fallback: str,
) -> None:
    source = values.get(preferred)
    if source is None or not source.configured:
        source = values.get(fallback)
    if source is not None:
        selected[target] = EnvValue(target, source.raw_value)


def mysql_workload_credential_mode(values: dict[str, EnvValue]) -> str:
    configured = values.get("MYSQL_WORKLOAD_CREDENTIAL_MODE")
    mode = configured.plain_value.casefold() if configured else "dedicated"
    if mode not in MYSQL_WORKLOAD_CREDENTIAL_MODES:
        expected = ", ".join(sorted(MYSQL_WORKLOAD_CREDENTIAL_MODES))
        raise RuntimeError("MYSQL_WORKLOAD_CREDENTIAL_MODE must be one of: " + expected)
    return mode


def _mysql_alias(
    selected: dict[str, EnvValue],
    values: dict[str, EnvValue],
    target: str,
    dedicated: str,
    mode: str,
) -> None:
    if mode == "shared":
        source = values.get(target)
        if source is not None:
            selected[target] = EnvValue(target, source.raw_value)
        return
    _alias(selected, values, target, dedicated, target)


def build_service_environments(
    values: dict[str, EnvValue],
    *,
    tenant_profiles: Iterable[dict] = (),
    require_tenant_credentials: bool = False,
) -> dict[str, dict[str, EnvValue]]:
    mysql_mode = mysql_workload_credential_mode(values)
    search_common = _nonsecret_values(
        values,
        excluded_prefixes=("ANALYTICS_", "POSTGRES_"),
    )
    api = dict(search_common)
    for name in PROVIDER_KEYS | {"API_ADMIN_KEY"}:
        _copy(api, values, name)
    for name in values:
        if name.endswith("_API_KEY") and not name.endswith("_ANALYTICS_API_KEY"):
            _copy(api, values, name)
    _mysql_alias(api, values, "MYSQL_USER", "MYSQL_SEARCH_USER", mysql_mode)
    _mysql_alias(
        api,
        values,
        "MYSQL_PASSWORD",
        "MYSQL_SEARCH_PASSWORD",
        mysql_mode,
    )
    _alias(api, values, "PGVECTOR_USER", "PGVECTOR_SEARCH_USER", "PGVECTOR_USER")
    _alias(
        api,
        values,
        "PGVECTOR_PASSWORD",
        "PGVECTOR_SEARCH_PASSWORD",
        "PGVECTOR_PASSWORD",
    )
    for name in values:
        if name.endswith("_DB_TLS_KEY_FILE"):
            _copy(api, values, name)

    ingestion = dict(search_common)
    _copy(ingestion, values, "API_ADMIN_KEY")
    _mysql_alias(
        ingestion,
        values,
        "MYSQL_USER",
        "MYSQL_INGEST_USER",
        mysql_mode,
    )
    _mysql_alias(
        ingestion,
        values,
        "MYSQL_PASSWORD",
        "MYSQL_INGEST_PASSWORD",
        mysql_mode,
    )
    _alias(
        ingestion,
        values,
        "PGVECTOR_USER",
        "PGVECTOR_INGEST_USER",
        "PGVECTOR_USER",
    )
    _alias(
        ingestion,
        values,
        "PGVECTOR_PASSWORD",
        "PGVECTOR_INGEST_PASSWORD",
        "PGVECTOR_PASSWORD",
    )
    for name in values:
        if name.endswith("_DB_TLS_KEY_FILE"):
            _copy(ingestion, values, name)

    telemetry = _nonsecret_values(
        values,
        prefixes=("MYSQL_", "SEARCH_ANALYTICS_", "GAINR_DB_TLS_"),
    )
    _copy(telemetry, values, "API_TENANT_CONFIG_DIR")
    _copy(telemetry, values, "PGVECTOR_DATABASE")
    _alias(
        telemetry,
        values,
        "PGVECTOR_USER",
        "PGVECTOR_SEARCH_USER",
        "PGVECTOR_USER",
    )
    _mysql_alias(
        telemetry,
        values,
        "MYSQL_USER",
        "MYSQL_TELEMETRY_USER",
        mysql_mode,
    )
    _mysql_alias(
        telemetry,
        values,
        "MYSQL_PASSWORD",
        "MYSQL_TELEMETRY_PASSWORD",
        mysql_mode,
    )
    for name in values:
        if name.endswith("_DB_TLS_KEY_FILE"):
            _copy(telemetry, values, name)

    analytics = _nonsecret_values(
        values,
        prefixes=("ANALYTICS_", "MYSQL_", "GAINR_DB_TLS_"),
    )
    for name in values:
        if name.endswith("_ANALYTICS_API_KEY"):
            _copy(analytics, values, name)
    _mysql_alias(
        analytics,
        values,
        "MYSQL_USER",
        "MYSQL_ANALYTICS_USER",
        mysql_mode,
    )
    _mysql_alias(
        analytics,
        values,
        "MYSQL_PASSWORD",
        "MYSQL_ANALYTICS_PASSWORD",
        mysql_mode,
    )
    for name in values:
        if name.endswith("_DB_TLS_KEY_FILE"):
            _copy(analytics, values, name)

    pgvector = {}
    _copy(pgvector, values, "POSTGRES_USER")
    _copy(pgvector, values, "POSTGRES_PASSWORD")

    database_admin = _nonsecret_values(values)
    for name in ROLE_SOURCES | {"POSTGRES_USER", "POSTGRES_PASSWORD"}:
        _copy(database_admin, values, name)
    if mysql_mode == "shared":
        _copy(database_admin, values, "MYSQL_USER")
        _copy(database_admin, values, "MYSQL_PASSWORD")
    else:
        _alias(
            database_admin,
            values,
            "MYSQL_USER",
            "MYSQL_ADMIN_USER",
            "MYSQL_USER",
        )
        _alias(
            database_admin,
            values,
            "MYSQL_PASSWORD",
            "MYSQL_ADMIN_PASSWORD",
            "MYSQL_PASSWORD",
        )
    _alias(
        database_admin,
        values,
        "PGVECTOR_USER",
        "POSTGRES_USER",
        "PGVECTOR_USER",
    )
    _alias(
        database_admin,
        values,
        "PGVECTOR_PASSWORD",
        "POSTGRES_PASSWORD",
        "PGVECTOR_PASSWORD",
    )

    environments = {
        "api": api,
        "ingestion": ingestion,
        "telemetry": telemetry,
        "analytics-api": analytics,
        "pgvector": pgvector,
        "database-admin": database_admin,
    }
    _propagate_tenant_environments(
        environments,
        values,
        tenant_profiles,
        require_credentials=require_tenant_credentials,
    )
    return environments


def _propagate_tenant_environments(
    environments: dict[str, dict[str, EnvValue]],
    values: dict[str, EnvValue],
    profiles: Iterable[dict],
    *,
    require_credentials: bool,
) -> None:
    """Copy declared references, preserving existing workload-specific aliases."""
    references: dict[str, set[str]] = {service: set() for service in environments}
    secrets: set[str] = set()
    required: set[tuple[str, str]] = set()

    def reference(section, field, default, services, *, secret=False, mandatory=False):
        name = str(section.get(field, default)).strip()
        if not KEY_RE.fullmatch(name):
            raise RuntimeError(f"Invalid tenant environment reference: {field}")
        if secret:
            secrets.add(name)
        for service in services:
            references[service].add(name)
            if mandatory:
                required.add((service, name))

    def database(section, prefix, services, *, source=True):
        for field in ("host", "port", "database", "user", "password"):
            reference(
                section,
                f"{field}_env",
                f"{prefix}_{field.upper()}",
                services,
                secret=field == "password",
                mandatory=field in {"user", "password"},
            )
        if source:
            reference(
                section, "index_namespace_env", f"{prefix}_INDEX_NAMESPACE", services
            )
            tls = dict(section.get("tls", {}))
            for field in ("mode", "ca_file", "cert_file", "key_file"):
                reference(
                    tls, f"{field}_env", f"{prefix}_TLS_{field.upper()}", services
                )

    for raw in profiles:
        company = str(dict(raw.get("company", {})).get("id", "")).strip().upper()
        source = dict(raw.get("database", {}))
        backend = str(source.get("backend", "mysql")).strip().casefold()
        prefix = "POSTGRES" if backend == "postgres" else "MYSQL"
        database(source, prefix, ("api", "ingestion", "telemetry", "database-admin"))
        storage = dict(raw.get("storage", {}))
        vector = dict(storage.get("pgvector", {}))
        if not vector.get("use_company_database", prefix == "POSTGRES"):
            database(
                vector, "PGVECTOR", ("api", "ingestion", "database-admin"), source=False
            )
            # Telemetry loads tenant identities but does not access vector data.
            for field in ("host", "port", "database", "user"):
                reference(
                    vector, f"{field}_env", f"PGVECTOR_{field.upper()}", ("telemetry",)
                )
        for name in dict(raw.get("api", {})).get("key_envs") or (f"{company}_API_KEY",):
            reference({"key": name}, "key", "", ("api",), secret=True, mandatory=True)
        analytics = dict(raw.get("analytics", {}))
        if analytics.get("enabled", False):
            database(source, company, ("analytics-api",))
            for name in analytics.get("api_key_envs", ()):
                reference({"key": name}, "key", "", ("analytics-api",), secret=True)
            telemetry = dict(analytics.get("telemetry", {}))
            if not telemetry.get("use_company_database", True):
                database(
                    dict(telemetry.get("database", {})),
                    f"{company}_ANALYTICS",
                    ("analytics-api", "telemetry", "database-admin"),
                )

    # A declared password can have an arbitrary name without PASSWORD/SECRET.
    # Remove it from generic copies before granting the explicit workload access.
    for service, selected in environments.items():
        for name in secrets - references[service]:
            selected.pop(name, None)
        for name in references[service]:
            if name in values:
                selected.setdefault(name, values[name])
    if require_credentials:
        missing = sorted(
            f"{service}:{name}"
            for service, name in required
            if name not in environments[service]
            or not environments[service][name].configured
        )
        if missing:
            raise RuntimeError(
                "Missing tenant workload credentials: " + ", ".join(missing)
            )
