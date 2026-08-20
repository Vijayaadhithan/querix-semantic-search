#!/usr/bin/env python3
"""Run one zero-downtime tenant ingestion and hot promotion."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.tenant_config import load_tenant_registry
from ingestion.generations import run_shadow_ingestion


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True)
    parser.add_argument("--mysql-batch-size", type=int, default=500)
    parser.add_argument("--embed-batch-size", type=int, default=32)
    parser.add_argument("--overlap-floor", type=float, default=0.80)
    args = parser.parse_args()
    if args.mysql_batch_size <= 0 or args.embed_batch_size <= 0:
        parser.error("batch sizes must be positive")
    if not 0 <= args.overlap_floor <= 1:
        parser.error("--overlap-floor must be between zero and one")
    profile = load_tenant_registry(require_api_keys=False).get(args.company)
    result = run_shadow_ingestion(
        profile,
        mysql_batch_size=args.mysql_batch_size,
        embed_batch_size=args.embed_batch_size,
        overlap_floor=args.overlap_floor,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
