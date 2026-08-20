from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.tenant_config import TenantStorageConfig
from ingestion import generations
from search.bm25 import PersistentBM25Index
from storage.index_generations import profile_for_slot


@dataclass(frozen=True)
class Profile:
    company_id: str
    storage: TenantStorageConfig
    database: object
    endpoint_slug: str = "company"


def _profile(tmp_path: Path) -> Profile:
    return Profile(
        company_id="company",
        storage=TenantStorageConfig(
            bm25_path=tmp_path / "company" / "bm25.sqlite3",
            pgvector_table="company_vectors",
            pgvector_prewarm_on_startup=True,
            pgvector_prewarm_mode="buffer",
        ),
        database=SimpleNamespace(content_column="embedding_content"),
    )


def _write_bm25(path: Path) -> None:
    index = PersistentBM25Index(path)
    try:
        index.upsert(
            [
                {
                    "doc_id": "doc-1",
                    "product_id": "1",
                    "content": "camera equipment event",
                }
            ]
        )
    finally:
        index.close()


def test_candidate_validation_preserves_retrieval_overlap_and_warms_before_swap(
    tmp_path,
    monkeypatch,
):
    profile = _profile(tmp_path)
    candidate = profile_for_slot(profile, "b")
    _write_bm25(profile.storage.bm25_path)
    _write_bm25(candidate.storage.bm25_path)

    class Collection:
        def __init__(self, table):
            self.table = table
            self.prewarmed = False

        def count(self):
            return 1

        def query(self, **_kwargs):
            return {"ids": [["doc-1"]]}

        def prewarm_hnsw_index(self, *, mode):
            self.prewarmed = True
            return {"mode": mode, "duration_ms": 1.0}

    collections = {
        "company_vectors": Collection("company_vectors"),
        "company_vectors__b": Collection("company_vectors__b"),
    }
    monkeypatch.setattr(
        generations,
        "get_tenant_vector_collection",
        lambda selected, create=False: collections[selected.storage.pgvector_table],
    )
    monkeypatch.setattr(generations, "count_database_rows", lambda *_args: 1)
    monkeypatch.setattr(generations, "embed_texts", lambda *_args, **_kwargs: [[0.1]])

    result = generations.validate_and_warm_candidate(
        profile,
        queries=("camera",),
        overlap_floor=1.0,
        compare_limit=1,
        warm_candidates=1,
    )

    assert result["source_rows"] == result["vectors"] == result["bm25"] == 1
    assert result["vector_overlap"] == [1.0]
    assert result["bm25_overlap"] == [1.0]
    assert result["prewarm"]["mode"] == "buffer"
    assert collections["company_vectors__b"].prewarmed is True


def test_candidate_count_mismatch_fails_before_embedding_or_promotion(
    tmp_path,
    monkeypatch,
):
    profile = _profile(tmp_path)
    candidate = profile_for_slot(profile, "b")
    _write_bm25(candidate.storage.bm25_path)

    class Collection:
        def count(self):
            return 1

    monkeypatch.setattr(
        generations,
        "get_tenant_vector_collection",
        lambda *_args, **_kwargs: Collection(),
    )
    monkeypatch.setattr(generations, "count_database_rows", lambda *_args: 2)
    monkeypatch.setattr(
        generations,
        "embed_texts",
        lambda *_args, **_kwargs: pytest.fail("embedding must not run"),
    )

    with pytest.raises(RuntimeError, match="count validation failed"):
        generations.validate_and_warm_candidate(profile, queries=("camera",))
