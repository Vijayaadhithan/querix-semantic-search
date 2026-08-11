import sqlite3
from pathlib import Path

import pytest

from scripts.warm_search_paths import prewarm_file, warm_bm25


def _bm25_index(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE VIRTUAL TABLE products_fts USING fts5(content)")
        connection.executemany(
            "INSERT INTO products_fts(content) VALUES (?)",
            [
                ("comfortable vehicle for long distance travel",),
                ("camera and equipment for an event",),
            ],
        )


def test_warm_bm25_runs_read_only_representative_queries(tmp_path):
    path = tmp_path / "bm25.sqlite3"
    _bm25_index(path)

    result = warm_bm25(
        path,
        [
            "comfortable vehicle",
            "camera event",
        ],
        candidates=20,
    )

    assert result["result_counts"] == [1, 1]
    assert len(result["query_ms"]) == 2
    assert result["file_bytes"] == path.stat().st_size
    assert result["file_read_ms"] >= 0


def test_warm_bm25_rejects_empty_results(tmp_path):
    path = tmp_path / "bm25.sqlite3"
    _bm25_index(path)

    with pytest.raises(RuntimeError, match="returned no rows"):
        warm_bm25(path, ["unfindableterm"], candidates=20)


def test_warm_bm25_requires_existing_index(tmp_path):
    with pytest.raises(RuntimeError, match="does not exist"):
        warm_bm25(tmp_path / "missing.sqlite3", ["vehicle"], candidates=20)


def test_prewarm_file_rejects_invalid_chunk_size(tmp_path):
    path = tmp_path / "bm25.sqlite3"
    path.write_bytes(b"index")

    with pytest.raises(ValueError, match="chunk_size"):
        prewarm_file(path, chunk_size=0)
