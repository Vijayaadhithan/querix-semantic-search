import sys
import json
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

    assert "metadata ->> %s = %s" in clause
    assert "metadata ->> %s IN (%s, %s)" in clause
    assert "::double precision >=" in clause
    assert "::double precision <=" in clause
    assert params == [
        "city_name",
        "Chennai",
        "subcategory_name",
        "Bike",
        "Car",
        "rental_fee",
        r"^-?[0-9]+(\.[0-9]+)?$",
        "rental_fee",
        100.0,
        "rental_fee",
        r"^-?[0-9]+(\.[0-9]+)?$",
        "rental_fee",
        1000.0,
    ]


def test_pgvector_metadata_filter_rejects_unknown_operator():
    try:
        PgVectorCollection._metadata_filter_sql({"city_name": {"$ne": "Chennai"}})
    except ValueError as exc:
        assert "$ne" in str(exc)
    else:
        raise AssertionError("Expected unsupported pgvector operator to fail.")


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
