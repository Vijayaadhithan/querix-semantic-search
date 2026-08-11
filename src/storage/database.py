from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from queue import Empty, LifoQueue
from typing import TypeAlias

from storage.mysql import (
    DEFAULT_MYSQL_CONFIG,
    MySQLRuntimeConfig,
    count_mysql_rows,
    detect_mysql_primary_key,
    fetch_mysql_columns,
    iter_mysql_rows,
    mysql_connection,
    mysql_source_name,
    require_pymysql,
)
from storage.mysql import (
    fetch_product_types_by_ids as fetch_mysql_product_types_by_ids,
)
from storage.mysql import (
    fetch_products_by_ids as fetch_mysql_products_by_ids,
)
from storage.postgres import (
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

    MYSQL_VALIDATION_INTERVAL_SECONDS = 30.0

    def __init__(self, config: DatabaseRuntimeConfig):
        self.config = config
        self.min_size = config.pool_min_size
        self.max_size = config.pool_max_size
        self.timeout_seconds = config.pool_timeout_seconds
        self.validation_interval_seconds = float(
            getattr(
                config,
                "pool_validation_interval_seconds",
                self.MYSQL_VALIDATION_INTERVAL_SECONDS,
            )
        )
        self._idle: LifoQueue = LifoQueue(maxsize=self.max_size)
        self._lock = threading.Lock()
        self._created = 0
        self._closed = False
        self._last_validated: dict[int, float] = {}
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
            connection = postgres_connection(self.config, dict_rows=True)
        else:
            pymysql = require_pymysql()
            connection = mysql_connection(
                cursorclass=pymysql.cursors.DictCursor,
                config=self.config,
            )
        self._mark_validated(connection)
        return connection

    def _mark_validated(self, connection) -> None:
        with self._lock:
            self._last_validated[id(connection)] = time.monotonic()

    def _locally_open(self, connection) -> bool:
        if isinstance(self.config, PostgresRuntimeConfig):
            return not connection.closed and not connection.broken
        return bool(connection.open)

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
            if not self._locally_open(connection):
                return False
            if isinstance(self.config, PostgresRuntimeConfig):
                return True
            with self._lock:
                last_validated = self._last_validated.get(
                    id(connection),
                    0.0,
                )
            if time.monotonic() - last_validated < self.validation_interval_seconds:
                return True
            connection.ping(reconnect=False)
            self._mark_validated(connection)
            return True
        except Exception:
            return False

    def _discard(self, connection) -> None:
        try:
            connection.close()
        finally:
            with self._lock:
                self._last_validated.pop(id(connection), None)
            self._unreserve()

    def validate_idle_connections(self) -> dict[str, int]:
        """Validate every currently idle connection without blocking checkouts.

        A LIFO pool normally exercises only its newest connection during quiet
        readiness checks. Older MySQL connections can therefore cross the
        server's ``wait_timeout`` and make the next concurrent search pay for
        dead-socket detection. Draining the idle queue lets maintenance touch
        every available connection while checked-out connections continue to
        serve traffic normally.
        """
        idle_connections = []
        while True:
            try:
                idle_connections.append(self._idle.get_nowait())
            except Empty:
                break

        healthy_connections = []
        discarded = 0
        for connection in idle_connections:
            if self._usable(connection):
                healthy_connections.append(connection)
            else:
                self._discard(connection)
                discarded += 1

        for connection in healthy_connections:
            if self._closed:
                self._discard(connection)
            else:
                self._idle.put(connection)

        created = 0
        while True:
            with self._lock:
                needs_connection = not self._closed and self._created < self.min_size
            if not needs_connection:
                break
            try:
                connection = self._create_reserved()
            except Exception:
                break
            if self._closed:
                self._discard(connection)
                break
            self._idle.put(connection)
            created += 1

        return {
            "checked": len(idle_connections),
            "healthy": len(healthy_connections),
            "discarded": discarded,
            "created": created,
        }

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
            if self._closed or not self._locally_open(connection):
                self._discard(connection)
            else:
                # A successful checkout is stronger evidence of a live
                # connection than another remote ping. Mark it healthy and
                # validate again only after it has actually been idle.
                self._mark_validated(connection)
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
