# Search Architecture

## Purpose

The service turns natural-language catalogue queries into tenant-isolated, filter-aware, ranked product results. The configured company database (MySQL or PostgreSQL) remains the source of truth. Retrieval indexes store only the data required to find candidates; returned cards are hydrated from the canonical result table.

## Components

| Component | Responsibility |
|---|---|
| Tenant registry | Validates API keys, endpoint slugs, database connections, pgvector tables, and BM25 paths |
| Query planner | Uses a shared prompt plus tenant context to extract intent and conservative constraints for semantic searches |
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
3. Exact catalogue categories with simple stated constraints use the
   deterministic indexed-database path and skip every model and retrieval
   provider.
4. Descriptive, ambiguous, misspelled, or multilingual requests use the
   semantic path. The planner applies the shared prompt, tenant prompt context,
   and tenant-scoped semantic aliases.
5. Ollama creates the query embedding; pgvector and standalone BM25 retrieve
   independent candidate windows.
6. Reciprocal-rank fusion and intent shaping create a provider-independent
   fallback order.
7. The hosted reranker scores the bounded candidate set.
8. Ranking policy demotes or removes low-confidence and wrong-intent results.
9. IDs are hydrated from the canonical result table.
10. The canonical API returns a cursor; compatibility adapters may expose
    page-number pagination. Eligible responses enter Redis with diagnostics.

## Routing and tenant language

Deterministic routing requires an exact tenant catalogue term. Fuzzy spellings,
phonetic neighbours, model-inferred categories, and aliases are not converted
into hard category filters. This prevents unrelated pairs such as `escort` and
`resort` from collapsing into one category. A tenant alias may help the semantic
planner understand colloquial, transliterated, or domain-specific wording, but
it remains relevance evidence rather than an exact database constraint.

The planner's base system prompt is common to all tenants. Each tenant may add
`planner.prompt_context` and `planner.query_aliases` in its YAML profile. Alias
configuration is included in the plan-cache fingerprint, and plan/result cache
keys are tenant-prefixed, so language guidance cannot leak across companies.

## Ranking and failure behavior

Semantic ranking is the primary result order for the semantic path. BM25
protects exact names, identifiers, and rare words. Explicit client filters and
exact user-stated catalogue constraints are hard. A category inferred by the
query model is a soft preference unless the user supplied it exactly.

Retrieval and reranking are fail-open. If vector/Ollama fails, standalone BM25
can still serve lexical candidates. If BM25 fails, pgvector can continue. A
reranker timeout, rate limit, or provider error retains the hybrid order. The
request fails only when both retrieval paths fail. Degraded responses are not
written to the result cache, so a temporary failure cannot become sticky.

The ranked window must cover every page that should preserve semantic order. Increasing it improves deep-page consistency but increases provider latency and token usage. Candidate and document-length changes must therefore be evaluated for reciprocal rank, latency, and API usage.

## Data isolation

Each tenant owns:

- one API endpoint slug and one or more API keys;
- one source/result database configuration;
- one pgvector table;
- one BM25 SQLite file;
- tenant-prefixed Redis keys;
- tenant-scoped usage and recent-search records.

Startup rejects shared endpoint slugs, API keys, company search-data tables,
pgvector tables, and BM25 files. Tenant identity is also stored in vector
metadata and verified after retrieval.

## Ingestion

The ingestion job reads the configured search-ready table in bounded batches. It upserts BM25 data, skips vectors whose content hash and embedding model are unchanged, embeds only changed rows, and writes to the tenant pgvector table.

Indexed document IDs use the tenant's stable `database.index_namespace`. This
allows a validated index to move from a local or staging database to production
without recalculating unchanged embeddings. An explicit namespace migration
re-keys transferred vectors to the authoritative company's identity while
preserving their embedding values.

Production runs the guarded incremental job around 03:00 IST. It prevents
overlap, reconciles deletions after a full scan, and restarts the API only after
a successful run. An unchanged scan does not advance the BM25 revision.

Deletion reconciliation is an explicit full-scan operation. A limited scan cannot reconcile deletions because unseen source rows may still be valid. A full replacement clears only the selected tenant's vector source and BM25 index.

## Storage model

The pgvector table stores a stable document ID, source text, JSON metadata, and a fixed-dimension vector. HNSW parameters are tenant-configurable:

- `m` controls graph connectivity and index size;
- `ef_construction` controls build quality and ingestion cost;
- `ef_search` controls query recall and query CPU.

The default `m=16`, `ef_construction=64`, and `ef_search=100` are balanced CPU-host values. Tune `ef_search` first when recall is insufficient, and validate latency under representative concurrency.

## 8 GB deployment profile

A single API process shares one hosted-provider chain across tenant engines. The service cache retains the active tenant on an 8 GB host, and tenant search concurrency remains one. Excess work waits for a bounded interval and then returns `503` with `Retry-After`. The default profile over-fetches from each retrieval source, applies intent shaping to a 40-item hybrid recall window, reranks one complete 20-result page, and truncates each API candidate to 300 characters. API, Redis, pgvector, and Docker-managed Ollama have explicit memory and log limits. No local reranker weights are required.

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
