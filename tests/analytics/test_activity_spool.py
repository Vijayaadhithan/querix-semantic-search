from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

import pytest

from analytics_service.filters import DashboardFilters
from analytics_service.prepared_views import SnapshotActivityRecords
from analytics_service.store import AnalyticsSnapshotStore
from verticals.marketplace.analytics.dashboard_filters import build_dashboard_overview


def _store(tmp_path):
    store = AnalyticsSnapshotStore(tmp_path / "snapshot.sqlite3")
    pairs = []
    for index in range(64):
        company = {
            "request_id": f"request-{index:03d}",
            "created_at": f"2026-10-08T08:{index % 60:02d}:00+00:00",
            "normalized_query": "camera" if index % 2 else "",
            "request_kind": "text_search" if index % 2 else "filtered_browse",
            "outcome": "zero_result" if index % 3 else "fulfilled",
            "categories": ["Photography"],
            "language": "Tamil" if index % 2 else "English",
            "filters": {
                "city_id": 456 if index % 2 else None,
                "city": "Chennai" if index % 3 else "CHENNAI",
                "subcategory_id": 42,
                "subcategory": "Camera" if index % 3 else "camera",
                "target_ad_type": "offer",
            },
            "search": {"result_count": index, "total_results": index * 2},
            "flags": {"unused_large_payload": "x" * 65_536},
        }
        internal = {
            **company,
            "api": {"status": "success" if index % 3 else "failure"},
            "performance": {
                "total_server_duration_ms": index * 1.25,
                "execution_path": "semantic",
                "cache": {"plan_hit": bool(index % 2), "result_hit": False},
                "stages_ms": {"retrieval_ms": index / 2},
            },
            "attempts": [{"provider": "test", "operation": "reranking"}],
            "diagnostics": {"code": "no_candidates" if index % 2 else None},
        }
        pairs.append((company, internal))
    version = store.publish(
        run_id=store.begin_refresh("gainr"),
        company_id="gainr",
        generated_at="2026-10-08T09:00:00+00:00",
        source_watermark=None,
        source_rows={"search_history": len(pairs)},
        company_dashboard={},
        internal_dashboard={},
        query_records=pairs,
    )
    return store, version


@pytest.mark.parametrize("internal", [False, True])
def test_spool_reads_large_payloads_once_and_preserves_views(
    tmp_path, monkeypatch, internal
):
    store, version = _store(tmp_path)
    full = store.dashboard_activity_records("gainr", internal=internal)
    facets = store.build_query_facets("gainr", version, internal=internal)
    original = store.iter_dashboard_activity_records
    scans = Counter()

    def track(*args, **kwargs):
        scans[kwargs["internal"]] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "iter_dashboard_activity_records", track)
    with SnapshotActivityRecords(store, "gainr", version, internal=internal) as records:
        spool_path = records._path
        assert spool_path.exists()
        assert spool_path.stat().st_size < 100_000
        assert len(records) == len(full) == 64
        assert records.query_facets() == facets
        assert list(records) == list(records) == list(full)
        for period in ("all", "24h", "7d", "30d", "90d"):
            for scope in ("all", "text_search", "browse"):
                kwargs = {
                    "internal": internal,
                    "filters": DashboardFilters(period=period, request_scope=scope),
                    "timezone_name": "Asia/Kolkata",
                    "now": datetime(2026, 10, 8, 9, tzinfo=UTC),
                }
                assert build_dashboard_overview(records, **kwargs) == (
                    build_dashboard_overview(full, **kwargs)
                )
        assert scans == {internal: 1}
    assert not spool_path.parent.exists()


def test_spool_cleanup_when_view_preparation_raises(tmp_path):
    store, version = _store(tmp_path)
    with pytest.raises(RuntimeError, match="view failed"):
        with SnapshotActivityRecords(
            store, "gainr", version, internal=False
        ) as records:
            directory = records._path.parent
            raise RuntimeError("view failed")
    assert not directory.exists()
    assert store.company_status("gainr")["has_snapshot"]


def test_spool_cleanup_when_source_read_raises(tmp_path, monkeypatch):
    import analytics_service.prepared_views as prepared

    store, version = _store(tmp_path)
    temporary_directory = prepared.tempfile.TemporaryDirectory
    directories = []

    def track_directory(**kwargs):
        directory = temporary_directory(dir=tmp_path, **kwargs)
        directories.append(directory.name)
        return directory

    def fail_source(*args, **kwargs):
        raise RuntimeError("source failed")

    monkeypatch.setattr(prepared.tempfile, "TemporaryDirectory", track_directory)
    monkeypatch.setattr(store, "iter_dashboard_activity_records", fail_source)
    with pytest.raises(RuntimeError, match="source failed"):
        SnapshotActivityRecords(store, "gainr", version, internal=False)
    assert directories
    assert not any((tmp_path / name).exists() for name in directories)
