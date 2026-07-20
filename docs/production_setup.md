# Production Setup Guide

This guide describes the supported production layout and the complete setup sequence for an Ubuntu host using Docker Compose. It covers first-time setup, migration from legacy systemd services, environment configuration, persistent storage permissions, startup, verification, routine redeployment, and common recovery steps.

Use [Production Deployment Commands](production_commands.md) for the shorter release-by-release workflow after this setup is complete.

## Supported production layout

The production services are:

| Service | Runtime | Exposure |
|---|---|---|
| API | Docker Compose | `127.0.0.1:8000`; publish through a TLS reverse proxy |
| pgvector | Docker Compose | Private Docker network; optional loopback maintenance port |
| Redis | Docker Compose | Private Docker network only |
| Ollama embeddings | Docker Compose profile `ollama` | Private Docker network; optional loopback maintenance port |
| Company database | Remote MySQL or PostgreSQL | TLS-protected connection from the API |
| Query planner | Hosted provider | Credentials from `.env.keys` |
| Reranker | Hosted provider chain | Credentials from `.env.keys` |

Docker is managed by systemd. Application services use `restart: unless-stopped`, so they continue after the SSH session closes and restart after a host reboot. Do not run a second API or Ollama process through systemd on the same ports.

## 1. Prepare the host

Confirm the required commands are installed:

```bash
git --version
docker --version
docker compose version
curl --version
```

Enable the Docker daemon at boot:

```bash
sudo systemctl enable --now docker
sudo systemctl is-enabled docker
sudo systemctl is-active docker
```

Both checks should report an enabled and active Docker service.

## 2. Disable legacy host services

Only Docker should own the application ports. Check for older API and Ollama services:

```bash
sudo systemctl status gainr-api --no-pager
sudo systemctl status ollama --no-pager
sudo ss -ltnp | grep -E ':(8000|11434)[[:space:]]' || true
```

When migrating both services into Compose, disable the legacy units:

```bash
sudo systemctl disable --now gainr-api
sudo systemctl disable --now ollama
```

If either unit does not exist, systemd may report `Unit not found`; that requires no action.

Confirm the ports are available before starting Compose:

```bash
sudo ss -ltnp | grep -E ':(8000|11434)[[:space:]]' || echo "Application ports are free"
```

Do not enable the legacy services again. For Docker application logs, use `docker compose logs`, not `journalctl -u gainr-api`.

## 3. Obtain the repository

For a new checkout:

```bash
git clone <repository-url> <production-repository-path>
cd <production-repository-path>
git switch main
```

For an existing checkout:

```bash
cd <production-repository-path>
git status --short
git pull --ff-only origin main
```

If production contains unexpected tracked changes, stop and review them. Do not discard them with reset or checkout commands.

## 4. Configure non-secret production values

Create or edit `.env`. Do not overwrite a working production file with `.env.example`, because Git does not manage environment-specific database and network values.

The core Docker production values are:

