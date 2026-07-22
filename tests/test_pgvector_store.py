import sys
import json
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pgvector_store
from pgvector_store import PgVectorCollection
from postgres_store import PostgresRuntimeConfig


def test_pgvector_metadata_filter_supports_collection_where_shape():
    clause, params = PgVectorCollection._metadata_filter_sql(
        {
            "$and": [
                {"city_name": "Chennai"},
                {"subcategory_name": {"$in": ["Bike", "Car"]}},
                {"rental_fee": {"$gte": 100, "$lte": 1000}},
            ]
        }
    )

    assert "metadata ->> 'city_name' = %s" in clause
    assert "metadata ->> 'subcategory_name' IN (%s, %s)" in clause
    assert "::double precision >=" in clause
    assert "::double precision <=" in clause
    assert params == [
        "Chennai",
        "Bike",
        "Car",
        r"^-?[0-9]+(\.[0-9]+)?$",
        100.0,
        r"^-?[0-9]+(\.[0-9]+)?$",
        1000.0,
    ]


def test_pgvector_metadata_filter_rejects_unknown_operator():
    try:
        PgVectorCollection._metadata_filter_sql({"city_name": {"$ne": "Chennai"}})
    except ValueError as exc:
        assert "$ne" in str(exc)
    else:
        raise AssertionError("Expected unsupported pgvector operator to fail.")


def test_pgvector_exact_ranks_complete_selective_filter(monkeypatch):
    statements = []

    class FakeCursor:
        rows = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            compact = " ".join(query.split())
            statements.append((compact, params))
            if compact.startswith("WITH eligible AS MATERIALIZED"):
                self.rows = [
                    {
                        "id": "nearest",
                        "document": "nearest car",
                        "metadata": {"city_name": "Chennai"},
                        "distance": 0.05,
                        "eligible_count": 2,
                    },
                    {
                        "id": "second",
                        "document": "second car",
                        "metadata": {"city_name": "Chennai"},
                        "distance": 0.1,
                        "eligible_count": 2,
                    },
                ]

        def fetchall(self):
            return self.rows

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(
        pgvector_store,
        "postgres_connection",
        lambda *_args, **_kwargs: FakeConnection(),
    )
    collection = PgVectorCollection.__new__(PgVectorCollection)
    collection.config = PostgresRuntimeConfig(
        host="localhost",
        port=5432,
        database="vectors",
        user="vectors",
        password="secret",
    )
    collection.table = "gainr_vectors"
    collection.dimensions = 2
    collection.hnsw_ef_search = 100
    collection._query_state = threading.local()

    results = collection.query(
        query_embeddings=[[0.1, 0.2]],
        n_results=2,
        where={"city_name": "Chennai"},
        include=["documents", "metadatas", "distances"],
        exact_filter_max_rows=100,
    )

    assert results["ids"] == [["nearest", "second"]]
    assert collection.last_query_metrics()["strategy"] == "exact_filtered"
    assert collection.last_query_metrics()["eligible_rows"] == 2
    exact_sql, exact_params = next(
        entry for entry in statements if entry[0].startswith("WITH eligible")
    )
    assert "metadata ->> 'city_name' = %s" in exact_sql
    assert exact_params[0] == "Chennai"


def test_pgvector_broad_filtered_subset_falls_back_to_hnsw(monkeypatch):
    statements = []

    class FakeCursor:
        rows = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            compact = " ".join(query.split())
            statements.append(compact)
            if compact.startswith("WITH eligible AS MATERIALIZED"):
                self.rows = [
                    {
                        "id": None,
                        "document": None,
                        "metadata": None,
                        "distance": None,
                        "eligible_count": 101,
                    }
                ]
            elif compact.startswith("SELECT id, document, metadata"):
                self.rows = [
                    {
                        "id": "hnsw-result",
                        "document": "broad match",
                        "metadata": {"state_name": "Tamil Nadu"},
                        "distance": 0.2,
                    }
                ]

        def fetchall(self):
            return self.rows

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(
        pgvector_store,
        "postgres_connection",
        lambda *_args, **_kwargs: FakeConnection(),
    )
    collection = PgVectorCollection.__new__(PgVectorCollection)
    collection.config = PostgresRuntimeConfig(
        host="localhost",
        port=5432,
        database="vectors",
        user="vectors",
        password="secret",
    )
    collection.table = "gainr_vectors"
    collection.dimensions = 2
    collection.hnsw_ef_search = 100
    collection._query_state = threading.local()

    results = collection.query(
        query_embeddings=[[0.1, 0.2]],
        n_results=1,
        where={"state_name": "Tamil Nadu"},
        include=["documents", "metadatas", "distances"],
        exact_filter_max_rows=100,
    )

    assert results["ids"] == [["hnsw-result"]]
    assert collection.last_query_metrics()["strategy"] == "hnsw"
    assert collection.last_query_metrics()["eligible_rows"] == 101
    assert sum(
        sql.startswith("SELECT id, document, metadata") for sql in statements
    ) == 1


