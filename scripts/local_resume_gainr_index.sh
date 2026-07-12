#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

MODE="${1:-resume}"

case "$MODE" in
  resume)
    INGEST_ARGS=(--company gainr --mysql)
    ;;
  replace)
    INGEST_ARGS=(--company gainr --mysql --mysql-replace-source)
    ;;
  *)
    echo "Usage: $0 [resume|replace]"
    echo "  resume  continue from existing hashes and embed only changed/missing rows"
    echo "  replace clear Gainr vector/BM25 source and rebuild from scratch"
    exit 1
    ;;
esac

set -a
# shellcheck disable=SC1091
source .env
# shellcheck disable=SC1091
source .env.keys
set +a

echo "Running Gainr ingestion mode: $MODE"
.venv/bin/python src/ingest.py "${INGEST_ARGS[@]}"
.venv/bin/python src/ingest.py --company gainr --list
