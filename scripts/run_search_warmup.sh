#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPANY_ID="${COMPANY_ID:-gainr}"
WARMUP_CANDIDATES="${WARMUP_CANDIDATES:-800}"
LOCK_FILE="${LOCK_FILE:-/tmp/semantic-search-warmup-${COMPANY_ID}.lock}"
INGEST_LOCK_FILE="${INGEST_LOCK_FILE:-/tmp/semantic-search-ingest-${COMPANY_ID}.lock}"

cd "$PROJECT_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Search-path warm-up is already running for ${COMPANY_ID}; skipping."
  exit 0
fi

# Avoid competing with the daily source scan and ingestion run.
exec 8>"$INGEST_LOCK_FILE"
if ! flock -n 8; then
  echo "Ingestion is running for ${COMPANY_ID}; skipping hourly warm-up."
  exit 0
fi

if ! curl -fsS --max-time 10 \
  http://127.0.0.1:8000/api/v1/ready >/dev/null; then
  echo "API is not ready; hourly search-path warm-up cannot run." >&2
  exit 1
fi

echo "Starting search-path warm-up for ${COMPANY_ID} at $(date --iso-8601=seconds)."
docker compose exec -T api python scripts/warm_search_paths.py \
  --company "$COMPANY_ID" \
  --candidates "$WARMUP_CANDIDATES"
