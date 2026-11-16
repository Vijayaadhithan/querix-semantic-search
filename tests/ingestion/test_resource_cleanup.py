import sqlite3
from types import SimpleNamespace

import pytest

from ingestion import service
from search.bm25 import PersistentBM25Index
from storage.mysql import MySQLRuntimeConfig


@pytest.mark.parametrize("failure", ["revision", "source", "embedding", "rebuild"])
def test_failed_ingestion_closes_its_bm25_connection(tmp_path, monkeypatch, failure):
    database = MySQLRuntimeConfig(
        host="localhost",
        port=3306,
        database="catalog",
        user="reader",
        password="test-only",
        search_table="ads_search_ready",
        content_column="embedding_content",
        bm25_column="bm25_content",
        search_id_column="id",
        result_table="ads",
        result_id_column="id",
    )
    tenant = SimpleNamespace(
        company_id="sample",
        database=database,
        storage=SimpleNamespace(bm25_path=tmp_path / "bm25.sqlite3"),
    )
    index = PersistentBM25Index(tenant.storage.bm25_path)
    monkeypatch.setattr(service, "PersistentBM25Index", lambda _path: index)
    monkeypatch.setattr(
        service, "fetch_database_columns", lambda _config: ["id", "embedding_content"]
    )
    monkeypatch.setattr(service, "detect_database_primary_key", lambda *args: "id")
    monkeypatch.setattr(service, "count_database_rows", lambda *args: 1)
    monkeypatch.setattr(
        service,
        "get_tenant_vector_collection",
        lambda *args, **kwargs: SimpleNamespace(
            get=lambda **kwargs: {"ids": [], "documents": [], "metadatas": []}
        ),
    )

    def unavailable(*args, **kwargs):
        raise RuntimeError("dependency unavailable")

    monkeypatch.setattr(
        service,
        "resolve_embedding_model_revision",
        unavailable if failure == "revision" else lambda: "test-revision",
    )
    monkeypatch.setattr(
        service,
        "iter_database_rows",
        unavailable
        if failure in {"source", "rebuild"}
        else lambda *args, **kwargs: iter(
            [{"id": 1, "embedding_content": "Title: Camera"}]
        ),
    )
    monkeypatch.setattr(service, "embed_for_upsert", unavailable)

    with pytest.raises(RuntimeError, match="dependency unavailable"):
        if failure == "rebuild":
            service.rebuild_mysql_bm25_index(tenant=tenant, inactive_generation=True)
        else:
            service.ingest_mysql_source(tenant=tenant)

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        index.count()
