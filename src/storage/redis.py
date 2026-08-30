import json
import logging
import math
import time
from typing import Any

try:
    import redis
    from redis.exceptions import RedisError, WatchError
except ImportError:  # Allows diagnostics to explain a missing optional client.
    redis = None

    class RedisError(Exception):
        pass

    class WatchError(RedisError):
        pass


LOGGER = logging.getLogger("uvicorn.error")
TOKEN_BUCKET_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local refill_per_ms = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])
local cost = tonumber(ARGV[5]) or 1
local values = redis.call('HMGET', key, 'tokens', 'updated_ms')
local tokens = tonumber(values[1])
local updated_ms = tonumber(values[2])
if tokens == nil then
  tokens = capacity
  updated_ms = now
end
tokens = math.min(capacity, tokens + ((now - updated_ms) * refill_per_ms))
local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end
redis.call('HSET', key, 'tokens', tokens, 'updated_ms', now)
redis.call('PEXPIRE', key, ttl_ms)
return {allowed, math.floor(tokens)}
"""

SLIDING_WINDOW_SCRIPT = """
local key = KEYS[1]
local sequence_key = KEYS[2]
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local current_time = redis.call('TIME')
local now_ms = (tonumber(current_time[1]) * 1000) + math.floor(tonumber(current_time[2]) / 1000)
local cutoff_ms = now_ms - window_ms
redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff_ms)
local count = redis.call('ZCARD', key)
if count >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local retry_ms = window_ms
  if oldest[2] ~= nil then
    retry_ms = math.max(window_ms - (now_ms - tonumber(oldest[2])), 0)
  end
  redis.call('PEXPIRE', key, window_ms)
  return {0, retry_ms}
