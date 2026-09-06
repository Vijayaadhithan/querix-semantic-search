from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

import pytest

from analytics_service.dashboard_cache import DashboardActivityCache


@pytest.mark.parametrize("distinct_keys", [False, True])
def test_concurrent_dashboard_misses_build_one_snapshot_at_a_time(distinct_keys):
    cache = DashboardActivityCache()
    started = Barrier(5)
    building = Event()
    release = Event()
    overlap = Event()
    state_lock = Lock()
    active = peak = calls = 0

    def build():
        nonlocal active, peak, calls
        with state_lock:
            calls += 1
            active += 1
            peak = max(peak, active)
            if active > 1:
                overlap.set()
        building.set()
        assert release.wait(5)
        with state_lock:
            active -= 1
        return {"requests": 55_664}

    def request(index):
        started.wait(timeout=5)
        key = (index,) if distinct_keys else ("same-dashboard",)
        return cache.get(key, build)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(request, index) for index in range(4)]
        started.wait(timeout=5)
        try:
            assert building.wait(5)
            assert not overlap.wait(0.1)
        finally:
            release.set()
        assert [future.result(timeout=5) for future in futures] == [
            {"requests": 55_664}
        ] * 4
    assert peak == 1
    assert calls == (4 if distinct_keys else 1)


def test_cached_dashboard_remains_available_during_another_build():
    cache = DashboardActivityCache()
    cached = cache.get(("cached",), lambda: {"requests": 42})
    building = Event()
    release = Event()

    def build():
        building.set()
        assert release.wait(5)
        return {"requests": 43}

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(cache.get, ("new",), build)
        try:
            assert building.wait(5)
            hit = pool.submit(cache.get, ("cached",), lambda: pytest.fail("cache miss"))
            assert hit.result(timeout=2) is cached
        finally:
            release.set()
        assert pending.result(timeout=5) == {"requests": 43}


def test_failed_dashboard_build_can_be_retried():
    cache = DashboardActivityCache()

    def fail():
        raise ValueError("invalid dashboard filters")

    with pytest.raises(ValueError, match="invalid dashboard filters"):
        cache.get(("retry",), fail)
    assert cache.get(("retry",), lambda: {"requests": 42}) == {"requests": 42}


def test_dashboard_cache_evicts_least_recently_used_summary():
    cache = DashboardActivityCache(maxsize=2)
    first = cache.get(("first",), dict)
    cache.get(("second",), dict)
    assert cache.get(("first",), dict) is first
    cache.get(("third",), dict)
    rebuilt = {"new": True}
    assert cache.get(("second",), lambda: rebuilt) is rebuilt


def test_dashboard_cache_requires_positive_capacity():
    with pytest.raises(ValueError, match="positive"):
        DashboardActivityCache(maxsize=0)
