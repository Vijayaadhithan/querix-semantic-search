"""Build, validate, warm, and activate an inactive tenant index generation."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from core.tenant_config import TenantProfile
from ingestion.service import ingest_mysql_source
from providers.ollama import embed_texts
from search.bm25 import PersistentBM25Index, tokenize_query
from storage.database import count_database_rows
from storage.index_generations import (
    candidate_generation,
    ensure_generation_state,
    record_candidate_ready,
    resolve_generation,
    seed_candidate_from_active,
)
from storage.vector import get_tenant_vector_collection

CONTROL_QUERIES = (
    "comfortable vehicle for long distance travel",
    "general home repair service",
    "camera and equipment for an event",
)


def _ids(result: dict[str, Any]) -> list[str]:
    rows = result.get("ids") or []
    return [str(value) for value in (rows[0] if rows else [])]


def _overlap(left: list[str], right: list[str]) -> float:
    denominator = max(min(len(left), len(right)), 1)
    return len(set(left) & set(right)) / denominator


def _bm25_ids(path, query: str, limit: int) -> list[str]:
    tokens = tokenize_query(query)
    if not tokens:
        return []
    match_query = " OR ".join(f'"{token}"' for token in tokens)
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            """
            SELECT products.doc_id
            FROM products_fts
            JOIN products ON products.rowid = products_fts.rowid
            WHERE products_fts MATCH ?
            ORDER BY bm25(products_fts)
            LIMIT ?
            """,
            (match_query, limit),
        ).fetchall()
    return [str(row[0]) for row in rows]


def validate_and_warm_candidate(
    profile: TenantProfile,
    *,
    queries: tuple[str, ...] = CONTROL_QUERIES,
    overlap_floor: float = 0.80,
    compare_limit: int = 40,
    warm_candidates: int = 800,
) -> dict[str, Any]:
    """Fail closed on count drift or a large retrieval-quality discontinuity."""
    if not 0 <= overlap_floor <= 1:
        raise ValueError("overlap_floor must be between zero and one")
    active = resolve_generation(profile)
    candidate = candidate_generation(profile)
    active_collection = get_tenant_vector_collection(active.profile, create=False)
    candidate_collection = get_tenant_vector_collection(
        candidate.profile,
        create=False,
    )
    source_rows = count_database_rows(
        profile.database.content_column,
        profile.database,
    )
    vector_rows = candidate_collection.count()
    candidate_bm25 = PersistentBM25Index(candidate.profile.storage.bm25_path)
    try:
        bm25_rows = candidate_bm25.count()
    finally:
        candidate_bm25.close()
    if source_rows <= 0 or source_rows != vector_rows or source_rows != bm25_rows:
        raise RuntimeError(
            "Candidate count validation failed: "
            f"source={source_rows} vectors={vector_rows} bm25={bm25_rows}."
        )

    started = time.perf_counter()
    embeddings = embed_texts(list(queries), timeout=300)
    vector_overlap = []
    vector_query_ms = []
    bm25_overlap = []
    bm25_query_ms = []
    for query, embedding in zip(queries, embeddings):
        active_ids = _ids(
            active_collection.query(
                query_embeddings=[embedding],
                n_results=min(compare_limit, active_collection.count()),
                include=[],
            )
        )
        vector_started = time.perf_counter()
        candidate_ids = _ids(
            candidate_collection.query(
                query_embeddings=[embedding],
                n_results=min(warm_candidates, vector_rows),
                include=[],
            )
        )
        vector_query_ms.append((time.perf_counter() - vector_started) * 1000)
        vector_overlap.append(_overlap(active_ids, candidate_ids[:compare_limit]))

        active_bm25_ids = _bm25_ids(
            active.profile.storage.bm25_path,
            query,
            compare_limit,
        )
        bm25_started = time.perf_counter()
        candidate_bm25_ids = _bm25_ids(
            candidate.profile.storage.bm25_path,
            query,
            warm_candidates,
        )
        bm25_query_ms.append((time.perf_counter() - bm25_started) * 1000)
        bm25_overlap.append(
            _overlap(active_bm25_ids, candidate_bm25_ids[:compare_limit])
        )

    lowest_vector_overlap = min(vector_overlap, default=1.0)
    lowest_bm25_overlap = min(bm25_overlap, default=1.0)
    if lowest_vector_overlap < overlap_floor or lowest_bm25_overlap < overlap_floor:
        raise RuntimeError(
            "Candidate retrieval-overlap validation failed: "
            f"vector={lowest_vector_overlap:.3f} bm25={lowest_bm25_overlap:.3f} "
            f"required={overlap_floor:.3f}."
        )

    prewarm = None
    if candidate.profile.storage.pgvector_prewarm_on_startup:
        prewarm = candidate_collection.prewarm_hnsw_index(
            mode=candidate.profile.storage.pgvector_prewarm_mode
        )
        # Run the candidate vector window again after relation prewarm, so the
        # generation handed to the API is the measured warm path.
        vector_query_ms = []
        for embedding in embeddings:
            query_started = time.perf_counter()
            result = candidate_collection.query(
                query_embeddings=[embedding],
                n_results=min(warm_candidates, vector_rows),
                include=[],
            )
            vector_query_ms.append((time.perf_counter() - query_started) * 1000)
            if not _ids(result):
                raise RuntimeError("Candidate HNSW warm-up returned no rows.")

    return {
        "source_rows": source_rows,
        "vectors": vector_rows,
        "bm25": bm25_rows,
        "queries": len(queries),
        "vector_overlap": [round(value, 4) for value in vector_overlap],
        "bm25_overlap": [round(value, 4) for value in bm25_overlap],
        "vector_query_ms": [round(value, 1) for value in vector_query_ms],
        "bm25_query_ms": [round(value, 1) for value in bm25_query_ms],
        "prewarm": prewarm,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def activate_candidate(
    profile: TenantProfile,
    *,
    slot: str,
    generation: str,
    api_url: str | None = None,
) -> dict[str, Any]:
    admin_key = os.getenv("API_ADMIN_KEY", "").strip()
    if not admin_key:
        raise RuntimeError("API_ADMIN_KEY is required to activate a generation.")
    base_url = (
        api_url or os.getenv("SHADOW_INGEST_API_URL") or "http://api:8000"
    ).rstrip("/")
    endpoint = profile.endpoint_slug or profile.company_id
    query = urllib.parse.urlencode({"slot": slot, "generation": generation})
    request = urllib.request.Request(
        f"{base_url}/api/v1/{endpoint}/admin/reload-index?{query}",
        method="POST",
        headers={"X-Admin-Key": admin_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = json.load(response)
    except Exception as exc:
        raise RuntimeError("Live index-generation activation failed.") from exc
    if payload.get("status") != "promoted":
        raise RuntimeError(f"Unexpected activation response: {payload!r}")
    return payload


def run_shadow_ingestion(
    profile: TenantProfile,
    *,
    mysql_batch_size: int,
    embed_batch_size: int,
    overlap_floor: float = 0.80,
) -> dict[str, Any]:
    ensure_generation_state(profile)
    seed = seed_candidate_from_active(profile)
    candidate = candidate_generation(profile)
    started = time.perf_counter()
    ingest_mysql_source(
        batch_size=mysql_batch_size,
        embed_batch_size=embed_batch_size,
        reconcile_deletions=True,
        tenant=candidate.profile,
    )
    validation = validate_and_warm_candidate(
        profile,
        overlap_floor=overlap_floor,
    )
    generation = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    record_candidate_ready(
        profile,
        slot=candidate.slot,
        generation=generation,
        validation=validation,
    )
    activation = activate_candidate(
        profile,
        slot=candidate.slot,
        generation=generation,
    )
    return {
        "status": "complete",
        "company_id": profile.company_id,
        "candidate_slot": candidate.slot,
        "generation": generation,
        "seed": seed,
        "validation": validation,
        "activation": activation,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }
