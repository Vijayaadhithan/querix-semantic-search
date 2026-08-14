from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.settings import EMBED_MODEL, EMBED_MODEL_REVISION
from ingestion.documents import (
    content_hash,
    metadata_hash,
    metadata_value,
    mysql_document_id,
    prepare_bm25_index_row,
    prepare_content_document,
    prepare_mysql_row,
)
from ingestion.service import (
    database_current_ids,
    database_row_changes,
    ingest_mysql_source,
    reconcile_deleted_documents,
)
from search.bm25 import PersistentBM25Index
from storage.mysql import MySQLRuntimeConfig, quote_mysql_identifier


def test_quote_mysql_identifier_escapes_backticks():
    assert quote_mysql_identifier("ads_search_ready") == "`ads_search_ready`"
    assert quote_mysql_identifier("we`ird") == "`we``ird`"
    with pytest.raises(ValueError):
        quote_mysql_identifier("")


def test_metadata_value_normalizes_supported_vector_metadata_types():
    assert metadata_value(None) is None
    assert metadata_value("") is None
    assert metadata_value(Decimal("12.50")) == 12.5
    assert metadata_value(date(2026, 6, 26)) == "2026-06-26"
    assert metadata_value(b"hello") == "hello"


def test_prepare_mysql_row_uses_embedding_content_as_document():
    row = {
        "id": 42,
        "title": "Road bike",
        "city": "Chennai",
        "price": Decimal("12500.00"),
        "embedding_content": "title: Road bike; category: Cycles",
    }

    doc_id, document, metadata = prepare_mysql_row(row, "embedding_content", "id")

    assert doc_id
    assert document == "title: Road bike; category: Cycles"
    assert metadata["source_type"] == "mysql"
    assert metadata["source_table"] == "ads_search_ready"
    assert metadata["primary_key_column"] == "id"
    assert metadata["primary_key_value"] == 42
    assert metadata["source_content_hash"] == content_hash(document)
    assert metadata["embedding_model_revision"] == EMBED_MODEL_REVISION
    assert metadata["source_metadata_hash"] == metadata_hash(metadata)
    assert metadata["city"] == "Chennai"
    assert metadata["price"] == 12500.0
    assert "embedding_content" not in metadata


def test_prepare_bm25_row_carries_internal_user_quality_metadata():
    row = {
        "id": 42,
        "bm25_content": "female plumber",
        "user_gender": 2,
        "user_is_aadhaar_gst_verified": 1,
    }

    prepared = prepare_bm25_index_row(row, "bm25_content", "id")

    assert prepared["user_gender"] == 2
    assert prepared["user_is_aadhaar_gst_verified"] == 1


def test_mysql_document_ids_are_company_isolated():
    alpha = mysql_document_id(
        "search_ready",
        42,
        database="catalog",
        company_id="alpha",
    )
    beta = mysql_document_id(
        "search_ready",
        42,
        database="catalog",
        company_id="beta",
    )

    assert alpha != beta


def test_database_document_ids_are_backend_isolated():
    mysql_id = mysql_document_id(
        "search_ready",
        42,
        database="catalog",
        company_id="alpha",
        backend="mysql",
    )
    postgres_id = mysql_document_id(
        "search_ready",
        42,
        database="catalog",
        company_id="alpha",
        backend="postgres",
    )

    assert mysql_id != postgres_id


def test_prepare_rows_use_stable_index_namespace_across_databases():
    common = {
        "host": "localhost",
        "port": 3306,
        "user": "search",
        "password": "secret",
        "search_table": "ads_search_ready",
        "content_column": "embedding_content",
        "bm25_column": "bm25_content",
        "search_id_column": "id",
        "result_table": "ads",
        "result_id_column": "id",
        "index_namespace": "rag_ht_test",
    }
    local = MySQLRuntimeConfig(database="rag_ht_test", **common)
    production = MySQLRuntimeConfig(database="production_database", **common)
    row = {"id": 42, "embedding_content": "Road bike in Chennai"}

    local_id, _, local_metadata = prepare_mysql_row(
        row,
        "embedding_content",
        "id",
        mysql_config=local,
        company_id="gainr",
    )
    production_id, _, production_metadata = prepare_mysql_row(
        row,
        "embedding_content",
        "id",
        mysql_config=production,
        company_id="gainr",
    )

    assert local_id == production_id
    assert local_metadata["source_file"] == ("mysql:rag_ht_test.ads_search_ready")
    assert production_metadata["source_file"] == ("mysql:rag_ht_test.ads_search_ready")
    assert production_metadata["source_database"] == "production_database"


