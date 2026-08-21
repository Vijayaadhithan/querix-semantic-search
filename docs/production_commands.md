# Production Deployment Commands

This runbook covers the complete workflow from a local Git push to a production Docker update. Run commands from the repository root unless stated otherwise.

Complete [Production Setup](production_setup.md) first on a new host or when migrating legacy systemd services into Docker. This document assumes the supported Docker networking, storage ownership, secrets, and restart policies are already configured.

The Docker services use `restart: unless-stopped`. When started with `docker compose up -d`, they continue after the SSH session or terminal closes and restart after a server reboot, provided the Docker service is enabled.

## Docker quick reference

Use these commands on the production host from the repository root:

```bash
# Start or restore both APIs, pgvector, Redis, and Docker-managed Ollama.
docker compose --profile ollama up -d

# Restart the current API image only; no rebuild or ingestion.
docker compose restart api

# Rebuild/recreate after code or tenant-YAML changes.
docker compose build api analytics-api
docker compose --profile ollama up -d --no-deps --force-recreate \
  api analytics-api

# Recreate after environment-only changes; a rebuild is unnecessary.
docker compose --profile ollama up -d --no-deps --force-recreate api

# Status and dependency checks.
docker compose ps
docker compose exec -T redis redis-cli ping
docker compose exec -T pgvector pg_isready

# Last 500 API lines and live logs.
docker compose logs --tail=500 --no-color api
docker compose logs -f --tail=200 api

# Serving-path readiness.
curl -fsS http://127.0.0.1:8000/api/v1/ready | jq

# Daily analytics readiness and manual refresh.
curl -fsS http://127.0.0.1:8010/api/v1/ready | jq
docker compose run --rm --no-deps analytics-api \
  python -m analytics_service.refresh --company acme

# Analytics-only users (interactive hidden password prompts).
docker compose run --rm analytics-api \
  python -m analytics_service.users create \
    --username analytics-admin --role internal_admin
docker compose run --rm analytics-api \
  python -m analytics_service.users create \
    --username acme-owner --role company_user --company acme
```

For search-stage timings rather than only container output:

```bash
curl -fsS \
  "http://127.0.0.1:8000/api/v1/$COMPANY_ID/admin/search-events?limit=20" \
  -H "X-Admin-Key: $API_ADMIN_KEY" | jq
```

For recent sanitized API logs without SSH:

```bash
curl -fsS \
  "https://api.example.com/api/v1/admin/logs?limit=100&level=INFO" \
  -H "X-Admin-Key: $API_ADMIN_KEY" | jq
```

For polling, pass the previous response's `next_after_id`:

```bash
curl -fsS \
  "https://api.example.com/api/v1/admin/logs?level=INFO&after_id=$LAST_LOG_ID" \
  -H "X-Admin-Key: $API_ADMIN_KEY" | jq
```

`docker compose restart api` reuses the existing image and environment.
Recreate the container after `.env` or `.env.keys` changes. Rebuild it after
source code, dependencies, or tenant YAML changes. None of these commands runs
ingestion. Never use `docker compose down -v` during routine operations.

## Routine code change: use this every time

For an ordinary code/configuration change, push from development and let the
deployment script rebuild/recreate both independently packaged API images.
Existing pgvector embeddings, BM25, Redis data, analytics snapshots, and the
Docker Ollama model are preserved.

Development machine:

```bash
git status --short
git diff --check
.venv/bin/ruff check src scripts tests
.venv/bin/python scripts/check_markdown.py
.venv/bin/python -m pytest -q
docker compose config --quiet
git add -A
git status --short
git commit -m "Describe the production change"
git push origin main
```

Production host:

```bash
cd <production-repository-path>
export BRANCH=main
git status --short
git pull --ff-only origin "$BRANCH" && \
  COMPANY_ID=acme ./scripts/deploy_production.sh
```

The script automatically validates Compose, rebuilds both API images, ensures
pgvector/Redis/Docker Ollama are running, recreates both APIs, waits for real
readiness, initializes the analytics snapshot only when it is missing, runs the
strict production tenant doctor, and shows status and recent logs. Because the
commands use `&&`, deployment does not start if `git pull` fails. It also refuses
to run over uncommitted production files or concurrently with another deployment.