```dotenv
DOCKER_OLLAMA_BASE_URL=http://ollama:11434
DOCKER_REDIS_URL=redis://redis:6379/0
DOCKER_MYSQL_HOST=<actual-production-database-host>

OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_KEEP_ALIVE=-1

REDIS_ENABLED=true
REDIS_URL=redis://redis:6379/0
REDIS_KEY_PREFIX=semantic_ads
REDIS_RESULT_CACHE_ENABLED=true
REDIS_RESULT_CACHE_TTL_SECONDS=300

RERANK_PROVIDER_ORDER=voyage-2.5,openrouter-nemotron,voyage-2.5-lite
RERANK_API_TIMEOUT_SECONDS=3
RERANK_MAX_DOCUMENT_CHARS=300
RERANK_CANDIDATE_K=20
PRIMARY_RANKED_K=20
HYBRID_CANDIDATE_K=40

VOYAGE_RERANK_URL=https://api.voyageai.com/v1/rerank
VOYAGE_RERANK_MODEL=rerank-2.5
VOYAGE_RERANK_LITE_MODEL=rerank-2.5-lite
VOYAGE_RERANK_RPM_PER_MODEL=3
OPENROUTER_RERANK_URL=https://openrouter.ai/api/v1/rerank
OPENROUTER_RERANK_MODEL=nvidia/llama-nemotron-rerank-vl-1b-v2:free
OPENROUTER_RERANK_RPM=20
OPENROUTER_RERANK_RPD=50

API_HOST=127.0.0.1
API_PORT=8000
API_LOG_LEVEL=info
API_AUTH_ENABLED=true
API_RATE_LIMIT_ENABLED=true
API_TENANT_CONFIG_DIR=configs/tenants
API_TENANT_ENGINE_CACHE_SIZE=1
API_TENANT_MAX_CONCURRENT_SEARCHES=1
API_SEARCH_SLOT_TIMEOUT_SECONDS=5
OLLAMA_QUERY_TIMEOUT_SECONDS=10
API_CORS_ORIGINS=https://<customer-domain>

USAGE_TRACKING_ENABLED=true
USAGE_DB_PATH=storage/usage.sqlite3

PGVECTOR_PORT=15432
PGVECTOR_DATABASE=rag_workbench
```

Add the company database hostname, port, database, source table, and result table required by its tenant profile. Do not place passwords in `.env`.

Keep `database.index_namespace` stable when transferring an existing index
between physical databases. Gainr uses the authoritative company database name
as its stable namespace. A transferred local index must be re-keyed with the
namespace-migration command; this preserves embeddings and avoids a full
recalculation. Do not change the namespace without another explicit migration.

Tenant query behavior belongs in `configs/tenants/<company>.yaml`:

```yaml
company:
  id: acme
  planner_adapter: gainr

planner:
  enabled: true
  query_aliases:
    tehcnician: technician
  prompt_context: >-
    Acme is an equipment-rental marketplace. Interpret its domain language
    using Acme's catalogue, filters, and listing meanings.
```

The planner adapter supplies the common prompt and canonical filter schema.
`prompt_context` adds company/domain guidance. `query_aliases` handles that
tenant's spelling, colloquial, or transliterated language as semantic evidence
only; it never creates a fuzzy hard category filter. Alias configuration is
part of the plan-cache fingerprint, and Redis keys are tenant-prefixed.

Use the existing `gainr` adapter only when a new tenant shares its canonical
marketplace meanings. A company with different filters, listing semantics, or
payload behavior needs an appropriate adapter and tenant mapping. This changes
internal interpretation, not the canonical `/search` request/response contract.

For a remote production database, prefer certificate and hostname verification:

```dotenv
<COMPANY>_DB_TLS_MODE=verify-full
<COMPANY>_DB_TLS_CA_FILE=<container-visible-ca-path>
```

Do not enable `verify-full` until the correct certificate is installed and mounted into the API container. If the database provider cannot supply the CA and hostname, use `require` as the encrypted fallback. `disable` may allow connectivity but does not pass the strict production security check.

Important Docker addresses:

- `DOCKER_REDIS_URL` is the value injected into the API container; use the
  Compose hostname `redis`, not `127.0.0.1`.
- `DOCKER_OLLAMA_BASE_URL` is the value injected into the API container; use
  the Compose hostname `ollama` for the supported production layout.
- `DOCKER_MYSQL_HOST` must be the real hostname of the company database as
  reachable from the API container.
- Compose supplies `pgvector:5432` to the API container. `PGVECTOR_PORT=15432` is only the optional loopback host mapping.
- Use the real remote database hostname for a remote company database.

Remove duplicate variables. The last duplicate may silently override the intended value.

## 5. Configure secrets

Create `.env.keys` with placeholders replaced on the production server:

