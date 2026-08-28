from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from scripts.ensure_service_credentials import ensure_credentials
from scripts.migrate_runtime_storage import migrate_runtime_storage
from scripts.provision_database_roles import _pgvector_table_candidates
from scripts.render_service_env import (
    _validate_production_sources,
    build_service_environments,
    parse_env_file,
    write_service_environments,
)

ROOT = Path(__file__).resolve().parents[2]


def _service_environments(tmp_path: Path, *, mysql_mode: str = "dedicated"):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "MYSQL_HOST=db\n"
        "MYSQL_DATABASE=tenant\n"
        "PGVECTOR_DATABASE=vectors\n"
        "ANALYTICS_API_PORT=8010\n"
        f"MYSQL_WORKLOAD_CREDENTIAL_MODE={mysql_mode}\n"
        "SEARCH_ANALYTICS_DELIVERY_MODE=daily_spool\n",
        encoding="utf-8",
    )
    keys_path = tmp_path / ".env.keys"
    keys_path.write_text(
        "API_ADMIN_KEY=admin-secret\n"
        "GAINR_API_KEY=search-api-secret\n"
        "GAINR_ANALYTICS_API_KEY=analytics-api-secret\n"
        "OPENROUTER_API_KEY=provider-secret\n"
        "MYSQL_USER=shared-user\n"
        "MYSQL_PASSWORD=shared-pass\n"
        "MYSQL_SEARCH_USER=search\n"
        "MYSQL_SEARCH_PASSWORD=search-pass\n"
        "MYSQL_INGEST_USER=ingest\n"
        "MYSQL_INGEST_PASSWORD=ingest-pass\n"
        "MYSQL_TELEMETRY_USER=telemetry\n"
        "MYSQL_TELEMETRY_PASSWORD=telemetry-pass\n"
        "MYSQL_ANALYTICS_USER=analytics\n"
        "MYSQL_ANALYTICS_PASSWORD=analytics-pass\n"
        "MYSQL_ADMIN_USER=administrator\n"
        "MYSQL_ADMIN_PASSWORD=administrator-pass\n"
        "PGVECTOR_SEARCH_USER=vector_search\n"
        "PGVECTOR_SEARCH_PASSWORD=vector-search-pass\n"
        "PGVECTOR_INGEST_USER=vector_ingest\n"
        "PGVECTOR_INGEST_PASSWORD=vector-ingest-pass\n"
        "POSTGRES_USER=postgres\n"
        "POSTGRES_PASSWORD=postgres-pass\n",
        encoding="utf-8",
    )
    values = parse_env_file(env_path, required=True)
    values.update(parse_env_file(keys_path, required=True))
    return build_service_environments(values)


def _raw(environments, service: str) -> dict[str, str]:
    return {name: value.raw_value for name, value in environments[service].items()}


def test_service_envs_keep_secrets_inside_their_workloads(tmp_path):
    environments = _service_environments(tmp_path)
    api = _raw(environments, "api")
    ingestion = _raw(environments, "ingestion")
    telemetry = _raw(environments, "telemetry")
    analytics = _raw(environments, "analytics-api")
    pgvector = _raw(environments, "pgvector")

    assert api["MYSQL_PASSWORD"] == "search-pass"
    assert api["PGVECTOR_PASSWORD"] == "vector-search-pass"
    assert api["OPENROUTER_API_KEY"] == "provider-secret"
    assert api["GAINR_API_KEY"] == "search-api-secret"
    assert "GAINR_ANALYTICS_API_KEY" not in api
    assert "POSTGRES_PASSWORD" not in api
    assert "MYSQL_ADMIN_PASSWORD" not in api

    assert ingestion["MYSQL_PASSWORD"] == "ingest-pass"
    assert ingestion["PGVECTOR_PASSWORD"] == "vector-ingest-pass"
    assert ingestion["API_ADMIN_KEY"] == "admin-secret"
    assert "OPENROUTER_API_KEY" not in ingestion
    assert "GAINR_API_KEY" not in ingestion

    assert telemetry["MYSQL_PASSWORD"] == "telemetry-pass"
    assert telemetry["PGVECTOR_DATABASE"] == "vectors"
    assert telemetry["PGVECTOR_USER"] == "vector_search"
    assert set(telemetry).isdisjoint(
        {"API_ADMIN_KEY", "GAINR_API_KEY", "OPENROUTER_API_KEY", "PGVECTOR_PASSWORD"}
    )
    assert analytics["MYSQL_PASSWORD"] == "analytics-pass"
    assert analytics["GAINR_ANALYTICS_API_KEY"] == "analytics-api-secret"
    assert "GAINR_API_KEY" not in analytics
    assert "OPENROUTER_API_KEY" not in analytics
    assert pgvector == {
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": "postgres-pass",
    }


def test_shared_mysql_mode_uses_provider_credential_without_admin_access(tmp_path):
    environments = _service_environments(tmp_path, mysql_mode="shared")

    for service in ("api", "ingestion", "telemetry", "analytics-api"):
        rendered = _raw(environments, service)
        assert rendered["MYSQL_USER"] == "shared-user"
        assert rendered["MYSQL_PASSWORD"] == "shared-pass"
        assert "MYSQL_ADMIN_PASSWORD" not in rendered
    database_admin = _raw(environments, "database-admin")
    assert database_admin["MYSQL_USER"] == "shared-user"
    assert database_admin["MYSQL_PASSWORD"] == "shared-pass"
    assert database_admin["MYSQL_WORKLOAD_CREDENTIAL_MODE"] == "shared"