The equivalent manual readiness check is:

```bash
until curl -fsS --max-time 5 \
  -o /tmp/semantic-search-ready.json \
  http://127.0.0.1:8000/api/v1/ready
do
  echo "Waiting for API..."
  docker compose ps
  sleep 3
done
jq . /tmp/semantic-search-ready.json
```

Do **not** run ingestion for an ordinary code change. Use ingestion only when
source rows/indexed metadata changed, or when the embedding/index contract
changed. Never use `docker compose down -v` during deployment.

The script never edits `.env` or `.env.keys`; Git ignores both. If release notes
require a new production environment value, edit it before running the script.
`docker compose config --quiet` will catch invalid or missing required values.

After first setup or any host-level change, run the read-only host audit. It
checks Compose health, resource headroom, secret-file permissions, restart
policies, readiness, strict production configuration, index visibility, Chroma
residue, public port bindings, the ingestion timer, and legacy virtualenv
references. It does not restart services, ingest data, or delete files.

```bash
COMPANY_ID=acme ./scripts/audit_production_host.sh
```

Install the daily verified backup timer once on the production host. It runs at
approximately 02:00 IST, before the 03:00 ingestion timer. It maintains one
rolling backup at `/root/backups/semantic-search/current`, validates the
custom-format pgvector dump, uses SQLite's online backup API, and writes
checksums before replacing the prior backup. The prior backup remains available
if creating or validating the new backup fails.

```bash
export PRODUCTION_REPO="$(pwd)"
sed "s|/opt/semantic-search|$PRODUCTION_REPO|g" \
  deploy/semantic-search-backup.service | \
  sudo tee /etc/systemd/system/semantic-search-backup.service >/dev/null
sudo cp deploy/semantic-search-backup.timer \
  /etc/systemd/system/semantic-search-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now semantic-search-backup.timer
systemctl list-timers semantic-search-backup.timer
```

Run and verify the first backup immediately:

```bash
sudo systemctl start semantic-search-backup.service
sudo systemctl status semantic-search-backup.service --no-pager
sudo journalctl -u semantic-search-backup.service -n 100 --no-pager
```

## 1. Before pushing from development

Review and verify the complete change set:

```bash
git status --short
git diff --stat
git diff --check
.venv/bin/ruff check src scripts tests
.venv/bin/python scripts/check_markdown.py
.venv/bin/pytest -q
docker compose config --quiet
```

Commit only after reviewing the files shown by `git status`:

```bash
git add -A
git status --short
git commit -m "Harden pgvector production search and deployment"
git push origin main
```

`.env`, `.env.keys`, `storage/`, pgvector data, and Redis data are not pushed to Git.

## 2. Connect to production

```bash
ssh <production-user>@<production-host>
cd <production-repository-path>
export COMPANY_ID=<tenant-slug>
export BRANCH=main
```

Confirm that the production checkout has no unexpected tracked changes:

```bash
git status --short
git branch --show-current
git rev-parse --short HEAD
```

If `git status --short` shows tracked changes, stop and review them. Do not overwrite production edits with reset or checkout commands.

## 3. Enable automatic restart after reboot

On an Ubuntu production host:

```bash
sudo systemctl enable --now docker
sudo systemctl is-enabled docker
sudo systemctl is-active docker
```

Legacy API and Ollama systemd services must remain disabled when Compose owns those services:

```bash
export COMPANY_ID="${COMPANY_ID:-acme}"
export LEGACY_API_SERVICE="${LEGACY_API_SERVICE:-semantic-search-api}"
sudo systemctl disable --now "$LEGACY_API_SERVICE"
sudo systemctl disable --now ollama
```

Commands may report `Unit not found` on a clean server; that requires no action. This is normally a one-time server setup.

## 4. Back up before updating

Stop only the API so BM25 and usage files are stable. PostgreSQL and Redis can remain available for backup:

