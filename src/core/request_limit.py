from __future__ import annotations

import threading
import time
from collections import deque


class RequestWindowLimiter:
    """Thread-safe rolling request budget with an optional Redis coordinator."""

    def __init__(
        self,
        requests_per_window: int,
        *,
        window_seconds: float = 60,
        clock=time.monotonic,
        redis_cache=None,
        scope: str | None = None,
    ):
        if requests_per_window <= 0 or window_seconds <= 0:
            raise ValueError("request budget and window must be greater than zero")
        if redis_cache is not None and not scope:
            raise ValueError("scope is required when Redis coordination is enabled")
        self.requests_per_window = requests_per_window
        self.window_seconds = window_seconds
        self.clock = clock
        self.redis_cache = redis_cache
        self.scope = scope
        self._requests = deque()
        self._lock = threading.Lock()

    def allow(self) -> tuple[bool, float]:
        if self.redis_cache is not None:
            result = self.redis_cache.allow_request_window(
                self.scope,
                self.requests_per_window,
                self.window_seconds,
            )
            if result is not None:
                return result
        return self._allow_memory()

    def _allow_memory(self) -> tuple[bool, float]:
        now = self.clock()
        cutoff = now - self.window_seconds
        with self._lock:
            while self._requests and self._requests[0] <= cutoff:
                self._requests.popleft()
            if len(self._requests) >= self.requests_per_window:
                return False, max(
                    self.window_seconds - (now - self._requests[0]),
                    0,
                )
            self._requests.append(now)
            return True, 0.0


class TokenBudgetLimiter:
    """Thread-safe weighted token bucket with optional Redis coordination."""

    def __init__(
        self,
        tokens_per_minute: int,
        *,
        clock=time.monotonic,
        redis_cache=None,
        scope: str | None = None,
    ):
        if tokens_per_minute <= 0:
            raise ValueError("token budget must be greater than zero")
        if redis_cache is not None and not scope:
            raise ValueError("scope is required when Redis coordination is enabled")
        self.tokens_per_minute = tokens_per_minute
        self.clock = clock
        self.redis_cache = redis_cache
        self.scope = scope
        self._tokens = float(tokens_per_minute)
        self._updated = clock()
        self._lock = threading.Lock()

    def allow(self, estimated_tokens: int) -> tuple[bool, float]:
        if estimated_tokens <= 0:
            raise ValueError("estimated token usage must be greater than zero")
        if estimated_tokens > self.tokens_per_minute:
            return False, 60.0
        if self.redis_cache is not None:
            result = self.redis_cache.allow_token_budget(
                self.scope,
                self.tokens_per_minute,
                estimated_tokens,
            )
            if result is not None:
                return result
        return self._allow_memory(estimated_tokens)

    def _allow_memory(self, estimated_tokens: int) -> tuple[bool, float]:
        now = self.clock()
        refill_per_second = self.tokens_per_minute / 60
        with self._lock:
            elapsed = max(now - self._updated, 0.0)
            self._tokens = min(
                float(self.tokens_per_minute),
                self._tokens + elapsed * refill_per_second,
            )
            self._updated = now
            if self._tokens < estimated_tokens:
                retry_after = (estimated_tokens - self._tokens) / refill_per_second
                return False, max(retry_after, 0.0)
            self._tokens -= estimated_tokens
            return True, 0.0
