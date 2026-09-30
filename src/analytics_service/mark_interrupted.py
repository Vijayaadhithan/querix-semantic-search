"""Record a refresh worker's nonzero exit after its process has stopped."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from .config import AnalyticsSettings
from .store import AnalyticsSnapshotStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", required=True)
    parser.add_argument("--started-after", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    args = parser.parse_args()
    try:
        started_at = datetime.fromisoformat(args.started_after.replace("Z", "+00:00"))
    except ValueError as exc:
        parser.error(f"Invalid --started-after timestamp: {exc}")
    if started_at.tzinfo is None:
        parser.error("--started-after must include a timezone")
    if args.exit_code == 0:
        parser.error("--exit-code must be nonzero")

    settings = AnalyticsSettings.from_env()
    store = AnalyticsSnapshotStore(settings.snapshot_db_path)
    count = store.interrupt_refreshes_started_since(
        args.company,
        started_at.astimezone(UTC),
        error_type=f"refresh_exit_{args.exit_code}",
    )
    print(
        f"Marked interrupted analytics refreshes company={args.company} count={count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
