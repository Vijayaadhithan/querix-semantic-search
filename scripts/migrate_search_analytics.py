#!/usr/bin/env python3
"""Create or migrate tenant history and internal API-usage tables."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.tenant_config import load_tenant_registry
from storage.mysql import MySQLRuntimeConfig
from storage.search_analytics import (
    create_search_analytics_schema,
    search_analytics_schema_status,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create or migrate durable tenant history and internal API-usage tables."
        ),
    )
    parser.add_argument("--company", required=True)
    args = parser.parse_args()

    profile = load_tenant_registry(require_api_keys=False).get(args.company)
    if not profile.analytics.enabled:
        raise SystemExit(f"Search analytics is disabled for company {args.company!r}.")
    if not isinstance(profile.database, MySQLRuntimeConfig):
        raise SystemExit(
            "Search analytics migration currently supports MySQL tenants only."
        )

    create_search_analytics_schema(
        profile.database,
        company_id=profile.company_id,
        search_history_table=profile.analytics.search_history_table,
        api_usage_table=profile.analytics.api_usage_table,
    )
    status = search_analytics_schema_status(
        profile.database,
        search_history_table=profile.analytics.search_history_table,
        api_usage_table=profile.analytics.api_usage_table,
    )
    missing = [table for table, present in status.items() if not present]
    if missing:
        raise SystemExit(
            "Search analytics migration did not create: " + ", ".join(missing)
        )
    print(
        "Search analytics schema ready "
        f"company={profile.company_id} "
        f"history_table={profile.analytics.search_history_table} "
        f"usage_table={profile.analytics.api_usage_table}"
    )


if __name__ == "__main__":
    main()