def test_pgvector_broad_filter_can_use_bounded_unfiltered_hnsw(monkeypatch):
    statements = []

    class FakeCursor:
        rows = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            compact = " ".join(query.split())
            statements.append((compact, params))
            if compact.startswith("WITH eligible AS MATERIALIZED"):
                self.rows = [
                    {
                        "id": None,
                        "document": None,
                        "metadata": None,
                        "distance": None,
                        "eligible_count": 101,
                    }
                ]
            elif compact.startswith("SELECT id, document, metadata"):
                self.rows = [
                    {
                        "id": "hnsw-result",
                        "document": "broad match",
                        "metadata": {"state_name": "Tamil Nadu"},
                        "distance": 0.2,
                    }
                ]

        def fetchall(self):
            return self.rows

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(
        pgvector_store,
        "postgres_connection",
        lambda *_args, **_kwargs: FakeConnection(),
    )
    collection = PgVectorCollection.__new__(PgVectorCollection)
    collection.config = PostgresRuntimeConfig(
        host="localhost",
        port=5432,
        database="vectors",
        user="vectors",
        password="secret",
    )
    collection.table = "gainr_vectors"
    collection.dimensions = 2
    collection.hnsw_ef_search = 100
    collection._query_state = threading.local()

    results = collection.query(
        query_embeddings=[[0.1, 0.2]],
        n_results=80,
        where={"state_name": "Tamil Nadu"},
        include=["documents", "metadatas", "distances"],
        exact_filter_max_rows=100,
        post_filter_n_results=800,
    )

    assert results["ids"] == [["hnsw-result"]]
    assert collection.last_query_metrics()["strategy"] == "hnsw_post_filter"
    query_sql, query_params = next(
        entry for entry in statements
        if entry[0].startswith("SELECT id, document, metadata")
    )
    assert "metadata ->> 'state_name'" not in query_sql
    assert query_params[-1] == 800


def test_pgvector_optimized_exact_filter_avoids_id_rejoin(monkeypatch):
    statements = []

    class FakeCursor:
        rows = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            compact = " ".join(query.split())
            statements.append((compact, params))
            if compact.startswith("SELECT COUNT(*) AS eligible_count"):
                self.rows = [{"eligible_count": 2}]
            elif compact.startswith("SELECT id, document, metadata"):
                self.rows = [
                    {
                        "id": "nearest",
                        "document": "nearest car",
                        "metadata": {"city_name": "Chennai"},
                        "distance": 0.05,
                    },
                    {
                        "id": "second",
                        "document": "second car",
                        "metadata": {"city_name": "Chennai"},
                        "distance": 0.1,
                    },
                ]

        def fetchone(self):
            return self.rows[0]

        def fetchall(self):
            return self.rows

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(
        pgvector_store,
        "postgres_connection",
        lambda *_args, **_kwargs: FakeConnection(),
    )
    collection = PgVectorCollection.__new__(PgVectorCollection)
    collection.config = PostgresRuntimeConfig(
        host="localhost",
        port=5432,
        database="vectors",
        user="vectors",
        password="secret",
    )
    collection.table = "gainr_vectors"
    collection.dimensions = 2
    collection.hnsw_ef_search = 100
    collection.query_mode = "optimized"
    collection._query_state = threading.local()

    results = collection.query(
        query_embeddings=[[0.1, 0.2]],
        n_results=2,
        where={"city_name": "Chennai"},
        include=["documents", "metadatas", "distances"],
        exact_filter_max_rows=100,
    )

    assert results["ids"] == [["nearest", "second"]]
    metrics = collection.last_query_metrics()
    assert metrics["query_mode"] == "optimized"
    assert metrics["strategy"] == "exact_filtered"
    assert not any(sql.startswith("WITH eligible") for sql, _ in statements)
    exact_sql = next(
        sql
        for sql, _params in statements
        if sql.startswith("SELECT id, document, metadata")
    )
    assert "metadata ->> 'city_name' = %s" in exact_sql
    assert "JOIN eligible" not in exact_sql


