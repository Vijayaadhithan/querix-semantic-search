# Semantic Product Search

A tenant-isolated semantic search service for product and classified catalogues. It combines PostgreSQL/pgvector retrieval, a persistent BM25 index, structured query planning, hosted reranking, canonical database hydration, cursor pagination, and API-visible diagnostics.

## What is implemented

- PostgreSQL/pgvector HNSW retrieval with one table per tenant.
- SQLite-backed BM25 lexical retrieval.
- Hybrid candidate fusion and a failover chain of hosted rerankers.
- Structured category, location, price, duration, and listing-type filters.
- Tenant-scoped API keys, rate limits, caches, indexes, and database configuration.
- Cursor-based search pagination and monthly usage reporting.
- Redis result caching and graceful reranker fallback.
- Docker deployment with persistent pgvector, Redis, and application data.

The production vector backend is pgvector only. Chroma is not a runtime or ingestion dependency.

## Search flow

```text
request
  -> authentication and rate limit
  -> deterministic or model-assisted query plan
  -> pgvector + BM25 candidate retrieval
  -> reciprocal-rank fusion
  -> hosted reranking
  -> canonical database hydration
  -> pagination, diagnostics, and cache
```

Broad catalogue queries remain browseable. Queries containing structured constraints apply those constraints before final ranking. If a reranker is unavailable, the API returns the fused semantic result with degraded diagnostics instead of failing the request.

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
- Rebuild embeddings whenever the embedding model or embedding text contract changes.
- Evaluate ranking changes against a versioned, reviewed query set before deployment.
- Place the API behind TLS termination and do not expose pgvector or Redis publicly.
