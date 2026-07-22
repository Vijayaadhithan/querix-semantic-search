import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tenant_config import (
    TenantRegistry,
    discover_tenant_profiles,
)
from postgres_store import PostgresRuntimeConfig
import vector_store


PROFILE = """
company:
  id: {company}
  planner_adapter: gainr
api:
  key_envs:
    - {key_env}
database:
  host_env: {prefix}_DB_HOST
  port_env: {prefix}_DB_PORT
  database_env: {prefix}_DB_NAME
  index_namespace: {company}_index
  user_env: {prefix}_DB_USER
  password_env: {prefix}_DB_PASSWORD
  search_ready_table: search_ready
  content_column: embedding_content
  bm25_column: bm25_content
  search_id_column: id
  result_table: products
  result_id_column: id
  timeouts:
    connect_seconds: 7
    read_seconds: 31
    write_seconds: 32
    statement_timeout_ms: 9000
  pool:
    min_size: 1
    max_size: 3
    timeout_seconds: 2.5
  tls:
    mode: disable
    mode_env: {prefix}_DB_TLS_MODE
    ca_file_env: {prefix}_DB_TLS_CA_FILE
storage:
  vector_backend: pgvector
  vector_dimensions: 768
  pgvector:
    use_company_database: {use_company_database}
    table: {company}_vectors
    hnsw:
      m: 12
      ef_construction: 48
      ef_search: 80
  bm25_path: {bm25_path}
payload:
  public_fields:
    - id
    - title
  filter_schema:
    subcategory_name: keyword
rate_limit:
  requests_per_minute: 30
  burst: 5
"""


def write_profile(
    tmp_path: Path,
    company: str,
    *,
    backend: str = "mysql",
) -> None:
    prefix = company.upper()
    (tmp_path / f"{company}.yaml").write_text(
        PROFILE.replace(
            "database:\n",
            f"database:\n  backend: {backend}\n",
        ).format(
            company=company,
            key_env=f"{prefix}_API_KEY",
            prefix=prefix,
            use_company_database=str(backend == "postgres").lower(),
            bm25_path=tmp_path / company / "bm25.sqlite3",
        ),
        encoding="utf-8",
    )


def set_database_environment(monkeypatch, company: str) -> None:
    prefix = company.upper()
    monkeypatch.setenv(f"{prefix}_DB_HOST", "localhost")
    monkeypatch.setenv(f"{prefix}_DB_PORT", "3306")
    monkeypatch.setenv(f"{prefix}_DB_NAME", f"db_{company}")
    monkeypatch.setenv(f"{prefix}_DB_USER", company)
    monkeypatch.setenv(f"{prefix}_DB_PASSWORD", "secret")
    monkeypatch.setenv("PGVECTOR_HOST", "localhost")
    monkeypatch.setenv("PGVECTOR_PORT", "5432")
    monkeypatch.setenv("PGVECTOR_DATABASE", "vectors")
    monkeypatch.setenv("PGVECTOR_USER", "vectors")
    monkeypatch.setenv("PGVECTOR_PASSWORD", "secret")


def test_tenant_profiles_resolve_separate_storage_and_api_keys(
    tmp_path,
    monkeypatch,
):
    for company in ("alpha", "beta"):
        write_profile(tmp_path, company)
        set_database_environment(monkeypatch, company)

    profiles = discover_tenant_profiles(tmp_path)
    registry = TenantRegistry(
        profiles,
        api_keys={"alpha": ["alpha-key"], "beta": ["beta-key"]},
    )

    assert profiles["alpha"].storage.pgvector_table == "alpha_vectors"
    assert profiles["alpha"].storage.bm25_path != profiles["beta"].storage.bm25_path
    assert profiles["alpha"].endpoint_slug == "alpha"
    assert profiles["alpha"].payload.request_mapping["query"] == "query"
    assert profiles["alpha"].database.connect_timeout_seconds == 7
    assert profiles["alpha"].database.read_timeout_seconds == 31
    assert profiles["alpha"].database.write_timeout_seconds == 32
    assert profiles["alpha"].database.statement_timeout_ms == 9000
    assert profiles["alpha"].database.pool_min_size == 1
    assert profiles["alpha"].database.pool_max_size == 3
    assert profiles["alpha"].database.pool_timeout_seconds == 2.5
    assert profiles["alpha"].database.index_namespace == "alpha_index"
    assert profiles["alpha"].database.tls_mode == "disable"
    assert profiles["alpha"].retrieval.semantic_related_tail_enabled is True
    assert (
        profiles["alpha"].retrieval.adaptive_vector_post_filter_metadata
        is False
    )
    assert (
        profiles["alpha"]
        .retrieval.semantic_related_tail_requires_explicit_category
        is False
    )
    assert profiles["alpha"].retrieval.reranker_relative_score_floor == 0.0
    assert profiles["alpha"].retrieval.reranker_min_score_by_provider == {}
    assert registry.resolve_api_key("alpha-key").company_id == "alpha"
    assert registry.resolve_api_key("beta-key").company_id == "beta"
    assert registry.resolve_api_key("wrong") is None


