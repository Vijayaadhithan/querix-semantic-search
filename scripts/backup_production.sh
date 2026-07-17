#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BACKUP_ROOT="${BACKUP_ROOT:-/root/backups/semantic-search}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
LOCK_FILE="${LOCK_FILE:-/tmp/semantic-search-production-backup.lock}"

cd "$PROJECT_DIR"

for command_name in docker flock python3 sha256sum tar; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required backup command is missing: ${command_name}" >&2
    exit 1
  fi
done

if [[ ! "$BACKUP_RETENTION_DAYS" =~ ^[1-9][0-9]*$ ]]; then
  echo "BACKUP_RETENTION_DAYS must be a positive integer." >&2
  exit 1
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Another production backup is already running."
  exit 0
fi

mkdir -p "$BACKUP_ROOT"
chmod 700 "$BACKUP_ROOT"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
final_dir="$BACKUP_ROOT/$timestamp"
work_dir="$(mktemp -d "$BACKUP_ROOT/.incomplete-${timestamp}-XXXXXX")"

cleanup() {
  rm -rf -- "$work_dir"
}
trap cleanup EXIT

git rev-parse HEAD > "$work_dir/git-commit.txt"
docker compose config --quiet

docker compose exec -T pgvector sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$work_dir/pgvector.dump"
docker compose exec -T pgvector pg_restore --list \
  < "$work_dir/pgvector.dump" >/dev/null

sqlite_root="$work_dir/sqlite"
mkdir -p "$sqlite_root"
while IFS= read -r -d '' source_path; do
  relative_path="${source_path#./}"
  target_path="$sqlite_root/$relative_path"
  mkdir -p "$(dirname "$target_path")"
  python3 - "$source_path" "$target_path" <<'PY'
import sqlite3
import sys

source_path, target_path = sys.argv[1:]
with sqlite3.connect(f"file:{source_path}?mode=ro", uri=True) as source:
    with sqlite3.connect(target_path) as target:
        source.backup(target)
        result = target.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise SystemExit(f"SQLite quick_check failed for {source_path}")
PY
done < <(find ./storage -type f \( -name '*.sqlite' -o -name '*.sqlite3' \) -print0)

tar -C "$sqlite_root" -czf "$work_dir/storage-sqlite.tar.gz" storage
(
  cd "$work_dir"
  sha256sum pgvector.dump storage-sqlite.tar.gz > SHA256SUMS
)

chmod -R go-rwx "$work_dir"
mv "$work_dir" "$final_dir"
trap - EXIT

find "$BACKUP_ROOT" \
  -mindepth 1 \
  -maxdepth 1 \
  -type d \
  -name '20??????T??????Z' \
  -mtime "+$BACKUP_RETENTION_DAYS" \
  -exec rm -rf -- {} +

echo "Production backup complete: ${final_dir}"