```dotenv
GEMINI_API_KEY=<query-provider-key>
GROQ_API_KEY=<optional-groq-query-provider-key>
VOYAGE_API_KEY=<fallback-reranker-key>

<COMPANY>_API_KEY=<customer-api-key>
API_ADMIN_KEY=<admin-api-key>

MYSQL_USER=<company-database-user>
MYSQL_PASSWORD=<company-database-password>

POSTGRES_USER=<pgvector-container-user>
POSTGRES_PASSWORD=<pgvector-container-password>
PGVECTOR_USER=<pgvector-api-user>
PGVECTOR_PASSWORD=<pgvector-api-password>
```

Groq query models use the `groq:` configuration prefix. After validating the
key and the Gainr planner evaluation, place `groq:openai/gpt-oss-20b` after
`gemini-3.1-flash-lite` and before the two Gemma fallbacks with the
environment-specific `QUERY_EXTRACT_MODELS` setting. Query plans are cached
in Redis plus bounded process memory for one hour, so repeated normalized
queries do not call any hosted planner.

Protect and verify the files:

```bash
chmod 600 .env.keys
git check-ignore -v .env .env.keys
```

Never commit, log, screenshot, or paste populated secret files. Rotate a credential immediately if it is exposed. Changing `POSTGRES_PASSWORD` in the environment does not automatically change the password inside an already initialized PostgreSQL volume; coordinate that rotation inside PostgreSQL.

## 6. Validate configuration and build the API

Validate Compose without printing the resolved secret values:

```bash
docker compose config --quiet
```

Build the current API image:

```bash
docker compose build --pull api
```

The production image intentionally contains no local reranker model, Torch, Transformers, or Hugging Face model cache.

The runtime reranker order is Voyage 2.5, OpenRouter Nemotron free, then Voyage
2.5 Lite. LangSearch and Jina are not used. Provider errors fail open to the
fused pgvector/BM25 order, and degraded responses are not cached.

## 7. Prepare persistent application storage

The host `storage/` directory is mounted into a non-root API container. The image-level ownership does not apply to a host bind mount, so assign it once after building the API image:

```bash
mkdir -p storage
docker compose run --rm --no-deps --user root api \
  sh -c 'chown -R app:app /app/storage && chmod -R u+rwX,go-rwx /app/storage'
```

Verify the API user can write:

```bash
docker compose run --rm --no-deps api \
  sh -c 'touch /app/storage/.write-test && rm /app/storage/.write-test && echo "Storage is writable"'
```

This prevents SQLite errors such as `attempt to write a readonly database` when the usage store enables WAL mode.

Run the ownership command again after restoring `storage/` from a backup created under a different user or host.

## 8. Start pgvector, Redis, and Ollama

Start persistent infrastructure:

```bash
docker compose up -d pgvector redis
docker compose --profile ollama up -d ollama
docker compose ps -a
```

Pull the embedding model into the persistent Ollama volume:

```bash
docker compose exec -T ollama ollama list
```

Only if `embeddinggemma:latest` is missing:

```bash
docker compose exec -T ollama ollama pull embeddinggemma:latest
```

Verify API-network access to Ollama:

```bash
docker compose run --rm --no-deps api \
  curl -fsS http://ollama:11434/api/tags
```

Verify Redis itself:

```bash
docker compose exec -T redis redis-cli ping
```

The expected Redis response is `PONG`.

## 9. Restore or verify search indexes

For an existing indexed deployment, do not rebuild embeddings merely because the API code or hosted reranker changed. Verify the existing artifacts:

```bash
export COMPANY_ID=<tenant-slug>

docker compose run --rm api python src/ingest.py \
  --company "$COMPANY_ID" \
  --list
```

Source, pgvector, and BM25 counts should be consistent with the expected catalogue. Run incremental ingestion only when source data or the embedding contract changed.

For a new host, restore a validated pgvector custom-format dump and the `storage/` archive before starting customer traffic. Verify artifact checksums before restoring. Use incremental ingestion only when a validated transfer is unavailable or the source has legitimately changed.