```bash
docker compose stop api
export BACKUP_DIR="$HOME/backups/semantic-search/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
git rev-parse HEAD > "$BACKUP_DIR/git-commit.txt"
```

Back up pgvector:

```bash
docker compose exec -T pgvector sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$BACKUP_DIR/pgvector.dump"
```

Back up BM25, usage data, and other application state:

```bash
tar -czf "$BACKUP_DIR/storage.tar.gz" storage
ls -lh "$BACKUP_DIR"
```

Do not use `docker compose down -v`; `-v` deletes the persistent Docker volumes.

## 5. Pull the approved Git revision

```bash
git fetch origin
git pull --ff-only origin "$BRANCH"
git rev-parse --short HEAD
git status --short
```

`--ff-only` prevents production from silently creating a merge commit.

## 6. Update production environment values

Git does not replace `.env` or `.env.keys`. Preserve existing database passwords, API keys, and admin keys, then add or update these non-secret values in `.env`:

```dotenv
DOCKER_OLLAMA_BASE_URL=http://ollama:11434
DOCKER_REDIS_URL=redis://redis:6379/0
DOCKER_MYSQL_HOST=<actual-production-database-host>

OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_KEEP_ALIVE=-1

REDIS_URL=redis://redis:6379/0

QUERY_EXTRACT_MODELS=groq:openai/gpt-oss-20b,gemini-3.1-flash-lite,gemma-4-26b-a4b-it
GROQ_TIMEOUT_SECONDS=5

RERANK_PROVIDER_ORDER=voyage-2.5,openrouter-nemotron,voyage-2.5-lite
RERANK_API_TIMEOUT_SECONDS=3
RERANK_MAX_DOCUMENT_CHARS=300

RERANK_CANDIDATE_K=20
PRIMARY_RANKED_K=20
HYBRID_CANDIDATE_K=40

API_AUTH_ENABLED=true
REDIS_ENABLED=true
API_TENANT_ENGINE_CACHE_SIZE=1
API_TENANT_MAX_CONCURRENT_SEARCHES=1
API_SEARCH_SLOT_TIMEOUT_SECONDS=5
OLLAMA_QUERY_TIMEOUT_SECONDS=10
```

Keep hosted-provider credentials in `.env.keys` or the production secret manager:

```dotenv
GROQ_API_KEY=<optional-production-groq-key>
VOYAGE_API_KEY=<production-voyage-key>
OPENROUTER_API_KEY=<production-openrouter-key>
```

The two Voyage entries use the same key with separate models. Free Nemotron is
bounded to 20 requests per minute and 50 requests per day before falling
through to Voyage Lite. If a provider is unavailable, remove its entry from
`RERANK_PROVIDER_ORDER`.

For a remote company database, prefer `verify-full` and configure its CA certificate path in `.env.keys` or the production secret manager. If the provider cannot supply the CA and hostname, use `require` as the encrypted fallback. Do not leave production at `disable`.

Do not copy `.env.example` over the production `.env`, and do not copy `.env.keys.example` over production secrets.

Review only the relevant non-secret values:

```bash
rg '^(RERANK_|PRIMARY_RANKED_K|HYBRID_CANDIDATE_K|API_AUTH_ENABLED|REDIS_ENABLED|API_TENANT_)' .env
```

## 7. Validate and rebuild the Docker image

```bash
docker compose config --quiet
docker compose build --pull api
docker compose --profile ollama up -d pgvector redis ollama
docker compose ps
```

The API image must be rebuilt because application code and Python requirements changed. Existing pgvector, Redis, BM25, and usage data are retained.

## 8. Prepare the embedding model

Start Docker-managed Ollama and prepare the embedding model:

```bash
docker compose --profile ollama up -d ollama
docker compose exec -T ollama ollama list
docker compose run --rm --no-deps api \
  curl -fsS http://ollama:11434/api/tags
```

Run `docker compose exec -T ollama ollama pull embeddinggemma:latest` only on
a new Ollama volume or when `ollama list` reports that the model is missing.

The reranker is hosted, so there are no reranker weights to download or prefetch. Startup validates that the configured provider chain has at least one matching credential.

## 9. Validate the company source