def test_pgvector_optimized_post_filter_fetches_only_filtered_payload(
    monkeypatch,
):
    statements = []

    class FakeCursor:
        rows = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            compact = " ".join(query.split())
            statements.append((compact, params))
            if compact.startswith("SELECT COUNT(*) AS eligible_count"):
                self.rows = [{"eligible_count": 101}]
            elif compact.startswith("WITH nearest AS MATERIALIZED"):
                self.rows = [
                    {
                        "id": "filtered-result",
                        "document": "Tamil Nadu camera",
                        "metadata": {"state_name": "Tamil Nadu"},
                        "distance": 0.2,
                    }
                ]

        def fetchone(self):
            return self.rows[0]

        def fetchall(self):
            return self.rows

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(
        pgvector_store,
        "postgres_connection",
        lambda *_args, **_kwargs: FakeConnection(),
    )
    collection = PgVectorCollection.__new__(PgVectorCollection)
    collection.config = PostgresRuntimeConfig(
        host="localhost",
        port=5432,
        database="vectors",
        user="vectors",
        password="secret",
    )
    collection.table = "gainr_vectors"
    collection.dimensions = 2
    collection.hnsw_ef_search = 100
    collection.query_mode = "optimized"
    collection._query_state = threading.local()

    results = collection.query(
        query_embeddings=[[0.1, 0.2]],
        n_results=80,
        where={"state_name": "Tamil Nadu"},
        include=["documents", "metadatas", "distances"],
        exact_filter_max_rows=100,
        post_filter_n_results=800,
    )

    assert results["ids"] == [["filtered-result"]]
    metrics = collection.last_query_metrics()
    assert metrics["strategy"] == "hnsw_post_filter"
    optimized_sql, optimized_params = next(
        entry
        for entry in statements
        if entry[0].startswith("WITH nearest AS MATERIALIZED")
    )
    assert "WHERE metadata ->> 'state_name' = %s" in optimized_sql
    assert "LIMIT %s" in optimized_sql
    assert optimized_params[2] == 800
    assert optimized_params[-1] == 80


def test_pgvector_shadow_serves_legacy_and_records_equivalence(monkeypatch):
    class FakeCursor:
        rows = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            compact = " ".join(query.split())
            if compact.startswith("WITH eligible AS MATERIALIZED"):
                self.rows = [
                    {
                        "id": "same",
                        "document": "same document",
                        "metadata": {"city_name": "Chennai"},
                        "distance": 0.05,
                        "eligible_count": 1,
                    }
                ]
            elif compact.startswith("SELECT COUNT(*) AS eligible_count"):
                self.rows = [{"eligible_count": 1}]
            elif compact.startswith("SELECT id, document, metadata"):
                self.rows = [
                    {
                        "id": "same",
                        "document": "same document",
                        "metadata": {"city_name": "Chennai"},
                        "distance": 0.05,
                    }
                ]

        def fetchone(self):
            return self.rows[0]

        def fetchall(self):
            return self.rows

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(
        pgvector_store,
        "postgres_connection",
        lambda *_args, **_kwargs: FakeConnection(),
    )
    collection = PgVectorCollection.__new__(PgVectorCollection)
    collection.config = PostgresRuntimeConfig(
        host="localhost",
        port=5432,
        database="vectors",
        user="vectors",
        password="secret",
    )
    collection.table = "gainr_vectors"
    collection.dimensions = 2
    collection.hnsw_ef_search = 100
    collection.query_mode = "shadow"
    collection._query_state = threading.local()

    results = collection.query(
        query_embeddings=[[0.1, 0.2]],
        n_results=1,
        where={"city_name": "Chennai"},
        include=["documents", "metadatas", "distances"],
        exact_filter_max_rows=100,
    )

    assert results["ids"] == [["same"]]
    metrics = collection.last_query_metrics()
    assert metrics["query_mode"] == "shadow"
    assert metrics["shadow_equal"] is True
    assert metrics["shadow_legacy_rows"] == 1
    assert metrics["shadow_optimized_rows"] == 1


