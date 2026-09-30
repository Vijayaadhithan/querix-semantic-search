#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPANY_ID="${COMPANY_ID:?COMPANY_ID must be set explicitly for the tenant}"
LOCK_FILE="${LOCK_FILE:-/tmp/semantic-search-analytics-${COMPANY_ID}.lock}"
ANALYTICS_BATCH_SIZE="${ANALYTICS_BATCH_SIZE:-500}"

if ! [[ "$ANALYTICS_BATCH_SIZE" =~ ^[1-9][0-9]*$ ]]; then
  echo "ANALYTICS_BATCH_SIZE must be a positive integer." >&2
  exit 1
fi

cd "$PROJECT_DIR"
python3 scripts/render_service_env.py --production --check
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Scheduled analytics is already running for ${COMPANY_ID}."
  exit 0
fi

echo "Uploading pending search analytics for ${COMPANY_ID}."
if ! docker compose run --rm --no-deps telemetry-uploader \
  python scripts/flush_search_analytics.py \
    --company "$COMPANY_ID" \
    --batch-size "$ANALYTICS_BATCH_SIZE"; then
  echo "Search analytics upload failed; local rows retained for retry." >&2
fi

echo "Building the analytics snapshot for ${COMPANY_ID}."
refresh_started_at="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"
if docker compose run --rm --no-deps analytics-refresh \
  python -m analytics_service.refresh --company "$COMPANY_ID"; then
  :
else
  refresh_exit_code=$?
  if ! docker compose exec -T analytics-api \
    python -m analytics_service.mark_interrupted \
      --company "$COMPANY_ID" \
      --started-after "$refresh_started_at" \
      --exit-code "$refresh_exit_code"; then
    echo "Could not record the interrupted analytics refresh." >&2
  fi
  echo "Analytics refresh failed; the previous snapshot remains active." >&2
  exit 1
fi

echo "Pruning expired analytics login sessions."
if ! docker compose run --rm --no-deps analytics-api \
  python -m analytics_service.users prune-sessions; then
  echo "Expired analytics session cleanup failed; the snapshot is still current." >&2
fi

echo "Scheduled analytics refresh completed at $(date --iso-8601=seconds)."