def test_shared_mysql_mode_passes_production_source_validation(tmp_path):
    _service_environments(tmp_path, mysql_mode="shared")
    keys_path = tmp_path / ".env.keys"
    os.chmod(keys_path, 0o600)
    values = parse_env_file(tmp_path / ".env", required=True)
    values.update(parse_env_file(keys_path, required=True))

    _validate_production_sources(values, keys_path)


def test_rendered_env_files_are_atomic_private_and_checkable(tmp_path):
    environments = _service_environments(tmp_path)
    output_dir = tmp_path / ".runtime" / "env"

    write_service_environments(environments, output_dir, check=False)
    write_service_environments(environments, output_dir, check=True)

    assert output_dir.stat().st_mode & 0o077 == 0
    assert all(path.stat().st_mode & 0o077 == 0 for path in output_dir.glob("*.env"))
    (output_dir / "api.env").write_text("stale\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Stale or unsafe"):
        write_service_environments(environments, output_dir, check=True)


def test_service_credentials_preserve_existing_values(tmp_path):
    keys_path = tmp_path / ".env.keys"
    keys_path.write_text(
        "MYSQL_SEARCH_USER=custom_search\nMYSQL_SEARCH_PASSWORD=keep-me\n",
        encoding="utf-8",
    )
    os.chmod(keys_path, 0o600)

    generated = ensure_credentials(keys_path)
    rendered = keys_path.read_text(encoding="utf-8")

    assert "MYSQL_SEARCH_USER=custom_search" in rendered
    assert "MYSQL_SEARCH_PASSWORD=keep-me" in rendered
    assert "MYSQL_INGEST_PASSWORD=" in rendered
    assert "MYSQL_SEARCH_USER" not in generated
    assert keys_path.stat().st_mode & 0o077 == 0


def test_shared_mysql_mode_generates_only_pgvector_roles(tmp_path):
    keys_path = tmp_path / ".env.keys"
    keys_path.write_text(
        "MYSQL_USER=shared\nMYSQL_PASSWORD=keep-me\n",
        encoding="utf-8",
    )
    os.chmod(keys_path, 0o600)

    generated = ensure_credentials(
        keys_path,
        mysql_mode="shared",
        persist_mysql_mode=True,
    )
    rendered = keys_path.read_text(encoding="utf-8")

    assert "MYSQL_WORKLOAD_CREDENTIAL_MODE=shared" in rendered
    assert "PGVECTOR_SEARCH_USER=" in rendered
    assert "MYSQL_SEARCH_USER=" not in rendered
    assert "MYSQL_WORKLOAD_CREDENTIAL_MODE" in generated


def test_service_credentials_generate_separate_analytics_api_keys(tmp_path):
    keys_path = tmp_path / ".env.keys"
    keys_path.write_text("GAINR_API_KEY=existing-search-key\n", encoding="utf-8")
    os.chmod(keys_path, 0o600)

    generated = ensure_credentials(
        keys_path,
        mysql_mode="shared",
        service_api_keys=("GAINR_ANALYTICS_API_KEY",),
    )
    rendered = keys_path.read_text(encoding="utf-8")

    assert "GAINR_ANALYTICS_API_KEY=" in rendered
    assert "GAINR_ANALYTICS_API_KEY" in generated
    assert "GAINR_API_KEY=existing-search-key" in rendered


def test_runtime_storage_migration_moves_sqlite_sidecars(tmp_path):
    storage = tmp_path / "storage"
    (storage / "companies").mkdir(parents=True)
    for name in (
        "usage.sqlite3",
        "usage.sqlite3-wal",
        "search_analytics_spool.sqlite3",
    ):
        (storage / name).write_text(name, encoding="utf-8")

    moves = migrate_runtime_storage(storage, check=False)

    assert len(moves) == 3
    assert not (storage / "usage.sqlite3").exists()
    assert (storage / "search-runtime" / "usage.sqlite3-wal").is_file()
    migrate_runtime_storage(storage, check=True)


def test_runtime_storage_migration_refuses_collisions(tmp_path):
    storage = tmp_path / "storage"
    (storage / "search-runtime").mkdir(parents=True)
    (storage / "usage.sqlite3").touch()
    (storage / "search-runtime" / "usage.sqlite3").touch()

    with pytest.raises(RuntimeError, match="Both legacy and isolated"):
        migrate_runtime_storage(storage, check=False)


def test_pgvector_role_provisioning_covers_two_slot_table_names():
    assert _pgvector_table_candidates("tenant_vectors") == {
        "tenant_vectors",
        "tenant_vectors_a",
        "tenant_vectors_b",
        "tenant_vectors__a",
        "tenant_vectors__b",
    }


def test_compose_has_separate_env_and_storage_boundaries():
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)["services"]

    assert ".env.keys" not in compose_text
    assert compose["api"]["volumes"] == [
        "./storage/companies:/app/storage/companies",
        "./storage/search-runtime:/app/storage/search-runtime",
        "./configs/tenants:/app/configs/tenants:ro",
    ]
    assert compose["analytics-api"]["volumes"] == [
        "./storage/analytics:/app/storage/analytics",
        "./configs/tenants:/app/configs/tenants:ro",
    ]
    assert compose["telemetry-uploader"]["volumes"][0] == (
        "./storage/search-runtime:/app/storage/search-runtime"
    )
    assert compose["pgvector"]["env_file"][0]["path"] == (".runtime/env/pgvector.env")
    for ignore_file in (".dockerignore", "Dockerfile.dockerignore"):
        assert ".runtime" in (ROOT / ignore_file).read_text(encoding="utf-8")
