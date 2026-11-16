import sqlite3

import pytest

from analytics_service.sqlite import analytics_connection


def test_connections_preserve_autocommit_and_enforce_foreign_keys(tmp_path):
    path = tmp_path / "analytics.sqlite3"
    with analytics_connection(path) as connection:
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))"
        )
        connection.execute("INSERT INTO parent VALUES (1)")

    with analytics_connection(path) as connection:
        assert connection.execute("SELECT id FROM parent").fetchone()["id"] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO child VALUES (2)")


def test_connection_closes_and_rolls_back_an_unfinished_transaction(tmp_path):
    path = tmp_path / "analytics.sqlite3"
    with pytest.raises(RuntimeError, match="interrupted"):
        with analytics_connection(path) as connection:
            connection.execute("CREATE TABLE records (id INTEGER)")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO records VALUES (1)")
            raise RuntimeError("interrupted")

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with analytics_connection(path) as reopened:
        assert reopened.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        reopened.execute("INSERT INTO records VALUES (2)")


def test_connection_closes_when_initialization_fails(monkeypatch):
    class PragmaFailureConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("PRAGMA"):
                raise sqlite3.OperationalError("initialization failed")
            return super().execute(sql, *args, **kwargs)

    connection = sqlite3.connect(":memory:", factory=PragmaFailureConnection)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connection)
    with pytest.raises(sqlite3.OperationalError, match="initialization failed"):
        with analytics_connection(":memory:"):
            pytest.fail("A connection with failed initialization must not be used")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
