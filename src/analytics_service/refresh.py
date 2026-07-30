from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence

from .config import AnalyticsSettings, load_analytics_registry
from .service import AnalyticsRefreshService
from .source import CsvAnalyticsDataSource, SqlAnalyticsDataSource
from .store import AnalyticsSnapshotStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and atomically publish daily analytics snapshots."
    )
    parser.add_argument(
        "--company",
        action="append",
        dest="companies",
        help="Company ID to refresh; repeat for multiple companies.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Refresh every analytics-enabled company.",
    )
    parser.add_argument(
        "--csv-data-dir",
        help="Use standalone analytics CSV files instead of SQL.",
    )
    parser.add_argument(
        "--if-missing",
        action="store_true",
        help="Refresh only companies that do not yet have a snapshot.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.all and not args.companies:
        raise SystemExit("Provide --company at least once or use --all.")
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = AnalyticsSettings.from_env()
    registry = load_analytics_registry(settings.tenant_config_dir)
    company_ids = (
        sorted(registry.companies)
        if args.all
        else list(dict.fromkeys(args.companies))
    )
    missing = [
        company_id
        for company_id in company_ids
        if registry.resolve_company(company_id) is None
    ]
    if missing:
        raise SystemExit(
            "Unknown or analytics-disabled companies: "
            + ", ".join(missing)
        )
    source = (
        CsvAnalyticsDataSource(args.csv_data_dir)
        if args.csv_data_dir
        else SqlAnalyticsDataSource()
    )
    store = AnalyticsSnapshotStore(settings.snapshot_db_path)
    service = AnalyticsRefreshService(source, store)
    if args.if_missing:
        company_ids = [
            company_id
            for company_id in company_ids
            if not store.company_status(company_id)["has_snapshot"]
        ]
    results = [
        service.refresh(registry.resolve_company(company_id))
        for company_id in company_ids
    ]
    print(json.dumps({"results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