def test_prepare_content_document_extracts_json_semantic_text():
    document, metadata = prepare_content_document(
        '{"semantic_text": "bike for sale", "city": "Chennai", "price": 1000}'
    )

    assert document == "bike for sale"
    assert metadata["content_format"] == "json"
    assert metadata["content_city"] == "Chennai"
    assert metadata["content_price"] == 1000


def test_prepare_content_document_normalizes_labeled_text():
    document, metadata = prepare_content_document(
        "Title: Bachelor Mansion for Daily Rent Description: Mansion for Daily Rent. "
        "Listing meta title: Bachelor-Mansion-for-Daily-Rent-Sitra-Coimbatore "
        "Main category: Accommodation & Spaces Subcategory: Mansion "
        "Listing rental duration: Per Day State: Tamil Nadu City: Coimbatore "
        "Locality: Sitra Selected attributes: Speciality Selected attribute values: AC"
    )

    assert "\nDescription: Mansion for Daily Rent." in document
    assert "\nState: Tamil Nadu" in document
    assert "\nCity: Coimbatore" in document
    assert metadata["content_format"] == "labeled_text"
    assert metadata["content_title"] == "Bachelor Mansion for Daily Rent"
    assert metadata["content_main_category"] == "Accommodation & Spaces"
    assert metadata["content_subcategory"] == "Mansion"
    assert metadata["content_listing_rental_duration"] == "Per Day"
    assert metadata["content_state"] == "Tamil Nadu"
    assert metadata["content_city"] == "Coimbatore"
    assert metadata["content_locality"] == "Sitra"


def test_prepare_content_document_flattens_json_without_semantic_text():
    document, metadata = prepare_content_document(
        '{"ad": {"title": "Road bike", "condition": "used"}, "tags": ["cycle"]}'
    )

    assert "ad.title: Road bike" in document
    assert "ad.condition: used" in document
    assert "tags.1: cycle" in document
    assert metadata["content_ad_title"] == "Road bike"


def test_prepare_mysql_row_parses_json_embedding_content():
    row = {
        "id": 7,
        "embedding_content": (
            '{"semantic_text": "title: Road bike; category: Cycles", "city": "Chennai"}'
        ),
    }

    _, document, metadata = prepare_mysql_row(row, "embedding_content", "id")

    assert document == "title: Road bike; category: Cycles"
    assert metadata["content_format"] == "json"
    assert metadata["content_city"] == "Chennai"


def test_prepare_mysql_row_skips_empty_embedding_content():
    row = {"id": 1, "embedding_content": " "}
    assert prepare_mysql_row(row, "embedding_content", "id") is None


class FakeMysqlCollection:
    def __init__(
        self,
        ids,
        hashes,
        documents=None,
        model=EMBED_MODEL,
        metadatas=None,
    ):
        self.data = {
            "ids": ids,
            "documents": documents or [f"document {doc_id}" for doc_id in ids],
            "metadatas": metadatas
            or [
                {
                    "embedding_model": model,
                    "source_content_hash": hash_value,
                }
                for hash_value in hashes
            ],
        }

    def get(self, **_kwargs):
        requested_ids = _kwargs["ids"]
        selected_ids = []
        selected_documents = []
        selected_metadatas = []
        for doc_id, document, metadata in zip(
            self.data["ids"], self.data["documents"], self.data["metadatas"]
        ):
            if doc_id in requested_ids:
                selected_ids.append(doc_id)
                selected_documents.append(document)
                selected_metadatas.append(metadata)
        return {
            "ids": selected_ids,
            "documents": selected_documents,
            "metadatas": selected_metadatas,
        }


