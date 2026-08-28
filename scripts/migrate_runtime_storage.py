#!/usr/bin/env python3
"""Move API-owned SQLite state out of the shared storage root."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATABASE_NAMES = ("usage.sqlite3", "search_analytics_spool.sqlite3")
SIDECAR_SUFFIXES = ("", "-wal", "-shm")


def migration_plan(storage_root: Path) -> list[tuple[Path, Path]]:
    runtime_root = storage_root / "search-runtime"
    moves = []
    for database_name in DATABASE_NAMES:
        for suffix in SIDECAR_SUFFIXES:
            source = storage_root / f"{database_name}{suffix}"
            target = runtime_root / f"{database_name}{suffix}"
            if source.exists() and target.exists():
                raise RuntimeError(
                    f"Both legacy and isolated runtime files exist: {source}, {target}"
                )
            if source.exists():
                moves.append((source, target))
    return moves


def migrate_runtime_storage(
    storage_root: Path,
    *,
    check: bool,
) -> list[tuple[Path, Path]]:
    storage_root = storage_root.resolve()
    plan = migration_plan(storage_root)
    if check:
        if plan:
            raise RuntimeError("Legacy API runtime files still require migration")
        return []
    runtime_root = storage_root / "search-runtime"
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    ownership_source = storage_root / "companies"
    if os.geteuid() == 0 and ownership_source.exists():
        owner = ownership_source.stat()
        os.chown(runtime_root, owner.st_uid, owner.st_gid)
    os.chmod(runtime_root, 0o750)
    for source, target in plan:
        source.replace(target)
    return plan


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    if args.check and args.preflight:
        parser.error("--check and --preflight are mutually exclusive")
    if args.preflight:
        plan = migration_plan(args.root.resolve() / "storage")
        print(f"API runtime storage preflight passed; pending moves={len(plan)}.")
        return
    moves = migrate_runtime_storage(args.root.resolve() / "storage", check=args.check)
    if moves:
        print(f"Moved {len(moves)} API runtime storage files.")
    else:
        print("API runtime storage is already isolated.")


if __name__ == "__main__":
    main()
