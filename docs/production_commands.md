# Production Deployment Commands

This runbook covers the complete workflow from a local Git push to a production Docker update. Run commands from the repository root unless stated otherwise.

The Docker services use `restart: unless-stopped`. When started with `docker compose up -d`, they continue after the SSH session or terminal closes and restart after a server reboot, provided the Docker service is enabled.

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

If Ollama runs as a host service:

```bash
sudo systemctl enable --now ollama
sudo systemctl is-enabled ollama
sudo systemctl is-active ollama
```

This is normally a one-time server setup.

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
RERANK_PROVIDER_ORDER=jina,voyage-2.5,voyage-2.5-lite
RERANK_API_TIMEOUT_SECONDS=3
RERANK_MAX_DOCUMENT_CHARS=300

RERANK_CANDIDATE_K=20
PRIMARY_RANKED_K=20
HYBRID_CANDIDATE_K=40

API_AUTH_ENABLED=true
REDIS_ENABLED=true
API_TENANT_ENGINE_CACHE_SIZE=1
API_TENANT_MAX_CONCURRENT_SEARCHES=1
```

Keep hosted-provider credentials in `.env.keys` or the production secret manager:

```dotenv
JINA_API_KEY=<production-jina-key>
VOYAGE_API_KEY=<production-voyage-key>
```

Jina is the primary provider. The two Voyage entries use the same key with separate models. If only one provider is licensed, remove unavailable entries from `RERANK_PROVIDER_ORDER`; at least one matching key is required.

For a remote company database, set the TLS mode variable referenced by the tenant YAML to `verify-full` and configure its CA certificate path in `.env.keys` or the production secret manager.

Do not copy `.env.example` over the production `.env`, and do not copy `.env.keys.example` over production secrets.

Review only the relevant non-secret values:

```bash
rg '^(RERANK_|PRIMARY_RANKED_K|HYBRID_CANDIDATE_K|API_AUTH_ENABLED|REDIS_ENABLED|API_TENANT_)' .env
```

## 7. Validate and rebuild the Docker image

```bash
docker compose config --quiet
docker compose pull pgvector redis
docker compose build --pull api
docker compose up -d pgvector redis
docker compose ps
```

The API image must be rebuilt because application code and Python requirements changed. Existing pgvector, Redis, BM25, and usage data are retained.

## 8. Prepare the embedding model

If Ollama runs on the host:

```bash
ollama pull embeddinggemma:latest
ollama list
```

If Ollama runs in Docker, set `OLLAMA_BASE_URL=http://ollama:11434` in `.env`, then run:

```bash
docker compose --profile ollama up -d ollama
docker compose exec -T ollama ollama pull embeddinggemma:latest
```

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

## 10. Update the indexes for this release

The reranker change does not change stored embeddings. If production already contains the complete pgvector index for the same `embeddinggemma:latest` embedding model, do not replace or force re-embed vectors.

Rebuild BM25 once for this upgrade so the current lexical fields and price/listing logic are populated from the source database:

```bash
docker compose run --rm api python src/ingest.py \
  --company "$COMPANY_ID" \
  --bm25-only \
  --mysql-batch-size 5000
```

Verify vector and BM25 counts:

```bash
docker compose run --rm api python src/ingest.py \
  --company "$COMPANY_ID" \
  --list
```

If pgvector is missing rows or source data changed, run an incremental full scan with deletion reconciliation:

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
docker compose up -d --remove-orphans api
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
  "$(docker compose ps -q redis)"
```

Each should report `restart=unless-stopped`.

After the new API is healthy, an installation upgraded from the former local-reranker profile may remove its now-unused Hugging Face volume once:

```bash
docker volume ls --filter label=com.docker.compose.volume=hf-cache
docker volume rm <volume-name-shown-above>
```

Do not remove pgvector, Redis, Ollama, or application storage volumes.

## 12. Production verification

Readiness:

```bash
curl -fsS http://127.0.0.1:8000/api/v1/ready
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
git pull --ff-only origin "$BRANCH"
docker compose config --quiet
docker compose build --pull api
docker compose up -d --no-deps --force-recreate api
docker compose ps
docker compose logs --tail=100 api
curl -fsS http://127.0.0.1:8000/api/v1/ready
```

Do not run ingestion automatically for every code-only release. Run it only when source data, embedding content, the embedding model, BM25 data, or index schema changed.

## 15. Rollback

The previous Git commit is stored in `$BACKUP_DIR/git-commit.txt`. To roll back code during the same maintenance session:

```bash
export PREVIOUS_COMMIT="$(cat "$BACKUP_DIR/git-commit.txt")"
git switch --detach "$PREVIOUS_COMMIT"
docker compose build api
docker compose up -d --no-deps --force-recreate api
docker compose logs --tail=200 api
```

Restore pgvector or `storage/` only if the failed release changed index data or schema. A code-only rollback should not restore data automatically.

After the main branch is fixed and pushed:

```bash
git switch main
git pull --ff-only origin main
```

## 16. Troubleshooting

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