This is read-only and does not generate embeddings:

```bash
docker compose run --rm api python -m cli.ingest \
  --company "$COMPANY_ID" \
  --database \
  --check \
  --limit 10
```

## 10. Decide whether indexes need updating

Do not update indexes for API, pagination, caching, fallback, reranker,
monitoring, documentation, or Docker-only changes. Existing embeddings remain
valid for those releases.

Run incremental ingestion only when source rows, BM25 text, indexed filter
metadata, or embedding text changed.

If the upstream ETL configuration changed which columns build
`bm25_content`, first regenerate every ETL row; an incremental ETL run processes
only source-record changes and cannot rewrite unchanged rows for a new content
contract:

```bash
cd <etl-repository-path>
.venv/bin/rag-ht-pipeline \
  --company "$COMPANY_ID" \
  --run-all \
  --no-csv \
  --publish
```

After the atomic ETL publish, run the backend command below. When
`embedding_content_hash` is unchanged and only `retrieval_metadata_hash`
changed, output should report rows as `unchanged for embeddings;
metadata-updated`. This is the expected zero-model-cost path.

Verify vector and BM25 counts:

```bash
docker compose run --rm api python -m cli.ingest \
  --company "$COMPANY_ID" \
  --list
```

```bash
docker compose run --rm api python -m cli.ingest \
  --company "$COMPANY_ID" \
  --database \
  --mysql-reconcile-deletions \
  --mysql-batch-size 500 \
  --embed-batch-size 32
```

The scheduled runner accepts `MYSQL_BATCH_SIZE` and `EMBED_BATCH_SIZE`
overrides. For a 2-vCPU host, use `MYSQL_BATCH_SIZE=250` and
`EMBED_BATCH_SIZE=8`; larger hosts retain the `500` and `32` defaults.

Incremental ingestion skips completely unchanged rows, reuses vectors for
metadata/BM25-only changes, and embeds only new or embedding-changed rows. Do
not use `--mysql-replace-source` for a routine deployment.

## 11. Start the production API in the background

```bash
docker compose --profile ollama up -d --no-deps --force-recreate api
docker compose ps
docker compose logs --tail=200 api
```

The `-d` flag is essential. It detaches the containers from the terminal. Closing SSH after this command does not stop them.

You may follow logs temporarily:

```bash
docker compose logs -f api
```

Pressing `Ctrl+C` while following logs exits only the log viewer; it does not stop the detached API container.

Confirm the restart policies:

```bash
docker inspect --format '{{.Name}} restart={{.HostConfig.RestartPolicy.Name}}' \
  "$(docker compose ps -q api)" \
  "$(docker compose ps -q pgvector)" \
  "$(docker compose ps -q redis)" \
  "$(docker compose ps -q ollama)"
```

Each should report `restart=unless-stopped`.

Do not remove pgvector, Redis, Ollama, or application storage volumes during a
release. The hosted-reranker image does not require a Hugging Face model volume.

## 12. Production verification

Readiness:

```bash
until curl -fsS --max-time 5 \
  -o /tmp/semantic-search-ready.json \
  http://127.0.0.1:8000/api/v1/ready
do
  echo "Waiting for API..."
  sleep 3
done
jq . /tmp/semantic-search-ready.json
```

Strict infrastructure and security verification:

```bash
docker compose exec -T api python scripts/doctor.py \
  --company "$COMPANY_ID" \
  --strict \
  --production
```

Read the company API key without putting it in shell history:

```bash
read -rs COMPANY_API_KEY
export COMPANY_API_KEY
```

Authenticated company health:

```bash
curl -fsS "http://127.0.0.1:8000/api/v1/${COMPANY_ID}/health" \
  -H "X-API-Key: $COMPANY_API_KEY"
```

Smoke search:

```bash
curl -fsS -X POST \
  "http://127.0.0.1:8000/api/v1/${COMPANY_ID}/search" \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $COMPANY_API_KEY" \
  -d '{"query":"example product query","page_size":10}'
```

For a tenant configured with a legacy compatibility adapter, use
`/filter-result` instead. Its mobile, web, and other clients must send the
selected location as `filter.city_id`; `/search` is intentionally disabled:

