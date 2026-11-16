"""SQLite connection policy shared by persistent analytics stores."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def analytics_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open an autocommit connection; callers own explicit transactions."""
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        yield connection
    finally:
        connection.close()