def test_tenant_profile_allows_tls_env_override(tmp_path, monkeypatch):
    write_profile(tmp_path, "alpha")
    set_database_environment(monkeypatch, "alpha")
    monkeypatch.setenv("ALPHA_DB_TLS_MODE", "verify-full")
    monkeypatch.setenv("ALPHA_DB_TLS_CA_FILE", "/run/secrets/alpha-ca.pem")

    profile = discover_tenant_profiles(tmp_path)["alpha"]

    assert profile.database.tls_mode == "verify-full"
    assert profile.database.tls_ca_file == "/run/secrets/alpha-ca.pem"


def test_tenant_profile_loads_company_specific_query_aliases(
    tmp_path,
    monkeypatch,
):
    write_profile(tmp_path, "alpha")
    set_database_environment(monkeypatch, "alpha")
    path = tmp_path / "alpha.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "api:\n",
            (
                "planner:\n"
                "  query_aliases:\n"
                "    BKE: Bike\n"
                "    techcician: technician\n"
                "api:\n"
            ),
            1,
        ),
        encoding="utf-8",
    )

    profile = discover_tenant_profiles(tmp_path)["alpha"]

    assert profile.planner_query_aliases == {
        "bke": "bike",
        "techcician": "technician",
    }


def test_tenant_profile_loads_company_specific_retrieval_policy(
    tmp_path,
    monkeypatch,
):
    write_profile(tmp_path, "alpha")
    set_database_environment(monkeypatch, "alpha")
    path = tmp_path / "alpha.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "api:\n",
            (
                "retrieval:\n"
                "  semantic_related_tail_enabled: false\n"
                "  semantic_related_tail_requires_explicit_category: true\n"
                "  reranker_relative_score_floor: 0.3\n"
                "  reranker_min_score_by_provider:\n"
                "    voyage-2.5: 0.05\n"
                "api:\n"
            ),
            1,
        ),
        encoding="utf-8",
    )

    profile = discover_tenant_profiles(tmp_path)["alpha"]

    assert profile.retrieval.semantic_related_tail_enabled is False
    assert (
        profile.retrieval.semantic_related_tail_requires_explicit_category
        is True
    )
    assert profile.retrieval.reranker_relative_score_floor == 0.3
    assert profile.retrieval.reranker_min_score_by_provider == {
        "voyage-2.5": 0.05
    }


def test_tenant_registry_rejects_shared_api_key(tmp_path, monkeypatch):
    for company in ("alpha", "beta"):
        write_profile(tmp_path, company)
        set_database_environment(monkeypatch, company)
    profiles = discover_tenant_profiles(tmp_path)

    with pytest.raises(ValueError, match="share an API key"):
        TenantRegistry(
            profiles,
            api_keys={"alpha": ["same"], "beta": ["same"]},
        )


def test_tenant_profiles_reject_shared_company_search_table(
    tmp_path,
    monkeypatch,
):
    for company in ("alpha", "beta"):
        write_profile(tmp_path, company)
        set_database_environment(monkeypatch, company)
    monkeypatch.setenv("BETA_DB_NAME", "db_alpha")

    with pytest.raises(ValueError, match="share a company search-data table"):
        discover_tenant_profiles(tmp_path)


def test_postgres_company_profile_is_supported(tmp_path, monkeypatch):
    write_profile(
        tmp_path,
        "alpha",
        backend="postgres",
    )
    set_database_environment(monkeypatch, "alpha")
    monkeypatch.setenv("ALPHA_DB_PORT", "5432")

    profile = discover_tenant_profiles(tmp_path)["alpha"]

    assert isinstance(profile.database, PostgresRuntimeConfig)
    assert profile.database.port == 5432
    assert profile.database.schema == "public"
    assert profile.database.result_type_column == "type"
    assert profile.storage.vector_backend == "pgvector"
    assert profile.storage.pgvector_database is profile.database
    assert profile.storage.pgvector_table == "alpha_vectors"
    assert profile.storage.pgvector_hnsw_m == 12
    assert profile.storage.pgvector_hnsw_ef_construction == 48
    assert profile.storage.pgvector_hnsw_ef_search == 80


def test_pgvector_profile_selects_pgvector_collection(
    tmp_path,
    monkeypatch,
):
    write_profile(
        tmp_path,
        "alpha",
        backend="postgres",
    )
    set_database_environment(monkeypatch, "alpha")
    monkeypatch.setenv("ALPHA_DB_PORT", "5432")
    profile = discover_tenant_profiles(tmp_path)["alpha"]
    captured = {}

    class FakePgVectorCollection:
        def __init__(
            self,
            database,
            table,
            dimensions,
            *,
            hnsw_m=16,
            hnsw_ef_construction=64,
            hnsw_ef_search=100,
            query_mode="legacy",
            create=False,
        ):
            captured.update(
                database=database,
                table=table,
                dimensions=dimensions,
                hnsw_m=hnsw_m,
                hnsw_ef_construction=hnsw_ef_construction,
                hnsw_ef_search=hnsw_ef_search,
                query_mode=query_mode,
                create=create,
            )

    monkeypatch.setattr(
        vector_store,
        "PgVectorCollection",
        FakePgVectorCollection,
    )

    result = vector_store.get_tenant_vector_collection(profile, create=True)

    assert isinstance(result, FakePgVectorCollection)
    assert captured["database"] is profile.database
    assert captured["table"] == "alpha_vectors"
    assert captured["dimensions"] == 768
    assert captured["hnsw_m"] == 12
    assert captured["hnsw_ef_construction"] == 48
    assert captured["hnsw_ef_search"] == 80
    assert captured["query_mode"] == "legacy"
    assert captured["create"] is True