def test_database_current_ids_matches_model_and_content_hash():
    ids = ["id-1", "id-2"]
    metadatas = [
        {"source_content_hash": "hash-1"},
        {"source_content_hash": "hash-2"},
    ]
    collection = FakeMysqlCollection(["id-1", "id-2"], ["hash-1", "old-hash"])

    assert database_current_ids(
        collection,
        ids,
        ["document id-1", "changed document"],
        metadatas,
    ) == {"id-1"}


def test_database_current_ids_accepts_matching_document_without_hash():
    collection = FakeMysqlCollection(
        ["id-1"],
        [None],
        documents=["stored document"],
    )

    assert database_current_ids(
        collection,
        ["id-1"],
        ["stored document"],
        [{"source_content_hash": "new-hash"}],
    ) == {"id-1"}


def test_database_current_ids_rejects_different_embedding_model():
    collection = FakeMysqlCollection(["id-1"], ["hash-1"], model="other-model")

    assert (
        database_current_ids(
            collection,
            ["id-1"],
            ["document id-1"],
            [{"source_content_hash": "hash-1"}],
        )
        == set()
    )


def test_database_row_changes_detects_metadata_without_reembedding():
    desired = {
        "embedding_model": EMBED_MODEL,
        "embedding_model_revision": EMBED_MODEL_REVISION,
        "source_content_hash": "hash-1",
        "city_id": 142,
    }
    desired["source_metadata_hash"] = metadata_hash(desired)
    stored = {
        **desired,
        "city_id": 81,
    }
    stored["source_metadata_hash"] = metadata_hash(stored)
    collection = FakeMysqlCollection(
        ["id-1"],
        ["hash-1"],
        documents=["same document"],
        metadatas=[stored],
    )

    changes = database_row_changes(
        collection,
        ["id-1"],
        ["same document"],
        [desired],
    )

    assert changes.current_ids == set()
    assert changes.metadata_only_ids == {"id-1"}


def test_database_row_changes_reembeds_changed_model_revision():
    desired = {
        "embedding_model": EMBED_MODEL,
        "embedding_model_revision": EMBED_MODEL_REVISION,
        "source_content_hash": "hash-1",
    }
    desired["source_metadata_hash"] = metadata_hash(desired)
    stored = {**desired, "embedding_model_revision": "old-digest"}
    collection = FakeMysqlCollection(
        ["id-1"],
        ["hash-1"],
        documents=["same document"],
        metadatas=[stored],
    )

    changes = database_row_changes(
        collection,
        ["id-1"],
        ["same document"],
        [desired],
    )

    assert changes.current_ids == set()
    assert changes.metadata_only_ids == set()


class ReconciliationCollection:
    def __init__(self, ids):
        self.ids = list(ids)
        self.deleted = []

    def get(self, **_kwargs):
        return {"ids": list(self.ids)}

    def delete(self, *, ids):
        self.deleted.extend(ids)
        self.ids = [doc_id for doc_id in self.ids if doc_id not in set(ids)]


def test_deletion_reconciliation_removes_only_unseen_rows(tmp_path):
    index = PersistentBM25Index(tmp_path / "bm25.sqlite3")
    index.upsert(
        [
            {"doc_id": "keep", "product_id": "1", "content": "keep"},
            {"doc_id": "stale-bm25", "product_id": "2", "content": "stale"},
        ]
    )
    collection = ReconciliationCollection(["keep", "stale-vector"])

    deleted_vectors, deleted_bm25 = reconcile_deleted_documents(
        collection,
        index,
        "mysql:catalog.search_ready",
        {"keep"},
    )

    assert deleted_vectors == 1
    assert deleted_bm25 == 1
    assert collection.ids == ["keep"]
    assert index.doc_ids() == {"keep"}
    index.close()


