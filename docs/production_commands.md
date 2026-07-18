# Production Deployment Commands

This runbook covers the complete workflow from a local Git push to a production Docker update. Run commands from the repository root unless stated otherwise.

Complete [Production Setup](production_setup.md) first on a new host or when migrating legacy systemd services into Docker. This document assumes the supported Docker networking, storage ownership, secrets, and restart policies are already configured.

The Docker services use `restart: unless-stopped`. When started with `docker compose up -d`, they continue after the SSH session or terminal closes and restart after a server reboot, provided the Docker service is enabled.

## Routine code change: use this every time

For an ordinary code/configuration change, push from development and recreate
only the production API. Existing pgvector embeddings, BM25, Redis data, and
the Docker Ollama model are preserved.

Development machine:

```bash
git status --short
git diff --check
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
  COMPANY_ID=gainr ./scripts/deploy_production.sh
```

The script automatically validates Compose, rebuilds the API image, ensures
pgvector/Redis/Docker Ollama are running, recreates only the API, waits for real
readiness, runs the strict production tenant doctor, and shows status and recent logs. Because the
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
COMPANY_ID=gainr ./scripts/audit_production_host.sh
```

Install the daily verified backup timer once on the production host. It runs at
approximately 02:00 IST, before the 03:00 ingestion timer, retains seven days,
validates the custom-format pgvector dump, uses SQLite's online backup API, and
writes checksums before publishing a completed backup directory.

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
sudo systemctl disable --now gainr-api
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

QUERY_EXTRACT_MODELS=groq:openai/gpt-oss-20b,gemini-3.1-flash-lite,gemma-4-26b-a4b-it,gemma-4-31b-it
GROQ_TIMEOUT_SECONDS=5

RERANK_PROVIDER_ORDER=langsearch,voyage-2.5,voyage-2.5-lite
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
LANGSEARCH_API_KEY=<production-langsearch-key>
VOYAGE_API_KEY=<production-voyage-key>
```

LangSearch is the primary provider. The two Voyage entries use the same key with separate models. If only one provider is available, remove unavailable entries from `RERANK_PROVIDER_ORDER`; at least one matching key is required.

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
docker compose run --rm api python src/ingest.py \
  --company "$COMPANY_ID" \
  --database \
  --check \
  --limit 10
```

## 10. Decide whether indexes need updating

Do not update indexes for API, pagination, caching, fallback, reranker,
monitoring, documentation, or Docker-only changes. Existing embeddings remain
valid for those releases.

Run incremental ingestion only when source rows, indexed filter metadata, or
embedding text changed:

Verify vector and BM25 counts:

```bash
docker compose run --rm api python src/ingest.py \
  --company "$COMPANY_ID" \
  --list
```

```bash
docker compose run --rm api python src/ingest.py \
  --company "$COMPANY_ID" \
  --database \
  --mysql-reconcile-deletions \
  --mysql-batch-size 500 \
  --embed-batch-size 32
```

Incremental ingestion skips rows whose content hash and embedding model are already current. Do not use `--mysql-replace-source` for a routine deployment.

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
  COMPANY_ID=gainr ./scripts/deploy_production.sh
```

Do not run ingestion automatically for every code-only release. Run it only when source data, embedding content, the embedding model, BM25 data, or index schema changed.

## 15. Daily 03:00 IST incremental ingestion

The scheduled job scans the configured source table, embeds only changed
content, reconciles deleted rows, and restarts the API only after ingestion
succeeds. A host lock prevents overlapping runs. The BM25 revision now changes
only when indexed content actually changes, so an unchanged daily scan does not
invalidate every cached search.

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

The timer uses `Persistent=true`: if the host is down at 03:00 IST, systemd
runs the missed job after the host starts. The five-minute randomized delay
keeps the start near 03:00 while avoiding an exact boundary spike.

The systemd service allows up to 48 hours for a genuine large re-embedding.
The script uses a stable named run container and removes it when systemd stops
or times out, so a failed unit cannot leave an orphan that overlaps tomorrow's
run.

### Migrate a transferred Gainr index to the company namespace

If `--list` shows the validated source
`mysql:rag_ht_test.ads_search_ready` plus a partial source using the physical
production database name, stop and disable the timer before cleanup. Deploy the
stable-index-namespace fix first. The company database source is authoritative.
Migrate the transferred vectors to that source without recalculating embeddings;
where a freshly embedded target row already exists, the target row wins. Then
rebuild BM25 without generating embeddings:

```bash
sudo systemctl disable --now semantic-search-ingest.timer
docker stop --time 60 <running-ingestion-container> 2>/dev/null || true

export BACKUP_DIR="$HOME/backups/semantic-search/namespace-repair-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
docker compose stop api
docker compose exec -T pgvector sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$BACKUP_DIR/pgvector.dump"
tar -czf "$BACKUP_DIR/storage.tar.gz" storage

docker compose run --rm api python src/ingest.py \
  --company gainr \
  --migrate-source 'mysql:rag_ht_test.ads_search_ready' \
  --migration-batch-size 1000 \
  --yes

docker compose run --rm api python src/ingest.py \
  --company gainr \
  --bm25-only \
  --mysql-batch-size 5000
docker compose --profile ollama up -d --no-deps api
```

Verify a 500-row sample before re-enabling the timer. Most unchanged catalogue
rows should be skipped; it must not classify all 500 as changed/new merely
because production uses a different physical database name:

```bash
docker compose run --rm api python src/ingest.py \
  --company gainr \
  --database \
  --limit 500 \
  --mysql-batch-size 500 \
  --embed-batch-size 32

docker compose run --rm api python src/ingest.py --company gainr --list
curl -fsS http://127.0.0.1:8000/api/v1/ready | jq

export PRODUCTION_REPO="$(pwd)"
sed "s|/opt/semantic-search|$PRODUCTION_REPO|g" \
  deploy/semantic-search-ingest.service | \
  sudo tee /etc/systemd/system/semantic-search-ingest.service >/dev/null
sudo cp deploy/semantic-search-ingest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now semantic-search-ingest.timer
```

Do not re-enable the timer if the sample unexpectedly says all 500 rows are
changed/new. Investigate the embedding model, content hash, primary key, and
namespace first.

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
