
import redis

from storage import redis as redis_cache
from storage.redis import RedisJsonCache


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
