#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPANY_ID="${COMPANY_ID:?COMPANY_ID must be set explicitly for the tenant}"
LOCK_FILE="${LOCK_FILE:-/tmp/semantic-search-analytics-${COMPANY_ID}.lock}"
ANALYTICS_BATCH_SIZE="${ANALYTICS_BATCH_SIZE:-500}"
ANALYTICS_RETRY_DELAYS="${ANALYTICS_RETRY_DELAYS:-60 300 900}"
ANALYTICS_RECOVERY_ONLY="${ANALYTICS_RECOVERY_ONLY:-0}"
RECOVERY_DIR="${ANALYTICS_RECOVERY_DIR:-${PROJECT_DIR}/.runtime/analytics-recovery/${COMPANY_ID}}"
RETRY_WRAPPER="${PROJECT_DIR}/scripts/analytics_retry.py"
read -r -a retry_delays <<< "$ANALYTICS_RETRY_DELAYS"

if ! [[ "$ANALYTICS_BATCH_SIZE" =~ ^[1-9][0-9]*$ ]]; then
  echo "ANALYTICS_BATCH_SIZE must be a positive integer." >&2
  exit 1
fi
if [[ ! "$COMPANY_ID" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
  echo "COMPANY_ID must be a tenant slug." >&2
  exit 1
fi
if [[ "$ANALYTICS_RECOVERY_ONLY" != "0" && "$ANALYTICS_RECOVERY_ONLY" != "1" ]]; then
  echo "ANALYTICS_RECOVERY_ONLY must be 0 or 1." >&2
  exit 1
fi
if (( ${#retry_delays[@]} > 3 )); then
  echo "At most three analytics retry delays are supported." >&2
  exit 1
fi
for delay in "${retry_delays[@]}"; do
  if ! [[ "$delay" =~ ^[0-9]+$ ]] || (( 10#$delay > 900 )); then
    echo "Analytics retry delays must be integers between 0 and 900 seconds." >&2
    exit 1
  fi
done

cd "$PROJECT_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Scheduled analytics is already running for ${COMPANY_ID}."
  exit 0
fi
if [[ "$ANALYTICS_RECOVERY_ONLY" == "1" ]] && \
  [[ ! -f "$RECOVERY_DIR/upload.pending" && ! -f "$RECOVERY_DIR/refresh.pending" ]]; then
  exit 0
fi
python3 scripts/render_service_env.py --production --check
mkdir -p "$RECOVERY_DIR"
chmod 700 "$RECOVERY_DIR"

run_with_retries() {
  local stage="$1" attempt=0 status
  shift
  while true; do
    if "$@"; then
      rm -f "$RECOVERY_DIR/${stage}.pending"
      return 0
    else
      status=$?
    fi
    if (( status != 75 )); then
      rm -f "$RECOVERY_DIR/upload.pending" "$RECOVERY_DIR/refresh.pending"
      return "$status"
    fi
    # Persistent state survives reboot; the recovery timer only acts on
    # transient failures. The shared flock prevents overlapping refreshes.
    touch "$RECOVERY_DIR/${stage}.pending"
    if (( attempt >= ${#retry_delays[@]} )); then
      echo "Analytics ${stage} retries exhausted; catch-up pending." >&2
      return 75
    fi
    echo "Transient analytics ${stage} failure; retry $((attempt + 1))/${#retry_delays[@]} in ${retry_delays[$attempt]} seconds."
    sleep "${retry_delays[$attempt]}"
    attempt=$((attempt + 1))
  done
}

if [[ "$ANALYTICS_RECOVERY_ONLY" == "1" ]]; then
  if docker compose run --rm --no-deps \
    -v "${RETRY_WRAPPER}:/app/scripts/analytics_retry.py:ro" analytics-refresh \
    python /app/scripts/analytics_retry.py probe --company "$COMPANY_ID"; then
    echo "Starting analytics catch-up for ${COMPANY_ID}."
  else
    probe_status=$?
    if (( probe_status == 75 )); then
      echo "Analytics source remains unavailable; catch-up will check again."
      exit 0
    fi
    # A permanent error must not cause a five-minute retry loop.
    rm -f "$RECOVERY_DIR/upload.pending" "$RECOVERY_DIR/refresh.pending"
    exit "$probe_status"
  fi
fi

echo "Uploading pending search analytics for ${COMPANY_ID}."
if run_with_retries upload docker compose run --rm --no-deps \
  -v "${RETRY_WRAPPER}:/app/scripts/analytics_retry.py:ro" telemetry-uploader \
  python /app/scripts/analytics_retry.py run-script scripts/flush_search_analytics.py \
  --company "$COMPANY_ID" \
  --batch-size "$ANALYTICS_BATCH_SIZE"; then
  :
else
  upload_exit_code=$?
  echo "Search analytics upload failed; local rows retained for retry." >&2
  exit "$upload_exit_code"
fi

echo "Building the analytics snapshot for ${COMPANY_ID}."
refresh_started_at="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"
if run_with_retries refresh docker compose run --rm --no-deps \
  -v "${RETRY_WRAPPER}:/app/scripts/analytics_retry.py:ro" analytics-refresh \
  python /app/scripts/analytics_retry.py run-module analytics_service.refresh \
  --company "$COMPANY_ID"; then
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
  exit "$refresh_exit_code"
fi

echo "Pruning expired analytics login sessions."
if ! docker compose run --rm --no-deps analytics-api \
  python -m analytics_service.users prune-sessions; then
  echo "Expired analytics session cleanup failed; the snapshot is still current." >&2
fi

if [[ "$ANALYTICS_RECOVERY_ONLY" == "1" ]] && command -v systemctl >/dev/null; then
  # A completed catch-up supersedes the earlier scheduled run's failure.
  # Do not clear its failed state on a no-op or failed recovery check.
  if ! systemctl reset-failed semantic-search-analytics.service; then
    echo "Analytics caught up, but the earlier systemd failure could not be cleared." >&2
  fi
fi

echo "Scheduled analytics refresh completed at $(date --iso-8601=seconds)."
