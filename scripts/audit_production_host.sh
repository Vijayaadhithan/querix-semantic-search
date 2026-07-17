#!/usr/bin/env bash
set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPANY_ID="${COMPANY_ID:-gainr}"

failures=0
warnings=0

pass() {
  printf '[OK] %s\n' "$1"
}

warn() {
  printf '[WARN] %s\n' "$1"
  warnings=$((warnings + 1))
}

fail() {
  printf '[FAIL] %s\n' "$1"
  failures=$((failures + 1))
}

cd "$PROJECT_DIR"

printf 'Production host audit: company=%s revision=%s\n' \
  "$COMPANY_ID" "$(git rev-parse --short HEAD 2>/dev/null || printf unknown)"

for command_name in docker curl git; do
  if command -v "$command_name" >/dev/null 2>&1; then
    pass "Required command available: ${command_name}"
  else
    fail "Required command missing: ${command_name}"
  fi
done

if ! docker compose version >/dev/null 2>&1; then
  fail "Docker Compose plugin is unavailable"
elif docker compose config --quiet; then
  pass "Docker Compose configuration is valid"
else
  fail "Docker Compose configuration is invalid"
fi

if [[ -n "$(git status --porcelain --untracked-files=normal 2>/dev/null)" ]]; then
  warn "Production checkout has uncommitted or untracked files"
else
  pass "Production checkout is clean"
fi

for secret_file in .env .env.keys; do
  if [[ ! -f "$secret_file" ]]; then
    fail "Missing ${secret_file}"
    continue
  fi
  mode="$(stat -c '%a' "$secret_file" 2>/dev/null || printf unknown)"
  if [[ "$mode" =~ ^[0-7]{3,4}$ ]] && (( (8#$mode & 077) == 0 )); then
    pass "${secret_file} is not group/world accessible (mode ${mode})"
  else
    fail "${secret_file} permissions are ${mode}; use chmod 600"
  fi
done

memory_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null)"
swap_kb="$(awk '/^SwapTotal:/ {print $2}' /proc/meminfo 2>/dev/null)"
if [[ "${memory_kb:-0}" -ge 7340032 ]]; then
  pass "Host memory is at least 7 GiB"
else
  warn "Host memory is below the expected 8 GB class"
fi
if [[ "${swap_kb:-0}" -ge 1048576 ]]; then
  pass "At least 1 GiB swap is configured"
else
  warn "Less than 1 GiB swap is configured; transient OOM protection is limited"
fi

available_kb="$(df -Pk . 2>/dev/null | awk 'NR == 2 {print $4}')"
if [[ "${available_kb:-0}" -ge 10485760 ]]; then
  pass "At least 10 GiB disk space is free"
elif [[ "${available_kb:-0}" -ge 5242880 ]]; then
  warn "Only 5-10 GiB disk space is free"
else
  fail "Less than 5 GiB disk space is free"
fi

for service in api pgvector redis ollama; do
  container_id="$(docker compose --profile ollama ps -q "$service" 2>/dev/null)"
  if [[ -z "$container_id" ]]; then
    fail "Compose service is not running: ${service}"
    continue
  fi
  state="$(docker inspect -f '{{.State.Status}}' "$container_id" 2>/dev/null)"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null)"
  restart_policy="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$container_id" 2>/dev/null)"
  if [[ "$state" == "running" && "$health" != "unhealthy" ]]; then
    pass "${service} is running (health=${health})"
  else
    fail "${service} state=${state:-unknown} health=${health:-unknown}"
  fi
  if [[ "$restart_policy" == "unless-stopped" || "$restart_policy" == "always" ]]; then
    pass "${service} restart policy is ${restart_policy}"
  else
    fail "${service} restart policy is ${restart_policy:-missing}"
  fi
done

if curl -fsS --max-time 10 http://127.0.0.1:8000/api/v1/ready >/dev/null; then
  pass "API readiness endpoint returns success"
else
  fail "API readiness endpoint is not successful"
fi

redis_maxmemory="$(
  docker compose exec -T redis redis-cli --raw CONFIG GET maxmemory 2>/dev/null |
    tail -n 1
)"
redis_policy="$(
  docker compose exec -T redis redis-cli --raw CONFIG GET maxmemory-policy \
    2>/dev/null | tail -n 1
)"
if [[ "${redis_maxmemory:-0}" -ge 134217728 && "$redis_policy" != "noeviction" ]]; then
  pass "Redis has a bounded eviction policy (${redis_policy})"
