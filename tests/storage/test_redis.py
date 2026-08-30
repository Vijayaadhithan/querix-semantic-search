import pytest
import redis

from storage import redis as redis_cache
from storage.redis import RedisJsonCache


class FakePipeline:
    def __init__(self, client):
        self.client = client
        self.pending = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def watch(self, _key):
        return None

    def get(self, key):
        return self.client.get(key)

    def multi(self):
        return None

    def set(self, key, value, ex):
        self.pending = (key, value, ex)

    def execute(self):
        assert self.pending is not None
        self.client.set(*self.pending)


class FakeRedisClient:
    def __init__(self):
        self.values = {}
        self.closed = False

    def ping(self):
        return True

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex):
        assert ex > 0
        self.values[key] = value

    def close(self):
        self.closed = True

    def pipeline(self):
        return FakePipeline(self)

    def eval(self, script, number_of_keys, *args):
        self.eval_call = (script, number_of_keys, args)
        return [0, 12500]


def test_redis_json_cache_round_trip(monkeypatch):
    client = FakeRedisClient()
    monkeypatch.setattr(
        redis_cache.redis.Redis,
        "from_url",
        lambda *_args, **_kwargs: client,
    )
    cache = RedisJsonCache("redis://localhost:6379/0", "test")

    assert cache.ping(force=True) is True
    assert cache.set_json("plans", "digest", {"value": 1}, 30) is True
    assert cache.get_json("plans", "digest") == {"value": 1}
    assert list(client.values) == ["test:plans:digest"]

    cache.close()
    assert client.closed is True


def test_redis_json_cache_atomically_prepends_unique_items(monkeypatch):
    client = FakeRedisClient()
    monkeypatch.setattr(
        redis_cache.redis.Redis,
        "from_url",
        lambda *_args, **_kwargs: client,
    )
    cache = RedisJsonCache("redis://localhost:6379/0", "test")

    first = cache.prepend_unique_json_item(
        "recent",
        "user",
        {"id": 100, "value": "bike"},
        30,
        10,
    )
    second = cache.prepend_unique_json_item(
        "recent",
        "user",
        {"id": 100, "value": "camera"},
        30,
        10,
    )
    repeated = cache.prepend_unique_json_item(
        "recent",
        "user",
        {"id": 100, "value": "BIKE"},
        30,
        10,
    )

    assert first == {"items": [{"id": 100, "value": "bike"}]}
    assert [item["value"] for item in second["items"]] == ["camera", "bike"]
    assert [item["value"] for item in repeated["items"]] == ["BIKE", "camera"]
    assert len({item["id"] for item in repeated["items"]}) == 2


def test_redis_failure_enters_cooldown_instead_of_raising(monkeypatch):
    class UnavailableRedisClient(FakeRedisClient):
        def ping(self):
            raise redis.exceptions.ConnectionError("unavailable")

    client = UnavailableRedisClient()
    monkeypatch.setattr(
        redis_cache.redis.Redis,
        "from_url",
        lambda *_args, **_kwargs: client,
    )
    cache = RedisJsonCache("redis://localhost:6379/0", "test")

    assert cache.ping(force=True) is False
    assert cache.connected is False
    assert cache.get_json("plans", "digest") is None
    assert cache.set_json("plans", "digest", {"value": 1}, 30) is False


def test_redis_request_window_returns_retry_seconds(monkeypatch):
    client = FakeRedisClient()
    monkeypatch.setattr(
        redis_cache.redis.Redis,
        "from_url",
        lambda *_args, **_kwargs: client,
    )
    cache = RedisJsonCache("redis://localhost:6379/0", "test")

    assert cache.allow_request_window("query:groq", 10, 60) == (
        False,
        12.5,
    )
    _script, number_of_keys, args = client.eval_call
    assert number_of_keys == 2
    assert args == (
        "test:provider_rate_limit:query:groq",
        "test:provider_rate_limit:query:groq:sequence",
        10,
        60000,
    )


def test_redis_token_budget_reserves_weighted_tokens(monkeypatch):
    client = FakeRedisClient()

    def reject_weighted_budget(script, number_of_keys, *args):
        client.eval_call = (script, number_of_keys, args)
        return [0, 1000]

    client.eval = reject_weighted_budget
    monkeypatch.setattr(
        redis_cache.redis.Redis,
        "from_url",
        lambda *_args, **_kwargs: client,
    )
    cache = RedisJsonCache("redis://localhost:6379/0", "test")

    allowed, retry_after = cache.allow_token_budget("query:groq", 8000, 2000)
    assert allowed is False
    assert retry_after == pytest.approx(7.5)
    _script, number_of_keys, args = client.eval_call
    assert number_of_keys == 1
    assert args[0].startswith("test:provider_token_limit:query:groq")
    assert args[3:] == (8000, 120000, 2000)