## 10. Start the API

Start in detached mode:

```bash
docker compose --profile ollama up -d --no-deps --force-recreate api
docker compose ps -a
docker compose logs --tail=200 api
```

The `-d` flag keeps the containers running after the terminal or SSH session closes.

The API may briefly show `health: starting` while it validates providers and warms the embedding model. It must settle to a running, healthy state without a restart loop.

## 11. Verify production

Check readiness:

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

The container health check uses the cheap `/api/v1/live` endpoint every five
minutes. `/api/v1/ready` performs deeper tenant dependency checks and caches
successful results for `API_READINESS_CACHE_SECONDS` (300 seconds by default);
failed results are immediately rechecked on the next request.

Run the infrastructure doctor:

```bash
docker compose exec -T api python scripts/doctor.py \
  --company "$COMPANY_ID"
```

After database TLS, CORS, authentication, and admin credentials are complete, run the strict gate:

```bash
docker compose exec -T api python scripts/doctor.py \
  --company "$COMPANY_ID" \
  --strict \
  --production
```

Perform an authenticated smoke search without placing the key in shell history:

```bash
read -rs COMPANY_API_KEY
echo
export COMPANY_API_KEY

curl -fsS --max-time 120 -X POST \
  "http://127.0.0.1:8000/api/v1/${COMPANY_ID}/search" \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $COMPANY_API_KEY" \
  -d '{"query":"example product query","page_size":10}'

unset COMPANY_API_KEY
```

Finally, test the TLS reverse-proxy endpoint and the actual customer frontend. Loopback readiness alone does not verify DNS, certificates, proxy routing, CORS, or frontend credentials.

## 12. Verify restart behavior

Confirm every service uses the intended restart policy:

```bash
docker inspect --format '{{.Name}} restart={{.HostConfig.RestartPolicy.Name}}' \
  "$(docker compose ps -q api)" \
  "$(docker compose ps -q pgvector)" \
  "$(docker compose ps -q redis)" \
  "$(docker compose ps -q ollama)"
```

Each service should report `restart=unless-stopped`.

Confirm legacy services remain disabled:

```bash
systemctl is-enabled gainr-api
systemctl is-active gainr-api
systemctl is-enabled ollama
systemctl is-active ollama
```

For the Docker-managed layout, the legacy services should be disabled and inactive.

Test an actual host reboot only during an approved maintenance window:

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

## 13. Routine code deployment

Back up before applying an unverified release. Then update and recreate the API:

```bash
cd <production-repository-path>
git status --short
git pull --ff-only origin main && \
  COMPANY_ID=gainr ./scripts/deploy_production.sh
```

The deployment script performs the Compose validation, API build/recreation,
readiness wait, tenant doctor, container status, and log checks automatically.
It stops on the first failed gate and prints API diagnostics after a readiness
failure.

Git does not update `.env` or `.env.keys`, and the script never edits them.
Apply release-specific environment changes manually before running the command.

Do not run ingestion for every code deployment. Run it only when source rows, indexed text, the embedding model, filter metadata, or index schema changed.

### Daily 03:00 IST synchronization

