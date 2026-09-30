"""Run a production-sized local analytics refresh against synthetic telemetry.

The catalogue is read from the configured local database. Search and API
history are generated in bounded chunks. The result is published only to a
temporary SQLite file, never to the normal analytics snapshot or source database.
"""

from __future__ import annotations

import argparse
import logging
import os
import resource
import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from analytics_service.config import AnalyticsSettings, load_analytics_registry
from analytics_service.memory import log_refresh_memory
from analytics_service.service import AnalyticsRefreshService
from analytics_service.source import SOURCE_BATCH_SIZE, SqlAnalyticsDataSource
from analytics_service.store import AnalyticsSnapshotStore


class SyntheticHistorySource(SqlAnalyticsDataSource):
    def __init__(self, rows: int) -> None:
        self.rows = rows

    def _chunks(self, template: pd.Series, *, search: bool):
        generated_at = datetime.now(UTC)
        for start in range(0, self.rows, SOURCE_BATCH_SIZE):
            stop = min(start + SOURCE_BATCH_SIZE, self.rows)
            frame = pd.DataFrame(
                {
                    column: [template[column]] * (stop - start)
                    for column in template.index
                }
            )
            frame["id"] = range(start + 1, stop + 1)
            frame["request_id"] = [
                f"memory-probe-{index:07d}" for index in range(start, stop)
            ]
            frame["created_at"] = [
                generated_at - timedelta(minutes=index % 120_000)
                for index in range(start, stop)
            ]
            if search:
                frame["query_text"] = [
                    f"phone repair in Chennai {index % 100}"
                    for index in range(start, stop)
                ]
            yield frame

    @contextmanager
    def stage(self, company):
        with super().stage(company) as stage:
            log_refresh_memory("benchmark_catalog_staged")
            for name in ("search_history", "api_usage"):
                template = pd.read_sql_query(
                    f'SELECT * FROM "{name}" LIMIT 1', stage.connection
                )
                if template.empty:
                    raise RuntimeError("Local database needs one search and API row")
                stage.replace(
                    name,
                    self._chunks(template.iloc[0], search=name == "search_history"),
                )
            log_refresh_memory("benchmark_history_staged")
            yield stage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", default="gainr")
    parser.add_argument("--rows", type=int, default=153_000)
    args = parser.parse_args()
    if args.rows < 1:
        parser.error("--rows must be positive")
    os.environ["ANALYTICS_REFRESH_MEMORY_TRACE"] = "1"
    logging.basicConfig(level=logging.INFO)
    settings = AnalyticsSettings.from_env()
    company = load_analytics_registry(settings.tenant_config_dir).resolve_company(
        args.company
    )
    if company is None:
        parser.error("Unknown or analytics-disabled company")
    with tempfile.TemporaryDirectory(prefix="analytics-memory-") as directory:
        store = AnalyticsSnapshotStore(Path(directory) / "snapshot.sqlite3")
        result = AnalyticsRefreshService(
            SyntheticHistorySource(args.rows), store
        ).refresh(company)
        status = store.company_status(args.company)
        assert status["latest_run"]["status"] == "complete"
        assert result["query_records"] == args.rows
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_mib = peak_rss / (1024 if sys.platform.startswith("linux") else 1024**2)
    cgroup_peak = Path("/sys/fs/cgroup/memory.peak")
    print(
        f"rows={args.rows} query_records={result['query_records']} "
        f"peak_process_rss_mib={peak_mib:.1f} "
        + (
            f"peak_cgroup_mib={int(cgroup_peak.read_text()) / 1024**2:.1f}"
            if cgroup_peak.exists()
            else ""
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
