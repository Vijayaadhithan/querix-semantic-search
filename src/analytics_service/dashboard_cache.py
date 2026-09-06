from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from threading import Lock
from typing import Any


class DashboardActivityCache:
    """Cache summaries while allowing only one decoded snapshot per API process.

    Concurrent cache misses must not each allocate a snapshot's query records.
    Recheck the cache after waiting so identical requests share the completed
    summary. A separate cache lock keeps existing hits available during a build.
    """

    def __init__(self, maxsize: int = 32):
        if maxsize < 1:
            raise ValueError("Dashboard cache size must be positive")
        self._maxsize = maxsize
        self._entries: OrderedDict[tuple[object, ...], dict[str, Any]] = OrderedDict()
        self._cache_lock = Lock()
        self._build_lock = Lock()

    def _lookup(self, key: tuple[object, ...]) -> dict[str, Any] | None:
        with self._cache_lock:
            if key not in self._entries:
                return None
            self._entries.move_to_end(key)
            return self._entries[key]

    def get(
        self,
        key: tuple[object, ...],
        build: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        cached = self._lookup(key)
        if cached is not None:
            return cached
        with self._build_lock:
            cached = self._lookup(key)
            if cached is not None:
                return cached
            result = build()
            with self._cache_lock:
                self._entries[key] = result
                while len(self._entries) > self._maxsize:
                    self._entries.popitem(last=False)
            return result