```bash
read -r CITY_ID
curl -fsS -X POST \
  "http://127.0.0.1:8000/api/v1/${COMPANY_ID}/filter-result" \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $COMPANY_API_KEY" \
  -d "{\"searchTerm\":\"example product query\",\"filter\":{\"city_id\":${CITY_ID}},\"page\":1}"
unset CITY_ID
```

Clear the temporary shell variable afterward:

```bash
unset COMPANY_API_KEY
```

The Compose file binds the API to `127.0.0.1`. A host-level Nginx or Caddy reverse proxy can reach it. A reverse proxy running in Docker should join the Compose network and address the `api` service directly.

## 13. Verify operation after closing SSH

Close the SSH session, reconnect, and run:

```bash
cd <production-repository-path>
docker compose ps
curl -fsS http://127.0.0.1:8000/api/v1/ready
```

To verify reboot recovery during a maintenance window:

```bash
sudo reboot
```

After reconnecting:

```bash
cd <production-repository-path>
systemctl is-active docker
docker compose ps
curl -fsS http://127.0.0.1:8000/api/v1/ready
```

Do not test reboot recovery during customer traffic without an approved maintenance window.

## 14. Future routine deployments

For later code-only releases that do not change dependencies, embedding text, embedding model, BM25 schema, or tenant storage configuration:

```bash
cd <production-repository-path>
export BRANCH=main
git status --short
git pull --ff-only origin "$BRANCH" && \
  COMPANY_ID=acme ./scripts/deploy_production.sh
```

Do not run ingestion automatically for every code-only release. Run it only when source data, embedding content, the embedding model, BM25 data, or index schema changed.

## 15. Daily 03:00 IST zero-downtime ingestion

The scheduled job scans the configured source table while the current tenant
generation remains live. It updates the inactive pgvector/BM25 slot, embeds
only changed content, reconciles deleted rows, validates counts and retrieval
overlap, prewarms the candidate, and hot-activates it. It does not restart or
stop the API. A host lock prevents overlapping runs. The generation token is
part of result-cache identity, preventing results from an older generation
from leaking across promotion.

The first run creates the second physical generation from the existing active
indexes. Later runs alternate the two bounded slots. A failed candidate is not
promoted; searches continue against the last validated active generation.

The scheduled units read the tenant explicitly from a host-only environment
file. Create it once before enabling the timers:

```bash
sudo install -d -m 0750 /etc/semantic-search
printf 'COMPANY_ID=%s\n' "$COMPANY_ID" | \
  sudo tee /etc/semantic-search/tenant.env >/dev/null
sudo chmod 0640 /etc/semantic-search/tenant.env
```

Install the systemd units using the current production checkout path:

```bash
export PRODUCTION_REPO="$(pwd)"
sed "s|/opt/semantic-search|$PRODUCTION_REPO|g" \
  deploy/semantic-search-ingest.service | \
  sudo tee /etc/systemd/system/semantic-search-ingest.service >/dev/null
sudo cp deploy/semantic-search-ingest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now semantic-search-ingest.timer
systemctl list-timers semantic-search-ingest.timer
```

Inspect the timer without forcing an immediate ingestion run:

```bash
systemctl status semantic-search-ingest.timer --no-pager
journalctl -u semantic-search-ingest.timer -n 50 --no-pager
```

During a run, verify that the API remains available and inspect the generation:

```bash
curl -fsS http://127.0.0.1:8000/api/v1/ready
docker compose exec -T api python scripts/doctor.py --company "$COMPANY_ID"
cat "storage/companies/$COMPANY_ID/index-generations.json"
```

If the newly promoted generation later proves unsuitable, hot-roll back to the
recorded previous slot without restarting the API:

```bash
docker compose exec -e COMPANY_ID="$COMPANY_ID" -T api sh -lc \
  'curl -fsS -X POST -H "X-Admin-Key: $API_ADMIN_KEY" \
  "http://127.0.0.1:8000/api/v1/$COMPANY_ID/admin/rollback-index"'
```