def test_pgvector_shadow_optimization_failure_serves_legacy(monkeypatch):
    class FakeCursor:
        rows = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            compact = " ".join(query.split())
            if compact.startswith("WITH eligible AS MATERIALIZED"):
                self.rows = [
                    {
                        "id": "legacy",
                        "document": "legacy document",
                        "metadata": {"city_name": "Chennai"},
                        "distance": 0.05,
                        "eligible_count": 1,
                    }
                ]
            elif compact.startswith("SELECT COUNT(*) AS eligible_count"):
                raise RuntimeError("optimized SQL unavailable")

        def fetchall(self):
            return self.rows

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(
        pgvector_store,
        "postgres_connection",
        lambda *_args, **_kwargs: FakeConnection(),
    )
    collection = PgVectorCollection.__new__(PgVectorCollection)
    collection.config = PostgresRuntimeConfig(
        host="localhost",
        port=5432,
        database="vectors",
        user="vectors",
        password="secret",
    )
    collection.table = "gainr_vectors"
    collection.dimensions = 2
    collection.hnsw_ef_search = 100
    collection.query_mode = "shadow"
    collection._query_state = threading.local()

    results = collection.query(
        query_embeddings=[[0.1, 0.2]],
        n_results=1,
        where={"city_name": "Chennai"},
        include=["documents", "metadatas", "distances"],
        exact_filter_max_rows=100,
    )

    assert results["ids"] == [["legacy"]]
    metrics = collection.last_query_metrics()
    assert metrics["shadow_equal"] is False
    assert metrics["shadow_error"] == "RuntimeError"


def test_pgvector_source_migration_keeps_existing_target_rows(monkeypatch):
    state = {
        "source_batches": [
            [
                {
                    "id": "old-1",
                    "metadata": {
                        "source_file": "mysql:local.search_ready",
                        "source_database": "local",
                        "primary_key_value": 1,
                    },
                },
                {
                    "id": "old-2",
                    "metadata": {
                        "source_file": "mysql:local.search_ready",
                        "source_database": "local",
                        "primary_key_value": 2,
                    },
                },
            ],
            [],
        ],
        "deleted": [],
        "updated": [],
        "commits": 0,
        "rollbacks": 0,
    }

    class FakeCursor:
        def __init__(self):
            self.rows = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            compact = " ".join(query.split())
            if "to_regclass" in compact:
                self.rows = [{"table_name": "public.gainr_vectors"}]
            elif "SELECT id, metadata" in compact:
                self.rows = state["source_batches"].pop(0)
            elif compact.startswith("SELECT id FROM"):
                self.rows = [{"id": "new-1"}]
            elif compact.startswith("DELETE FROM"):
                state["deleted"].extend(params[0])

        def executemany(self, _query, rows):
            state["updated"].extend(rows)

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            state["commits"] += 1

        def rollback(self):
            state["rollbacks"] += 1

    monkeypatch.setattr(
        pgvector_store,
        "postgres_connection",
        lambda *_args, **_kwargs: FakeConnection(),
    )
    config = PostgresRuntimeConfig(
        host="localhost",
        port=5432,
        database="vectors",
        user="vectors",
        password="secret",
    )
    collection = PgVectorCollection(config, "gainr_vectors", 768)

    migrated, kept = collection.migrate_source_namespace(
        "mysql:local.search_ready",
        "mysql:production.search_ready",
        target_database="production",
        target_id=lambda primary_key: f"new-{primary_key}",
    )

    assert (migrated, kept) == (1, 1)
    assert state["deleted"] == ["old-1"]
    assert state["updated"][0][0] == "new-2"
    assert state["updated"][0][2] == "old-2"
    updated_metadata = json.loads(state["updated"][0][1])
    assert updated_metadata["source_file"] == (
        "mysql:production.search_ready"
    )
    assert updated_metadata["source_database"] == "production"
    assert state["commits"] == 1
    assert state["rollbacks"] == 1
