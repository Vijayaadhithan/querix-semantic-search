#!/usr/bin/env python3
"""Verify telemetry-table access with the current workload credentials."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.tenant_config import load_tenant_registry
from storage.search_analytics import search_analytics_schema_status


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True)
    args = parser.parse_args()
    profile = load_tenant_registry(require_api_keys=False).get(args.company)
    status = search_analytics_schema_status(
        profile.database,
        search_history_table=profile.analytics.search_history_table,
        api_usage_table=profile.analytics.api_usage_table,
    )
    missing = [table for table, ready in status.items() if not ready]
    if missing:
        raise SystemExit("Telemetry tables are unavailable: " + ", ".join(missing))
    print(f"Telemetry schema access verified for company={profile.company_id}.")


if __name__ == "__main__":
    main()