def test_deletion_reconciliation_rejects_partial_scan():
    with pytest.raises(RuntimeError, match="requires a full scan"):
        ingest_mysql_source(limit=10, reconcile_deletions=True)


def test_small_ingestion_sample_refreshes_metadata_and_new_vectors(
    tmp_path,
    monkeypatch,
):
    from ingestion import service as ingestion_service

    database = MySQLRuntimeConfig(
        host="localhost",
        port=3306,
        database="catalog",
        user="search",
        password="secret",
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
    rows = [
        {
            "id": 1,
            "embedding_content": "Title: Camera",
            "bm25_content": "camera",
            "city_id": 142,
        },
        {
            "id": 2,
            "embedding_content": "Title: Bicycle",
            "bm25_content": "bicycle",
            "city_id": 142,
        },
        {
            "id": 3,
            "embedding_content": "Title: Drill",
            "bm25_content": "drill",
            "city_id": 142,
        },
    ]
    prepared = [
        prepare_mysql_row(
            row,
            "embedding_content",
            "id",
            mysql_config=database,
            company_id="sample",
        )
        for row in rows
    ]
    assert all(item is not None for item in prepared)
    first_id, first_document, first_metadata = prepared[0]
    second_id, second_document, second_metadata = prepared[1]
    stale_second_metadata = {**second_metadata, "city_id": 81}
    stale_second_metadata["source_metadata_hash"] = metadata_hash(stale_second_metadata)

    class SampleCollection:
        def __init__(self):
            self.rows = {
                first_id: (first_document, first_metadata),
                second_id: (second_document, stale_second_metadata),
            }
            self.metadata_updates = []
            self.vector_upserts = []

        def get(self, *, ids=None, where=None, include=None):
            del where, include
            selected = ids or list(self.rows)
            present = [doc_id for doc_id in selected if doc_id in self.rows]
            return {
                "ids": present,
                "documents": [self.rows[doc_id][0] for doc_id in present],
                "metadatas": [self.rows[doc_id][1] for doc_id in present],
            }

        def update_metadatas(self, *, ids, metadatas):
            self.metadata_updates.extend(ids)
            for doc_id, metadata in zip(ids, metadatas):
                self.rows[doc_id] = (self.rows[doc_id][0], metadata)

        def upsert(self, *, ids, documents, embeddings, metadatas):
            assert len(ids) == len(embeddings)
            self.vector_upserts.extend(ids)
            for doc_id, document, metadata in zip(ids, documents, metadatas):
                self.rows[doc_id] = (document, metadata)

        def count(self):
            return len(self.rows)

    collection = SampleCollection()
    monkeypatch.setattr(
        ingestion_service,
        "fetch_database_columns",
        lambda _config: ["id", "embedding_content", "bm25_content", "city_id"],
    )
    monkeypatch.setattr(
        ingestion_service,
        "detect_database_primary_key",
        lambda *_args: "id",
    )
    monkeypatch.setattr(
        ingestion_service,
        "count_database_rows",
        lambda *_args: len(rows),
    )
    monkeypatch.setattr(
        ingestion_service,
        "iter_database_rows",
        lambda *_args, **_kwargs: iter(rows),
    )
    monkeypatch.setattr(
        ingestion_service,
        "get_tenant_vector_collection",
        lambda *_args, **_kwargs: collection,
    )
    monkeypatch.setattr(
        ingestion_service,
        "embed_for_upsert",
        lambda documents, *_args, **_kwargs: [[0.0] * 3 for _ in documents],
    )
    monkeypatch.setattr(
        ingestion_service,
        "resolve_embedding_model_revision",
        lambda: EMBED_MODEL_REVISION,
    )

    ingest_mysql_source(batch_size=3, tenant=tenant)

    assert collection.metadata_updates == [second_id]
    assert collection.vector_upserts == [prepared[2][0]]
    index = PersistentBM25Index(tenant.storage.bm25_path)
    assert index.count() == 3
    index.close()
    assert not (tmp_path / ".ingestion-state.json").exists()
    assert (tmp_path / "ingestion-manifest.json").exists()