end
local sequence = redis.call('INCR', sequence_key)
local member = tostring(now_ms) .. ':' .. tostring(sequence)
redis.call('ZADD', key, now_ms, member)
redis.call('PEXPIRE', key, window_ms)
redis.call('PEXPIRE', sequence_key, window_ms)
return {1, 0}
"""


class RedisJsonCache:
    """Small resilient Redis JSON cache with a failure cooldown."""

    def __init__(
        self,
        url: str,
        key_prefix: str,
        socket_timeout_seconds: float = 1.0,
        retry_cooldown_seconds: float = 30.0,
    ):
        if redis is None:
            raise RuntimeError(
                "The redis Python package is not installed. Run: uv sync"
            )
        self._client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=socket_timeout_seconds,
            socket_timeout=socket_timeout_seconds,
            health_check_interval=30,
        )
        self.key_prefix = key_prefix.strip(":") or "semantic_ads"
        self.retry_cooldown_seconds = retry_cooldown_seconds
        self.connected = False
        self._retry_after = 0.0

    def _key(self, namespace: str, key: str) -> str:
        return f"{self.key_prefix}:{namespace}:{key}"

    def _can_attempt(self, force: bool = False) -> bool:
        return force or self.connected or time.monotonic() >= self._retry_after

    def _mark_success(self) -> None:
        self.connected = True
        self._retry_after = 0.0

    def _mark_failure(self, operation: str, exc: Exception) -> None:
        was_connected = self.connected
        self.connected = False
        self._retry_after = time.monotonic() + self.retry_cooldown_seconds
        if was_connected:
            LOGGER.warning(
                "Redis cache became unavailable operation=%s error=%s",
                operation,
                type(exc).__name__,
            )

    def ping(self, force: bool = False) -> bool:
        if not self._can_attempt(force):
            return False
        try:
            result = bool(self._client.ping())
        except (RedisError, OSError) as exc:
            self._mark_failure("ping", exc)
            return False
        if result:
            self._mark_success()
        return result

    def get_json(self, namespace: str, key: str) -> dict[str, Any] | None:
        if not self._can_attempt():
            return None
        try:
            raw = self._client.get(self._key(namespace, key))
            self._mark_success()
        except (RedisError, OSError) as exc:
            self._mark_failure("get", exc)
            return None
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            LOGGER.warning("Ignoring invalid JSON in the Redis cache.")
            return None
        return value if isinstance(value, dict) else None

    def set_json(
        self,
        namespace: str,
        key: str,
        value: dict[str, Any],
        ttl_seconds: int,
    ) -> bool:
        if not self._can_attempt():
            return False
        try:
            self._client.set(
                self._key(namespace, key),
                json.dumps(value, separators=(",", ":"), ensure_ascii=False),
                ex=ttl_seconds,
            )
            self._mark_success()
            return True
        except (RedisError, OSError, TypeError) as exc:
            self._mark_failure("set", exc)
            return False

    def prepend_unique_json_item(
        self,
        namespace: str,
        key: str,
        item: dict[str, Any],
        ttl_seconds: int,
        limit: int,
        identity_field: str = "value",
    ) -> dict[str, Any] | None:
        """Atomically prepend and deduplicate an item across API workers."""
        if not self._can_attempt() or ttl_seconds <= 0 or limit <= 0:
            return None
        redis_key = self._key(namespace, key)
        for _attempt in range(16):
            try:
                with self._client.pipeline() as pipe:
                    pipe.watch(redis_key)
                    raw = pipe.get(redis_key)
                    try:
                        payload = json.loads(raw) if raw is not None else {}
                    except (TypeError, json.JSONDecodeError):
                        payload = {}
                    raw_items = (
                        payload.get("items", []) if isinstance(payload, dict) else []
                    )
                    items = [
                        dict(existing)
                        for existing in raw_items
                        if isinstance(existing, dict)
                    ]
                    incoming = dict(item)
                    identity = str(incoming.get(identity_field, "")).casefold()
                    items = [
                        existing
                        for existing in items
                        if str(existing.get(identity_field, "")).casefold() != identity
                    ]
                    existing_ids = {
                        int(existing["id"])
                        for existing in items
                        if str(existing.get("id", "")).isdigit()
                    }
                    if str(incoming.get("id", "")).isdigit():
                        while int(incoming["id"]) in existing_ids:
                            incoming["id"] = int(incoming["id"]) + 1
                    updated = {"items": [incoming, *items][:limit]}
                    pipe.multi()
                    pipe.set(
                        redis_key,
                        json.dumps(
                            updated,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                        ex=ttl_seconds,
                    )
                    pipe.execute()
                self._mark_success()
                return updated
            except WatchError:
                continue
            except (RedisError, OSError, TypeError, ValueError) as exc:
                self._mark_failure("prepend_unique", exc)
                return None
        LOGGER.warning("Redis atomic cache update remained contended after retries.")
        return None

    def allow_rate_limit(
        self,
        scope: str,
        requests_per_minute: int,
        burst: int,
    ) -> tuple[bool, int] | None:
        if not self._can_attempt():
            return None
        refill_per_ms = requests_per_minute / 60_000
        ttl_ms = max(
            60_000,
            math.ceil((burst / refill_per_ms) * 2),
        )
        try:
            result = self._client.eval(
                TOKEN_BUCKET_SCRIPT,
                1,
                self._key("rate_limit", scope),
                int(time.time() * 1000),
                refill_per_ms,
                burst,
                ttl_ms,
            )
            self._mark_success()
        except (RedisError, OSError, TypeError, ValueError) as exc:
            self._mark_failure("rate_limit", exc)
            return None
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            return None
        return bool(int(result[0])), int(result[1])

    def allow_request_window(
        self,
        scope: str,
        requests_per_window: int,
        window_seconds: float = 60,
    ) -> tuple[bool, float] | None:
        """Atomically enforce a rolling request window across API workers."""
        if not self._can_attempt():
            return None
        window_ms = max(int(window_seconds * 1000), 1)
        key = self._key("provider_rate_limit", scope)
        try:
            result = self._client.eval(
                SLIDING_WINDOW_SCRIPT,
                2,
                key,
                f"{key}:sequence",
                requests_per_window,
                window_ms,
            )
            self._mark_success()
        except (RedisError, OSError, TypeError, ValueError) as exc:
            self._mark_failure("provider_rate_limit", exc)
            return None
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            return None
        return bool(int(result[0])), max(float(result[1]) / 1000, 0.0)

    def allow_token_budget(
        self,
        scope: str,
        tokens_per_minute: int,
        estimated_tokens: int,
    ) -> tuple[bool, float] | None:
        """Atomically reserve a weighted provider token budget across workers."""
        if not self._can_attempt():
            return None
        if estimated_tokens <= 0 or tokens_per_minute <= 0:
            raise ValueError("token budget and estimate must be greater than zero")
        if estimated_tokens > tokens_per_minute:
            return False, 60.0
        refill_per_ms = tokens_per_minute / 60_000
        ttl_ms = 120_000
        try:
            result = self._client.eval(
                TOKEN_BUCKET_SCRIPT,
                1,
                self._key("provider_token_limit", scope),
                int(time.time() * 1000),
                refill_per_ms,
                tokens_per_minute,
                ttl_ms,
                estimated_tokens,
            )
            self._mark_success()
        except (RedisError, OSError, TypeError, ValueError) as exc:
            self._mark_failure("provider_token_limit", exc)
            return None
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            return None
        allowed = bool(int(result[0]))
        remaining = max(float(result[1]), 0.0)
        retry_after = (
            0.0
            if allowed
            else max((estimated_tokens - remaining) / (tokens_per_minute / 60), 0.0)
        )
        return allowed, retry_after

    def close(self) -> None:
        self._client.close()


def create_redis_cache(
    enabled: bool,
    url: str,
    key_prefix: str,
) -> RedisJsonCache | None:
    if not enabled:
        LOGGER.info("Redis cache is disabled; using process memory.")
        return None
    try:
        cache = RedisJsonCache(url, key_prefix)
    except RuntimeError as exc:
        LOGGER.warning("%s Falling back to process memory.", exc)
        return None
    if cache.ping(force=True):
        LOGGER.info(
            "Redis cache connected key_prefix=%s",
            cache.key_prefix,
        )
    else:
        LOGGER.warning(
            "Redis cache is unavailable; using process memory "
            "and retrying in the background."
        )
    return cache
