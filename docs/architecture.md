# Search Architecture

## Purpose

The service turns natural-language catalogue queries into tenant-isolated, filter-aware, ranked product results. PostgreSQL remains the source of truth. Retrieval indexes store only the data required to find candidates; returned cards are hydrated from the canonical result table.

## Components

| Component | Responsibility |
|---|---|
| Tenant registry | Validates API keys, endpoint slugs, database connections, pgvector tables, and BM25 paths |
| Query planner | Extracts intent and conservative structured constraints |
| Ollama embedding service | Produces query and ingestion embeddings with the configured model |
| PostgreSQL/pgvector | Stores tenant vectors, retrieval text, and filter metadata; provides HNSW ANN search |
| Persistent BM25 | Provides lexical and exact-term recall |
| Fusion layer | Combines vector and BM25 ranks using reciprocal-rank fusion |
| Hosted reranker chain | Scores the strongest candidates and fails over between configured APIs |
| Canonical database | Supplies current public product fields and visibility state |
| Redis | Stores result and plan caches shared by the API process |
| Usage and event stores | Record tenant-safe request totals and redacted execution diagnostics |

## Request lifecycle

1. The API resolves the tenant from the endpoint and API key.
2. Rate limiting and per-tenant concurrency controls are applied.
3. Deterministic parsing handles clear filters; the query model handles ambiguous intent.
4. Filters are resolved against the tenant catalogue vocabulary.
5. pgvector and BM25 retrieve independent candidate windows.
6. Reciprocal-rank fusion creates a provider-independent fallback order.
7. The reranker scores the bounded candidate set.
8. Ranking policy removes low-confidence or wrong-intent results.
9. IDs are hydrated from the canonical result table.
10. The API returns a cursor, diagnostics, and results; eligible responses enter Redis.

## Ranking and failure behavior

Semantic ranking is the primary result order. BM25 protects exact names, identifiers, and rare words. Structured filters are hard constraints only when confidently extracted or supplied by the client.

Reranking is fail-open. A timeout, rate limit, or provider error retains the hybrid order and marks the response degraded. Degraded responses are not written to the result cache, so a temporary provider failure cannot become sticky.

The ranked window must cover every page that should preserve semantic order. Increasing it improves deep-page consistency but increases provider latency and token usage. Candidate and document-length changes must therefore be evaluated for reciprocal rank, latency, and API usage.

## Data isolation

Each tenant owns:

- one API endpoint slug and one or more API keys;
- one source/result database configuration;
- one pgvector table;
- one BM25 SQLite file;
- tenant-prefixed Redis keys;
- tenant-scoped usage and recent-search records.

Startup rejects shared endpoint slugs, API keys, pgvector tables, and BM25 files. Tenant identity is also stored in vector metadata and verified after retrieval.

## Ingestion

The ingestion job reads the configured search-ready table in bounded batches. It upserts BM25 data, skips vectors whose content hash and embedding model are unchanged, embeds only changed rows, and writes to the tenant pgvector table.

Deletion reconciliation is an explicit full-scan operation. A limited scan cannot reconcile deletions because unseen source rows may still be valid. A full replacement clears only the selected tenant's vector source and BM25 index.

## Storage model

The pgvector table stores a stable document ID, source text, JSON metadata, and a fixed-dimension vector. HNSW parameters are tenant-configurable:

- `m` controls graph connectivity and index size;
- `ef_construction` controls build quality and ingestion cost;
- `ef_search` controls query recall and query CPU.

The default `m=16`, `ef_construction=64`, and `ef_search=100` are balanced CPU-host values. Tune `ef_search` first when recall is insufficient, and validate latency under representative concurrency.

## 8 GB deployment profile

A single API process shares one hosted-provider chain across tenant engines. The service cache should retain only the active tenant on an 8 GB host, and tenant search concurrency should initially remain one. The default profile over-fetches up to 80 results from each retrieval source, applies intent shaping to a 40-item hybrid recall window, reranks one complete 20-result page, and truncates each API candidate to 300 characters. Redis and pgvector run as separate containers with persistent volumes. No local reranker weights or model cache are required.

## Security

- Terminate TLS at a reverse proxy and bind the application port to loopback.
- Do not publish Redis or pgvector to the public network.
- Store credentials outside the image and repository.
- Use database TLS verification for remote production databases.
- Return only tenant-approved public fields.
- Keep diagnostic event content redacted and bounded.

## Scaling path

Before adding API workers, account for process-local state, database pools, and provider rate limits. Scale in this order:

1. Measure per-stage latency and memory.
2. Move all shared state to external services.
3. Confirm provider quotas and database capacity support the worker count.
4. Add workers or hosts behind a load balancer.
5. Re-run retrieval and load evaluations.

The API contract does not depend on a single host, but the 8 GB profile intentionally optimizes for one warm worker.
