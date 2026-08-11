#!/usr/bin/env python3
"""Bulk-deliver durable local search analytics to a tenant's MySQL DB."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.settings import SEARCH_ANALYTICS_SPOOL_PATH
from core.tenant_config import load_tenant_registry
from storage.mysql import MySQLRuntimeConfig
from storage.search_analytics_spool import (
    deliver_search_analytics_spool,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload pending local search analytics to tenant MySQL.",
    )
    parser.add_argument("--company", default="gainr")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    profile = load_tenant_registry(require_api_keys=False).get(args.company)
    if not profile.analytics.enabled:
        raise SystemExit(f"Search analytics is disabled for company {args.company!r}.")
    if not isinstance(profile.database, MySQLRuntimeConfig):
        raise SystemExit(
            "Search analytics delivery currently supports MySQL tenants only."
        )

    result = deliver_search_analytics_spool(
        SEARCH_ANALYTICS_SPOOL_PATH,
        profile.database,
        company_id=profile.company_id,
        search_history_table=profile.analytics.search_history_table,
        api_usage_table=profile.analytics.api_usage_table,
        batch_size=args.batch_size,
    )
    print(
        "Search analytics delivery complete "
        f"company={profile.company_id} "
        f"uploaded={result['uploaded']} "
        f"deleted={result['deleted']} "
        f"pending={result['pending']} "
        f"spool_bytes={result['spool_bytes']}"
    )


if __name__ == "__main__":
    main()