Install the timer once using the current checkout path, following
[Daily 03:00 IST incremental ingestion](production_commands.md#15-daily-0300-ist-incremental-ingestion).
After later code pulls, the unit continues to call the script from that checkout;
reinstall it only if the repository path or unit files change.

Verify the schedule without starting ingestion manually:

```bash
systemctl list-timers semantic-search-ingest.timer
systemctl status semantic-search-ingest.timer --no-pager
journalctl -u semantic-search-ingest.service -n 100 --no-pager
```

## 14. Backups

Stop only the API while capturing SQLite-backed application state:

```bash
docker compose stop api
export BACKUP_DIR="$HOME/backups/semantic-search/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
git rev-parse HEAD > "$BACKUP_DIR/git-commit.txt"
```

Create a PostgreSQL custom-format backup:

```bash
docker compose exec -T pgvector sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$BACKUP_DIR/pgvector.dump"
```

Back up application state:

```bash
tar -czf "$BACKUP_DIR/storage.tar.gz" storage
sha256sum "$BACKUP_DIR/pgvector.dump" "$BACKUP_DIR/storage.tar.gz" \
  > "$BACKUP_DIR/SHA256SUMS"
ls -lh "$BACKUP_DIR"
```

Restart the API after the backup:

```bash
docker compose --profile ollama up -d --no-deps api
```

Store backups outside the application host and periodically test a restore in a separate environment.

## 15. Logs and monitoring

Follow application logs:

```bash
docker compose logs -f --tail=200 api
```

Pressing `Ctrl+C` exits the log viewer only. It does not stop detached containers.

Review recent failures and degraded provider calls:

```bash
docker compose logs --since=15m api \
  | grep -Ei 'error|exception|degraded|fallback|429|timeout' || true
```

Review container resource use:

```bash
docker stats --no-stream
```

Monitor readiness, end-to-end duration, planner time, vector time, reranker time and usage, provider fallbacks, Redis connectivity, database latency, container memory, and HTTP error rates.

## 16. Troubleshooting

### Port 8000 is already in use

Cause: a legacy systemd API or another process still owns the loopback port.

```bash
sudo ss -ltnp | grep ':8000'
sudo systemctl disable --now gainr-api
docker compose --profile ollama up -d --no-deps --force-recreate api
```

### API container ID is empty

Cause: `docker compose ps -q api` returns nothing when the API failed before reaching a running state.

```bash
docker compose ps -a
docker compose logs --tail=200 api
```

Fix the startup error before running `docker inspect` again.

### SQLite reports a read-only database

Cause: the host bind mount is owned by a user that does not match the container `app` user.

```bash
docker compose stop api
docker compose run --rm --no-deps --user root api \
  sh -c 'chown -R app:app /app/storage && chmod -R u+rwX,go-rwx /app/storage'
docker compose --profile ollama up -d --no-deps --force-recreate api
```

### Ollama connection is refused

The API container must use the Docker-specific Compose service address:

```dotenv
DOCKER_OLLAMA_BASE_URL=http://ollama:11434
```

Then:

```bash
docker compose --profile ollama up -d ollama
docker compose exec -T ollama ollama list
docker compose --profile ollama up -d --no-deps --force-recreate api
```

### Redis is healthy but the doctor cannot connect

The API container must use the Compose service name:

```dotenv
DOCKER_REDIS_URL=redis://redis:6379/0
```

Check for duplicate values or a shell override:

```bash
grep -n '^DOCKER_REDIS_URL=' .env
docker compose --profile ollama up -d --no-deps --force-recreate api
```

Verify from the API container:

```bash
docker compose exec -T api python -c \
  'import os, redis; print(os.environ["REDIS_URL"]); print(redis.Redis.from_url(os.environ["REDIS_URL"]).ping())'
```

### Reranker providers fail

Confirm that the provider order has matching credentials in `.env.keys`. Do not print credential values. Hosted reranking fails open to hybrid retrieval, and degraded results are not cached.

### Strict production doctor fails

Address the specific reported control. Common causes are disabled database TLS verification, missing CORS origins, disabled API authentication, a missing admin key, or unavailable Redis.

## 17. Safety rules

- Never run `docker compose down -v` during routine operations; `-v` deletes persistent named volumes.
- Never delete the pgvector, Redis, Ollama, or application storage volumes as a cleanup step.
- Never reset a dirty production Git checkout without reviewing the changes.
- Never commit `.env`, `.env.keys`, certificates containing private keys, database dumps, or production storage.
- Never paste production secrets into tickets, chat, logs, screenshots, or command output.
- Never rebuild all embeddings solely because API or reranker code changed.
- Never expose the API directly to the internet; terminate TLS at a reverse proxy.
- Never expose Redis or pgvector publicly.
