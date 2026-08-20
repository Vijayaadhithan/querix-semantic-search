#!/usr/bin/env python3
"""Warm the local embedding, pgvector HNSW, and BM25 search paths."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPT_DIR))

from warm_hnsw import DEFAULT_QUERIES, warm_hnsw

from core.tenant_config import load_tenant_registry
from search.bm25 import tokenize_query
from storage.index_generations import resolve_generation


def prewarm_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> dict[str, float]:
    """Read a file sequentially into the host page cache."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    if not path.is_file():
        raise RuntimeError(f"BM25 index does not exist: {path}")
    started = time.perf_counter()
    file_bytes = 0
    buffer = bytearray(chunk_size)
    with path.open("rb", buffering=0) as stream:
        while read_bytes := stream.readinto(buffer):
            file_bytes += read_bytes
    return {
        "file_bytes": float(file_bytes),
        "file_read_ms": (time.perf_counter() - started) * 1000,
    }


def warm_bm25(
    path: Path,
    queries: list[str],
    candidates: int,
) -> dict[str, object]:
    """Run representative FTS queries using a read-only SQLite connection."""
    if candidates <= 0:
        raise ValueError("candidates must be greater than zero")
    file_warm = prewarm_file(path)

    timings: list[float] = []
    result_counts: list[int] = []
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        for query in queries:
            tokens = tokenize_query(query)
            if not tokens:
                raise RuntimeError("Representative BM25 query has no tokens.")
            match_query = " OR ".join(f'"{token}"' for token in tokens)
            query_started = time.perf_counter()
            rows = connection.execute(
                """
                SELECT rowid
                FROM products_fts
                WHERE products_fts MATCH ?
                ORDER BY bm25(products_fts)
                LIMIT ?
                """,
                (match_query, candidates),
            ).fetchall()
            timings.append((time.perf_counter() - query_started) * 1000)
            result_counts.append(len(rows))
            if not rows:
                raise RuntimeError(
                    f"Representative BM25 warm-up returned no rows: {query!r}"
                )

    return {
        **file_warm,
        "query_ms": timings,
        "result_counts": result_counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=("Warm Ollama embeddings and representative HNSW/BM25 queries."),
    )
    parser.add_argument("--company", default="gainr")
    parser.add_argument("--candidates", type=int, default=100)
    parser.add_argument("--query", action="append", dest="queries")
    args = parser.parse_args()
    if args.candidates <= 0:
        parser.error("--candidates must be greater than zero")

    queries = args.queries or list(DEFAULT_QUERIES)
    started = time.perf_counter()
    # warm_hnsw embeds the representative queries first, so this one operation
    # keeps the Ollama model and the pgvector HNSW path warm.
    warm_hnsw(args.company, queries, args.candidates)

    profile = resolve_generation(
        load_tenant_registry(require_api_keys=False).get(args.company)
    ).profile
    bm25 = warm_bm25(
        profile.storage.bm25_path,
        queries,
        args.candidates,
    )
    query_ms = ", ".join(f"{value:.0f}" for value in bm25["query_ms"])
    result_counts = ", ".join(str(value) for value in bm25["result_counts"])
    print(
        f"BM25 warm-up complete company={args.company} "
        f"file_bytes={int(bm25['file_bytes'])} "
        f"file_read_ms={bm25['file_read_ms']:.0f} "
        f"queries={len(queries)} query_ms=[{query_ms}] "
        f"results=[{result_counts}]",
        flush=True,
    )
    print(
        f"Search-path warm-up complete company={args.company} "
        f"duration_ms={(time.perf_counter() - started) * 1000:.0f}",
        flush=True,
    )


if __name__ == "__main__":
    main()
