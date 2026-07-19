# Semantic Product Search

A tenant-isolated semantic search service for product and classified catalogues. It combines PostgreSQL/pgvector retrieval, a persistent BM25 index, structured query planning, hosted reranking, canonical database hydration, cursor pagination, and API-visible diagnostics.

## What is implemented

- PostgreSQL/pgvector HNSW retrieval with one table per tenant.
- SQLite-backed BM25 lexical retrieval.
- Hybrid candidate fusion and a failover chain of hosted rerankers.
- Structured category, location, price, duration, and listing-type filters.
- Tenant-scoped API keys, rate limits, caches, indexes, and database configuration.
- Cursor-based search pagination and monthly usage reporting.
- Redis result caching plus graceful vector, BM25, and reranker degradation.
- Docker deployment with persistent pgvector, Redis, Ollama, and application data.
- Gainr compatibility pagination with a ranked 20-result first page and
  filtered continuation pages.
- Real serving-path readiness, bounded overload admission, rotated container
  logs, and a guarded daily incremental-ingestion timer.

The production vector backend is pgvector only. Chroma is not a runtime or ingestion dependency.

## Search flow

```text
request
  -> authentication and rate limit
  -> route
     -> exact category/simple stated filters
        -> deterministic indexed database lookup
     -> descriptive, ambiguous, typo, or multilingual query
        -> tenant-aware query plan and query embedding
        -> pgvector HNSW + BM25 candidate retrieval
        -> reciprocal-rank fusion and intent shaping
        -> hosted reranking
  -> canonical database hydration
  -> pagination, diagnostics, and cache
```

The deterministic path does not call the planner model, embedding model, vector
search, BM25, or a reranker. It is reserved for an exact catalogue category and
simple user-stated constraints. Model-inferred categories and tenant query
aliases remain soft semantic evidence; they never become fuzzy hard filters.

The semantic path uses a shared planner prompt plus tenant-specific context and
aliases, Ollama `embeddinggemma:latest`, pgvector HNSW, persistent BM25,
reciprocal-rank fusion, intent shaping, and the configured hosted-reranker
failover chain. Explicit client filters remain authoritative. If one retrieval
path or a reranker is unavailable, the API uses the remaining safe result path
and reports degraded diagnostics. A request fails only when no serving path
remains.

## Repository layout

```text
configs/tenants/        Tenant database, storage, API, and retrieval profiles
eval/                   Query-planning and retrieval evaluation cases
scripts/                Diagnostics, key generation, and maintenance utilities
src/api.py              FastAPI routes and tenant service pool
src/search_engine.py    Search orchestration, ranking, cache, and pagination state
src/retrieval.py        Vector, BM25, filtering, and fusion logic
src/pgvector_store.py   pgvector collection interface and HNSW management
src/ingest.py           Tenant database ingestion CLI
tests/                  Unit and contract tests
```

## Documentation

- [Architecture](docs/architecture.md)
- [API integration](docs/company_api_integration.md)
- [Local pgvector workflow](docs/local_pgvector_workflow.md)
- [Production operations](docs/production_search_operations.md)
- [Production setup](docs/production_setup.md)
- [Production commands](docs/production_commands.md)
- [Retrieval evaluation gates](eval/README.md)

For every ordinary code change, use the copy-paste workflow at the top of
[Production commands](docs/production_commands.md#routine-code-change-use-this-every-time).
After a successful pull, `scripts/deploy_production.sh` rebuilds/recreates only
the API, waits for real readiness, runs the tenant doctor, and leaves existing
embeddings and indexes untouched.

## Docker quick commands

Run these from the repository root. The production command includes the Ollama
profile; local macOS may use host Ollama as described in the local workflow.

```bash
# Production: start or restore the complete stack.
docker compose --profile ollama up -d

# Restart the existing API image without rebuilding it.
docker compose restart api

# Rebuild/recreate the API after code or tenant-config changes.
docker compose build api
docker compose --profile ollama up -d --no-deps --force-recreate api

# Status, last 500 API log lines, and live logs.
docker compose ps
docker compose logs --tail=500 --no-color api
docker compose logs -f --tail=200 api

# Readiness.
curl -fsS http://127.0.0.1:8000/api/v1/ready | jq
```

Never use `docker compose down -v` for routine work because it deletes named
volumes. See [Production commands](docs/production_commands.md) for deployment,
diagnostics, ingestion, backup, and recovery commands.

## Configuration boundaries

Keep non-secret defaults in `.env` and tenant YAML files. Keep passwords, API keys, and provider credentials in `.env.keys` or a production secret manager. Never commit either populated file.

Each tenant profile must define a unique endpoint slug, BM25 path, and pgvector table. Startup validation rejects shared tenant resources.

Important reranker controls:

| Variable | Purpose |
|---|---|
| `RERANK_PROVIDER_ORDER` | Ordered hosted-provider failover chain |
| `RERANK_CANDIDATE_K` | Number of fused candidates sent to reranking |
| `PRIMARY_RANKED_K` | Ranked window retained for paging |
| `HYBRID_CANDIDATE_K` | Candidate window produced by hybrid retrieval |
| `RERANK_MAX_DOCUMENT_CHARS` | Maximum characters sent per candidate document |

## Development verification

Use the commands in [Local pgvector workflow](docs/local_pgvector_workflow.md). The minimum code gate is:

```bash
.venv/bin/pytest -q
```

Initial server preparation is documented in [Production setup](docs/production_setup.md). Deployment, ingestion, health checks, evaluation, and rollback commands are kept in [Production commands](docs/production_commands.md).

## Operational expectations

- Start with one API worker on an 8 GB host and increase only after load testing.
- Keep Redis enabled in production.
- Keep tenant search concurrency bounded to protect memory and latency.
- Use `DOCKER_OLLAMA_BASE_URL`, `DOCKER_REDIS_URL`, and
  `DOCKER_MYSQL_HOST` for container networking; keep ordinary host values for
  direct Python commands.
- Rebuild embeddings whenever the embedding model or embedding text contract changes.
- Do not run ingestion for API-only, documentation, pagination, caching,
  fallback, or reranker changes.
- Evaluate ranking changes against a versioned, reviewed query set before deployment.
- Place the API behind TLS termination and do not expose pgvector or Redis publicly.