else
  fail "Redis maxmemory/eviction is unsafe (${redis_maxmemory:-unknown}, ${redis_policy:-unknown})"
fi

if docker compose exec -T api python scripts/doctor.py \
  --company "$COMPANY_ID" --strict --production; then
  pass "Strict production doctor passed"
else
  fail "Strict production doctor failed"
fi

if docker compose exec -T api python src/ingest.py \
  --company "$COMPANY_ID" --list; then
  pass "Tenant vector source listing succeeded"
else
  fail "Tenant vector source listing failed"
fi

if docker compose exec -T api python -c \
  'import importlib.util, sys; sys.exit(importlib.util.find_spec("chromadb") is not None)'; then
  pass "Chroma is absent from the production API image"
else
  fail "Chroma is installed in the production API image"
fi

chroma_paths="$(find . -path './.git' -prune -o -iname '*chroma*' -print 2>/dev/null)"
chroma_docker="$(
  { docker volume ls --format '{{.Name}}'; docker image ls --format '{{.Repository}}:{{.Tag}}'; } \
    2>/dev/null | grep -i chroma || true
)"
if [[ -z "$chroma_paths" && -z "$chroma_docker" ]]; then
  pass "No Chroma-named host paths, images, or volumes were found"
else
  warn "Chroma-named host artifacts exist; verify they are unused before removal"
  [[ -n "$chroma_paths" ]] && printf '%s\n' "$chroma_paths"
  [[ -n "$chroma_docker" ]] && printf '%s\n' "$chroma_docker"
fi

if command -v ss >/dev/null 2>&1; then
  public_ports="$(
    ss -ltnH 2>/dev/null |
      awk '$4 ~ /(^|:)(8000|5432|6379|11434)$/ && $4 !~ /^(127\.0\.0\.1|\[::1\]):/'
  )"
  if [[ -z "$public_ports" ]]; then
    pass "Application and data ports are not publicly bound"
  else
    fail "One or more application/data ports are publicly bound"
    printf '%s\n' "$public_ports"
  fi
else
  warn "ss is unavailable; public port bindings were not checked"
fi

if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-enabled --quiet docker && systemctl is-active --quiet docker; then
    pass "Docker is enabled and active under systemd"
  else
    fail "Docker is not both enabled and active under systemd"
  fi
  if systemctl is-enabled --quiet semantic-search-ingest.timer && \
    systemctl is-active --quiet semantic-search-ingest.timer; then
    pass "Scheduled ingestion timer is enabled and active"
  else
    warn "Scheduled ingestion timer is not both enabled and active"
  fi
  if systemctl is-enabled --quiet semantic-search-backup.timer && \
    systemctl is-active --quiet semantic-search-backup.timer; then
    pass "Scheduled backup timer is enabled and active"
  else
    warn "Scheduled backup timer is not both enabled and active"
  fi
  legacy_refs="$(
    grep -RIl --exclude='*.wants' '/Peronsal_rag/.venv' \
      /etc/systemd/system /etc/cron.d /etc/crontab 2>/dev/null || true
  )"
  if [[ -z "$legacy_refs" ]]; then
    pass "No legacy systemd/cron references to the host virtualenv were found"
  else
    warn "Legacy host virtualenv references still exist"
    printf '%s\n' "$legacy_refs"
  fi
fi

backup_root="${BACKUP_ROOT:-/root/backups/semantic-search}"
latest_backup="$(
  find "$backup_root" -mindepth 1 -maxdepth 1 -type d -name '20??????T??????Z' \
    -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2-
)"
if [[ -n "$latest_backup" && -f "$latest_backup/SHA256SUMS" ]]; then
  backup_age_seconds=$((
    $(date +%s) - $(stat -c '%Y' "$latest_backup/SHA256SUMS")
  ))
  if [[ "$backup_age_seconds" -le 129600 ]]; then
    pass "A checksummed production backup is less than 36 hours old"
  else
    warn "The newest checksummed production backup is older than 36 hours"
  fi
else
  warn "No completed checksummed production backup was found"
fi

printf 'Host audit summary: %d failure(s), %d warning(s).\n' "$failures" "$warnings"
exit "$((failures > 0))"