The timer uses `Persistent=true`: if the host is down at 03:00 IST, systemd
runs the missed job after the host starts. The five-minute randomized delay
keeps the start near 03:00 while avoiding an exact boundary spike.

The systemd service allows up to 48 hours for a genuine large re-embedding. The
script uses `docker compose run --no-deps`, a stable named run container, and
removes it when systemd stops or times out. It cannot recreate serving
dependencies or leave an orphan that overlaps tomorrow's run. Candidate
ingestion state is stored beside the candidate BM25 file and does not make the
active service unready.

### Two-hour analytics refresh

Analytics runs independently at every even-hour `:30` in `Asia/Kolkata`. It
uploads the stable analytics spool, atomically publishes a new snapshot, and
prunes expired analytics sessions. It does not run ingestion or restart either
API.

```bash
export PRODUCTION_REPO="$(pwd)"
sed "s|/opt/semantic-search|$PRODUCTION_REPO|g" \
  deploy/semantic-search-analytics.service | \
  sudo tee /etc/systemd/system/semantic-search-analytics.service >/dev/null
sudo cp deploy/semantic-search-analytics.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now semantic-search-analytics.timer
systemctl list-timers semantic-search-analytics.timer
```

The timer uses `Persistent=true`, a five-minute randomized delay, and a host
lock to prevent overlapping analytics refreshes. Failed spool uploads remain
local for retry; failed snapshot builds keep the previous snapshot active.

### Periodic local search-path warm-up

The API warms Ollama and pgvector during startup. On a low-traffic server,
kernel and database cache paths can still cool after a long idle period. Install
the 30-minute timer to run three lightweight, read-only representative queries
through Ollama, pgvector HNSW, and BM25:

```bash
export PRODUCTION_REPO="$(pwd)"
sed "s|/opt/semantic-search|$PRODUCTION_REPO|g" \
  deploy/semantic-search-warmup.service | \
  sudo tee /etc/systemd/system/semantic-search-warmup.service >/dev/null
sudo cp deploy/semantic-search-warmup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now semantic-search-warmup.timer
sudo systemctl start semantic-search-warmup.service
systemctl list-timers semantic-search-warmup.timer
journalctl -u semantic-search-warmup.service -n 50 --no-pager
```

The timer runs approximately once every 30 minutes, with up to two minutes of
jitter. It starts ten minutes after a reboot because API startup already warms
the same paths. It skips a run when daily ingestion is active, does not call
the hosted planner or reranker, and opens BM25 read-only. If a warm-up is just
finishing when daily ingestion starts, ingestion waits for the short warm-up
instead of skipping the daily source scan.

## 16. Rollback

The previous Git commit is stored in `$BACKUP_DIR/git-commit.txt`. To roll back code during the same maintenance session:

```bash
export PREVIOUS_COMMIT="$(cat "$BACKUP_DIR/git-commit.txt")"
git switch --detach "$PREVIOUS_COMMIT"
docker compose build api
docker compose --profile ollama up -d --no-deps --force-recreate api
docker compose logs --tail=200 api
```

Restore pgvector or `storage/` only if the failed release changed index data or schema. A code-only rollback should not restore data automatically.

After the main branch is fixed and pushed:

```bash
git switch main
git pull --ff-only origin main
```

## 17. Troubleshooting

```bash
docker compose ps
docker compose logs --tail=300 api
docker compose logs --tail=100 pgvector
docker compose exec -T redis redis-cli ping
docker compose exec -T pgvector pg_isready
docker stats --no-stream
```

Common rules:

- `docker compose up` without `-d` stays attached to the terminal; use `up -d` in production.
- `docker compose stop` deliberately stops containers; restart them with `docker compose up -d`.
- `docker compose down` removes containers and networks but normally preserves named volumes.
- `docker compose down -v` deletes named volumes and must not be used during routine deployment.
- If no hosted reranker loads, verify the provider order and matching key names without printing secret values.
- If reranking is slow or costly, inspect API stage timings and usage before reducing the 20-candidate or 300-character limits; re-run relevance evaluation after every ranking change.
