#!/usr/bin/env python3
"""Warm representative pgvector HNSW paths after ingestion."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ollama_client import embed_texts  # noqa: E402
from tenant_config import load_tenant_registry  # noqa: E402
from vector_store import get_tenant_vector_collection  # noqa: E402


DEFAULT_QUERIES = (
    "comfortable vehicle for long distance travel",
    "general home repair service",
    "camera and equipment for an event",
)


def warm_hnsw(company_id: str, queries: list[str], candidates: int) -> None:
    profile = load_tenant_registry(require_api_keys=False).get(company_id)
    collection = get_tenant_vector_collection(profile, create=False)
    row_count = collection.count()
    if row_count <= 0:
        raise RuntimeError(f"Tenant {company_id!r} has no vectors to warm.")

    started = time.perf_counter()
    embeddings = embed_texts(queries, timeout=300)
    timings = []
    for embedding in embeddings:
        query_started = time.perf_counter()
        result = collection.query(
            query_embeddings=[embedding],
            n_results=min(candidates, row_count),
            include=[],
        )
        timings.append((time.perf_counter() - query_started) * 1000)
        if not result.get("ids") or not result["ids"][0]:
            raise RuntimeError("Representative HNSW warm-up returned no rows.")

    elapsed_ms = (time.perf_counter() - started) * 1000
    query_ms = ", ".join(f"{value:.0f}" for value in timings)
    print(
        f"HNSW warm-up complete company={company_id} vectors={row_count} "
        f"queries={len(queries)} query_ms=[{query_ms}] "
        f"duration_ms={elapsed_ms:.0f}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run representative unfiltered HNSW queries.",
    )
    parser.add_argument("--company", default="gainr")
    parser.add_argument("--candidates", type=int, default=100)
    parser.add_argument("--query", action="append", dest="queries")
    args = parser.parse_args()
    if args.candidates <= 0:
        parser.error("--candidates must be greater than zero")
    warm_hnsw(
        args.company,
        args.queries or list(DEFAULT_QUERIES),
        args.candidates,
    )


if __name__ == "__main__":
    main()
