# Semantic Product Search

A tenant-isolated semantic search service for product and classified catalogues. It combines PostgreSQL/pgvector retrieval, a persistent BM25 index, structured query planning, hosted reranking, canonical database hydration, cursor pagination, and API-visible diagnostics.

## What is implemented

- PostgreSQL/pgvector HNSW retrieval with one table per tenant.
- SQLite-backed BM25 lexical retrieval.
- Hybrid candidate fusion and a credential-aware failover chain of hosted
  Voyage and OpenRouter rerankers.
- Structured category, location, price, duration, and listing-type filters.
- Tenant-scoped API keys, rate limits, caches, indexes, and database configuration.
- Cursor-based search pagination and monthly usage reporting.
- Redis result caching plus graceful vector, BM25, and reranker degradation.
- Docker deployment with persistent pgvector, Redis, Ollama, and application data.
- Gainr compatibility pagination with a ranked 20-result first page and
  filtered continuation pages.
- Real serving-path readiness, bounded overload admission, rotated container
  logs, and a guarded daily incremental-ingestion timer.

The runtime and ingestion vector backend is PostgreSQL/pgvector only. Tenant
configuration still carries a `storage.vector_backend` discriminator, but
startup accepts only `pgvector`; it is not a pluggable-backend switch. Chroma is
not installed, opened, migrated, or used as a fallback.

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
        -> configured hosted reranker chain
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
failover chain. Supported chain entries are Voyage 2.5, Voyage 2.5 Lite, and
OpenRouter Nemotron; entries without matching credentials are skipped. There is
no local reranker model or local reranker fallback. Explicit client filters
remain authoritative. If vector or BM25 retrieval is unavailable, the other
retrieval path can continue. If every hosted reranker fails, the service keeps
the fused hybrid order, marks the response degraded, and does not cache it.

## Repository layout

```text
configs/tenants/        Tenant database, storage, API, and retrieval profiles
eval/                   Query-planning and retrieval evaluation cases
scripts/                Diagnostics, key generation, and maintenance utilities
src/api.py              FastAPI application lifecycle and routes
src/api_contracts.py    API schemas, cursor/session state, and process health
src/api_service.py      Search request service, paging, usage, and monitoring
src/api_tenants.py      Tenant engine/service lifecycle and prewarming
src/search_engine.py    Retrieval, ranking, and end-to-end search orchestration
src/search_engine_support.py
                        Planning, hydration, and result/plan cache support
src/search_ranking.py   Hosted reranking and fusion fallback behavior
src/search_policy.py    Generic tenant search-policy contract and default
src/gainr_search_policy.py
                        Gainr-only planner, fusion, and reranker intent policy
src/gainr_compat.py     Gainr compatibility API orchestration and card mapping
src/gainr_models.py     Gainr compatibility request schemas and field contracts
src/gainr_repository.py Gainr-specific read queries and canonical hydration
src/query_planner.py    High-level query planning and routing
src/query_planner_catalog.py
                        Catalog matching, constraints, and query analysis
src/query_planner_rules.py
                        Planner schemas, vocabularies, and deterministic rules
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

Each tenant profile must define a unique endpoint slug, company search-data
table, BM25 path, and pgvector table. Startup validation rejects shared tenant
resources. Tenant-specific ranking or interpretation belongs behind the
`company.search_policy` hook; `default` performs no domain-specific rewriting.

Important reranker controls:

| Variable | Purpose |
|---|---|
| `RERANK_PROVIDER_ORDER` | Ordered hosted-provider chain; providers without credentials are skipped |
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
