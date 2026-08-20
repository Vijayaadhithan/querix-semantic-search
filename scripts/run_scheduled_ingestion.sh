#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPANY_ID="${COMPANY_ID:-gainr}"
LOCK_FILE="${LOCK_FILE:-/tmp/semantic-search-ingest-${COMPANY_ID}.lock}"
WARMUP_LOCK_FILE="${WARMUP_LOCK_FILE:-/tmp/semantic-search-warmup-${COMPANY_ID}.lock}"
CONTAINER_NAME="${INGEST_CONTAINER_NAME:-semantic-search-ingest-${COMPANY_ID}}"
MYSQL_BATCH_SIZE="${MYSQL_BATCH_SIZE:-500}"
EMBED_BATCH_SIZE="${EMBED_BATCH_SIZE:-32}"
RETRIEVAL_OVERLAP_FLOOR="${RETRIEVAL_OVERLAP_FLOOR:-0.80}"

if ! [[ "$MYSQL_BATCH_SIZE" =~ ^[1-9][0-9]*$ ]]; then
  echo "MYSQL_BATCH_SIZE must be a positive integer." >&2
  exit 1
fi
if ! [[ "$EMBED_BATCH_SIZE" =~ ^[1-9][0-9]*$ ]]; then
  echo "EMBED_BATCH_SIZE must be a positive integer." >&2
  exit 1
fi
if ! [[ "$RETRIEVAL_OVERLAP_FLOOR" =~ ^(0(\.[0-9]+)?|1(\.0+)?)$ ]]; then
  echo "RETRIEVAL_OVERLAP_FLOOR must be between zero and one." >&2
  exit 1
fi

cd "$PROJECT_DIR"
# Ingestion has priority over the lightweight hourly job. If a warm-up is
# already finishing, wait for it instead of treating the daily ingestion as a
# duplicate and silently skipping the source scan.
exec 8>"$WARMUP_LOCK_FILE"
if ! flock -w 600 8; then
  echo "Timed out waiting for the search-path warm-up lock." >&2
  exit 1
fi
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Scheduled ingestion is already running for ${COMPANY_ID}."
  exit 0
fi

cleanup_container() {
  if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    echo "Stopping scheduled ingestion container ${CONTAINER_NAME}." >&2
    docker stop --time 30 "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  fi
}

if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  echo "Scheduled ingestion container ${CONTAINER_NAME} already exists." >&2
  exit 1
fi
trap cleanup_container EXIT TERM INT

echo "Starting shadow ingestion for ${COMPANY_ID} at $(date --iso-8601=seconds)."
docker compose run --rm --no-deps --name "$CONTAINER_NAME" api \
  python scripts/run_shadow_ingestion.py \
  --company "$COMPANY_ID" \
  --mysql-batch-size "$MYSQL_BATCH_SIZE" \
  --embed-batch-size "$EMBED_BATCH_SIZE" \
  --overlap-floor "$RETRIEVAL_OVERLAP_FLOOR"
curl -fsS http://127.0.0.1:8000/api/v1/ready >/dev/null
echo "Shadow ingestion, validation, pre-promotion warm-up, and hot activation completed."
