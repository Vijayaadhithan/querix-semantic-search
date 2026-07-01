import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import database_store
import mysql_store
import postgres_store


def mysql_config(**overrides):
    values = {
        "host": "mysql.example",
        "port": 3306,
        "database": "catalog",
        "user": "search",
        "password": "secret",
        "search_table": "search_ready",
        "content_column": "embedding_content",
        "bm25_column": "bm25_content",
        "search_id_column": "id",
        "result_table": "products",
        "result_id_column": "id",
    }
    values.update(overrides)
    return mysql_store.MySQLRuntimeConfig(**values)


def test_mysql_connection_applies_timeouts_and_tls_disable(monkeypatch):
    captured = {}

    class FakePyMySQL:
        @staticmethod
        def connect(**options):
            captured.update(options)
            return object()

    monkeypatch.setattr(mysql_store, "require_pymysql", lambda: FakePyMySQL)

    mysql_store.mysql_connection(
        config=mysql_config(
            connect_timeout_seconds=7,
            read_timeout_seconds=11,
            write_timeout_seconds=13,
            statement_timeout_ms=9000,
        )
    )

    assert captured["connect_timeout"] == 7
    assert captured["read_timeout"] == 11
    assert captured["write_timeout"] == 13
    assert captured["ssl_disabled"] is True
    assert captured["init_command"] == "SET SESSION MAX_EXECUTION_TIME=9000"


def test_postgres_connection_applies_tls_and_statement_timeout(monkeypatch):
    captured = {}

    class FakePsycopg:
        @staticmethod
        def connect(**options):
            captured.update(options)
            return object()

    monkeypatch.setattr(
        postgres_store,
        "require_psycopg",
        lambda: (FakePsycopg, object()),
    )
    config = postgres_store.PostgresRuntimeConfig(
        host="postgres.example",
        port=5432,
        database="catalog",
        user="search",
        password="secret",
        connect_timeout_seconds=8,
        statement_timeout_ms=12000,
        tls_mode="verify-full",
        tls_ca_file="/run/secrets/postgres-ca.pem",
    )

    postgres_store.postgres_connection(config, dict_rows=True)

    assert captured["connect_timeout"] == 8
    assert captured["sslmode"] == "verify-full"
    assert captured["sslrootcert"] == "/run/secrets/postgres-ca.pem"
    assert captured["options"] == "-c statement_timeout=12000"
    assert "password" in captured


def test_database_pool_reuses_connections_and_enforces_bound(monkeypatch):
    created = []

    class FakeConnection:
        open = True

        def ping(self, reconnect=False):
            assert reconnect is False

        def close(self):
            self.open = False

    config = mysql_config(pool_max_size=1, pool_timeout_seconds=0.01)
    pool = database_store.DatabaseConnectionPool(config)

    def new_connection():
        connection = FakeConnection()
        created.append(connection)
        return connection

    monkeypatch.setattr(pool, "_new_connection", new_connection)

    with pool.connection() as first:
        with pytest.raises(TimeoutError):
            with pool.connection():
                pass
    with pool.connection() as second:
        assert second is first

    assert len(created) == 1
    pool.close()
    assert first.open is False
