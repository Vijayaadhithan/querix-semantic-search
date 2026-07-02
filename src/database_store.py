from __future__ import annotations

from contextlib import contextmanager
from queue import Empty, LifoQueue
import threading
import time
from typing import TypeAlias

from mysql_store import (
    DEFAULT_MYSQL_CONFIG,
    MySQLRuntimeConfig,
    count_mysql_rows,
    detect_mysql_primary_key,
    fetch_mysql_columns,
    fetch_product_types_by_ids as fetch_mysql_product_types_by_ids,
    fetch_products_by_ids as fetch_mysql_products_by_ids,
    iter_mysql_rows,
    mysql_connection,
    mysql_source_name,
    require_pymysql,
)
from postgres_store import (
    PostgresRuntimeConfig,
    count_postgres_rows,
    detect_postgres_primary_key,
    fetch_postgres_columns,
    fetch_postgres_product_types_by_ids,
    fetch_postgres_products_by_ids,
    iter_postgres_rows,
    postgres_connection,
    postgres_source_name,
)


DatabaseRuntimeConfig: TypeAlias = MySQLRuntimeConfig | PostgresRuntimeConfig


class DatabaseConnectionPool:
    """Small bounded synchronous pool shared by one tenant search engine."""

    def __init__(self, config: DatabaseRuntimeConfig):
        self.config = config
        self.min_size = config.pool_min_size
        self.max_size = config.pool_max_size
        self.timeout_seconds = config.pool_timeout_seconds
        self._idle: LifoQueue = LifoQueue(maxsize=self.max_size)
        self._lock = threading.Lock()
        self._created = 0
        self._closed = False
        for _ in range(self.min_size):
            self._idle.put(self._create_reserved())

    def _reserve(self) -> bool:
        with self._lock:
            if self._closed or self._created >= self.max_size:
                return False
            self._created += 1
            return True

    def _unreserve(self) -> None:
        with self._lock:
            self._created = max(self._created - 1, 0)

    def _new_connection(self):
        if isinstance(self.config, PostgresRuntimeConfig):
            return postgres_connection(self.config, dict_rows=True)
        pymysql = require_pymysql()
        return mysql_connection(
            cursorclass=pymysql.cursors.DictCursor,
            config=self.config,
        )

    def _create_reserved(self):
        if not self._reserve():
            raise RuntimeError("Database connection pool is full")
        try:
            return self._new_connection()
        except Exception:
            self._unreserve()
            raise

    def _usable(self, connection) -> bool:
        try:
            if isinstance(self.config, PostgresRuntimeConfig):
                return not connection.closed and not connection.broken
            connection.ping(reconnect=False)
            return bool(connection.open)
        except Exception:
            return False

    def _discard(self, connection) -> None:
        try:
            connection.close()
        finally:
            self._unreserve()

    def _acquire(self):
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                connection = self._idle.get_nowait()
            except Empty:
                if self._reserve():
                    try:
                        return self._new_connection()
                    except Exception:
                        self._unreserve()
                        raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "Timed out waiting for a tenant database connection"
                    )
                try:
                    connection = self._idle.get(timeout=remaining)
                except Empty as exc:
                    raise TimeoutError(
                        "Timed out waiting for a tenant database connection"
                    ) from exc
            if self._usable(connection):
                return connection
            self._discard(connection)

    @contextmanager
    def connection(self):
        connection = self._acquire()
        try:
            yield connection
        except BaseException:
            self._discard(connection)
            raise
        else:
            if self._closed or not self._usable(connection):
                self._discard(connection)
            else:
                self._idle.put(connection)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        while True:
            try:
                connection = self._idle.get_nowait()
            except Empty:
                break
            self._discard(connection)


def create_database_pool(
    config: DatabaseRuntimeConfig | None,
) -> DatabaseConnectionPool | None:
    return DatabaseConnectionPool(config) if config is not None else None


def resolved_database_config(
    config: DatabaseRuntimeConfig | None = None,
) -> DatabaseRuntimeConfig:
    return config or DEFAULT_MYSQL_CONFIG


def database_backend(config: DatabaseRuntimeConfig | None = None) -> str:
    return (
        "postgres"
        if isinstance(resolved_database_config(config), PostgresRuntimeConfig)
        else "mysql"
    )


def database_source_name(
    config: DatabaseRuntimeConfig | None = None,
) -> str:
    resolved = resolved_database_config(config)
    if isinstance(resolved, PostgresRuntimeConfig):
        return postgres_source_name(resolved)
    return mysql_source_name(resolved)


def fetch_database_columns(
    config: DatabaseRuntimeConfig | None = None,
) -> list[str]:
    resolved = resolved_database_config(config)
    if isinstance(resolved, PostgresRuntimeConfig):
        return fetch_postgres_columns(resolved)
    return fetch_mysql_columns(config=resolved)


def detect_database_primary_key(
    columns: list[str],
    override: str | None = None,
    config: DatabaseRuntimeConfig | None = None,
) -> str | None:
    resolved = resolved_database_config(config)
    if isinstance(resolved, PostgresRuntimeConfig):
        return detect_postgres_primary_key(resolved, columns, override)
    return detect_mysql_primary_key(columns, override, config=resolved)


def count_database_rows(
    content_column: str | None = None,
    config: DatabaseRuntimeConfig | None = None,
) -> int:
    resolved = resolved_database_config(config)
    if isinstance(resolved, PostgresRuntimeConfig):
        return count_postgres_rows(resolved, content_column)
    return count_mysql_rows(content_column, config=resolved)


def iter_database_rows(
    content_column: str | None,
    primary_key_column: str | None,
    limit: int | None = None,
    config: DatabaseRuntimeConfig | None = None,
    fetch_batch_size: int = 1000,
):
    resolved = resolved_database_config(config)
    if isinstance(resolved, PostgresRuntimeConfig):
        yield from iter_postgres_rows(
            resolved,
            content_column,
            primary_key_column,
            limit,
            fetch_batch_size=fetch_batch_size,
        )
        return
    yield from iter_mysql_rows(
        content_column,
        primary_key_column,
        limit,
        config=resolved,
        fetch_batch_size=fetch_batch_size,
    )


def fetch_product_types_by_ids(
    product_ids,
    connection=None,
    config: DatabaseRuntimeConfig | None = None,
) -> dict[str, str]:
    resolved = resolved_database_config(config)
    if isinstance(resolved, PostgresRuntimeConfig):
        return fetch_postgres_product_types_by_ids(
            resolved,
            product_ids,
            connection,
        )
    return fetch_mysql_product_types_by_ids(
        product_ids,
        connection=connection,
        config=resolved,
    )


def fetch_products_by_ids(
    product_ids,
    connection=None,
    config: DatabaseRuntimeConfig | None = None,
) -> list[dict]:
    resolved = resolved_database_config(config)
    if isinstance(resolved, PostgresRuntimeConfig):
        return fetch_postgres_products_by_ids(
            resolved,
            product_ids,
            connection,
        )
    return fetch_mysql_products_by_ids(
        product_ids,
        connection=connection,
        config=resolved,
    )
