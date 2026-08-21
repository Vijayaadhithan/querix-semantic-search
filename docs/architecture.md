# Search Architecture

## Purpose

The service turns natural-language catalogue queries into tenant-isolated, filter-aware, ranked product results. The configured tenant database (MySQL or PostgreSQL) remains the source of truth. Retrieval indexes store only the data required to find candidates; returned cards are hydrated from the canonical result table.

## Components

| Component | Responsibility |
|---|---|
| Tenant registry | Validates API keys, endpoint slugs, database connections, pgvector tables, and BM25 paths |
| Query planner | Uses a shared prompt plus tenant context to extract intent and conservative constraints for semantic searches |
| Ollama embedding service | Produces query and ingestion embeddings with the configured model |
| PostgreSQL/pgvector | Stores tenant vectors, retrieval text, and filter metadata; provides HNSW ANN search |
| Persistent BM25 | Provides lexical and exact-term recall |
| Fusion layer | Combines vector and BM25 ranks using reciprocal-rank fusion |
| Tenant search policy | Optionally applies tenant-owned planner rewrites, soft candidate adjustments, and reranker context |
| Hosted reranker chain | Scores the strongest candidates and fails over between configured APIs |
| Canonical database | Supplies current public product fields and visibility state |
| Redis | Stores result and plan caches shared by the API process |
| Usage and event stores | Record tenant-safe request totals and redacted execution diagnostics |
| Daily analytics service | Reads normalized company SQL datasets, publishes atomic snapshots, and serves company-safe or internal dashboard contracts from a separate image |

## Code boundaries

Runtime code is grouped by responsibility under `src/`:

- `api` owns HTTP contracts, routes, request services, and tenant-engine
  lifecycle.
- `analytics_service` owns the separate scheduled SQL extraction, Search/API/Deep/
  Market calculations, versioned snapshot store, and analytics-only API.
- `analytics_service.adapters` owns company-facing analytics response adapters;
  authentication, snapshot access, and internal-admin contracts stay shared.
- `core` owns process settings, tenant profiles, and admission controls.
- `search` owns planning, BM25, retrieval, ranking, and the generic policy
  contract.
- `storage` owns database, pgvector, Redis, and usage persistence adapters.
- `providers` owns external model-provider clients.
- `ingestion` owns document preparation and index synchronization.
- `verticals/<vertical>` owns reusable domain behavior, such as marketplace
  offer/wanted interpretation. A vertical must not contain one client's
  compatibility contract.
- `tenants/<tenant>` owns client compatibility contracts and client-specific
  search exceptions. Generic search modules do not import client vocabulary
  directly.
- `tenants.compatibility` owns the compatibility-adapter registry. The shared
  API routes call the adapter protocol; tenant request models and legacy
  response shapes stay inside the tenant package.
- `cli` owns executable chat, ingestion, and evaluation entry points.

The `tests/` tree mirrors these boundaries so ownership and regression coverage
remain easy to locate.

## Analytics snapshot lifecycle

The analytics service is not part of the search request lifecycle. Every two
hours at `:30` Asia/Kolkata its independent timer reads the configured company
database and API telemetry source, calculates every dashboard module, writes
versioned individual-query records, and atomically switches the active
snapshot. HTTP requests only read that completed snapshot.

Company routes expose Search Intelligence, sanitized Individual Queries, Deep
Analytics, and Market Intelligence. Internal routes expose those modules plus
API Performance and full provider/model/token diagnostics. The two audiences
are materialized separately so internal fields cannot leak through response
filtering mistakes. Company dashboard users receive company-bound, server-side
login sessions; internal users receive an internal role and must still select
one company for every analytics request. Independent host-only company and
internal cookies allow both roles to remain signed in concurrently, while each
route reads only the cookie for its audience. The tenant API-key path remains
available only for service-to-service analytics calls. There is no
cross-company analytics endpoint.

The analytics API is built from `Dockerfile.analytics` and runs as the
`analytics-api` Compose service on port 8010. The semantic-search API continues
to use its existing image and port 8000.

Company-facing dashboard, query-list, and status payloads pass through the
adapter selected by `analytics.adapter` in the tenant YAML. The default adapter
preserves the canonical contract. A custom company adapter may reshape only
that company's responses; it cannot change tenant resolution, authentication,
audience filtering, or another company's stored snapshot. Internal-admin
analytics deliberately bypass company adapters so operational tooling keeps a
stable cross-company contract.

## Request lifecycle

1. The API resolves the tenant from the endpoint and API key.
2. Rate limiting and per-tenant concurrency controls are applied.
3. Exact catalogue categories with simple stated constraints use the
   deterministic indexed-database path and skip every model and retrieval
   provider.
4. Conversational, offer/wanted, descriptive, ambiguous, misspelled, or
   multilingual requests use semantic retrieval. Locally unambiguous requests
   use direct semantic planning; the remaining requests use the shared hosted
   planner with tenant prompt context and tenant-scoped aliases.
5. Ollama creates the query embedding; pgvector and standalone BM25 retrieve
   independent candidate windows.
6. Reciprocal-rank fusion and intent shaping create a provider-independent
   fallback order. The selected tenant search policy may adjust this deeper
   fused pool before the bounded reranker window is selected.
7. The hosted reranker scores the bounded candidate set.
8. Ranking policy demotes or removes low-confidence and wrong-intent results.
9. IDs are hydrated from the canonical result table.
10. The canonical API returns a cursor; compatibility adapters may expose
    page-number pagination. Eligible responses enter Redis with diagnostics.

## Compatibility adapters

The canonical company API is `/api/v1/{company_endpoint}/search`. A tenant that
must preserve an existing mobile/web contract can set `compatibility.adapter`
in its YAML profile. The adapter is resolved through
`tenants.compatibility.build_compatibility_adapter`, then receives raw route
payloads for validation and response shaping.

Adding a new compatibility contract should require:

- a tenant package under `src/tenants/<tenant>/`;
- adapter request/response parsing inside that package;
- one registry entry in `src/tenants/compatibility.py`;
- tenant YAML selecting the adapter name.

Shared API code must not import the new tenant's Pydantic request models.

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

Marketplace-specific interpretation is not selected from `company_id` inside
the planner or engine. `company.search_policy` is resolved while the tenant
engine is built. The default policy is identity-only; a reusable vertical policy
may provide shared marketplace semantics, while a tenant policy may add only
client-specific rewrites, category exceptions, candidate adjustments, or
bounded reranker context. The policy cache key is part of the planner
fingerprint.

A tenant policy may also return a high-confidence `category_intent` for phrases
whose marketplace meaning is unambiguous. The resolved subcategory becomes a
hard tenant-catalogue filter before vector or BM25 retrieval, while ambiguous
or equipment/product phrases remain semantic. These inferred task/service
phrases remain semantic; only literal catalogue/filter expressions can use
deterministic retrieval.

### Tenant and vertical extension model

Catalog categories are data-driven. A new retail, fashion, product, or service
category normally requires only that the tenant's search-ready source exposes
the category and that its planner catalog is rebuilt; it does not require a new
Python rule for every category. Tenant YAML `query_aliases` can add
tenant-scoped spelling, colloquial, or transliteration hints, but aliases are
soft relevance evidence and never fuzzy hard filters.

Add code only when the category has behavior that generic catalog matching
cannot safely infer, such as a service phrase that must map to a different
subcategory, a domain-specific ranking adjustment, or a legacy client payload.
Put reusable behavior in `src/verticals/<vertical>/`, and keep client-only
exceptions in `src/tenants/<tenant>/`. This lets multiple tenants share a
marketplace vertical without copying one client's rules into the common search
engine.

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

The ingestion job reads the configured search-ready table in bounded batches.
It compares embedding identity separately from retrieval metadata. New rows or
changed `embedding_content_hash` values are embedded and written to the tenant
pgvector table. Rows whose embedding text/model are current but whose
`retrieval_metadata_hash` changed reuse their stored vector while updating
vector metadata and BM25 content. Completely unchanged rows are skipped.

The upstream ETL includes normalized listing descriptions in both
`embedding_content` and `bm25_content`. This protects semantic recall and lets
lexical search find rare details that exist only in a description. Changing a
BM25 source-column configuration is a content-contract migration: run one full
upstream ETL rebuild without `--incremental`, publish it atomically, and then
run normal backend incremental ingestion. When the embedding hash is unchanged,
that backend pass is metadata-only and does not call the embedding model.

Indexed document IDs use the tenant's stable `database.index_namespace`. This
allows a validated index to move from a local or staging database to production
without recalculating unchanged embeddings. An explicit namespace migration
re-keys transferred vectors to the authoritative company's identity while
preserving their embedding values.

Production runs a guarded per-tenant shadow-generation job around 03:00 IST.
The active generation remains read-only to ingestion and continues serving.
The inactive pgvector table and BM25 file receive the complete incremental
reconciliation. Count equality, exact-search HNSW recall, representative BM25
overlap, and warm-query success gate activation. The tenant service pool builds the candidate
service outside its routing lock and atomically swaps only after readiness;
in-flight requests retain the previous service until they drain. No API
container restart is part of scheduled ingestion.

The selected slot and generation token live in an atomically published tenant
manifest. The token participates in result-cache keys. Backups include both
physical slots and the manifest; the preceding slot remains available for
rollback. Each tenant has independent slots, locks, paths, tables, and
activation, so one company's ingestion cannot block another company's search.

Deletion reconciliation is an explicit full-scan operation. A limited scan cannot reconcile deletions because unseen source rows may still be valid. A full replacement clears only the selected tenant's vector source and BM25 index.

Moving to another host does not require re-embedding when validated artifacts
are transferred. Restore the tenant pgvector table and its company-specific
BM25 file under `storage/companies/<tenant>/`, preserve the tenant's stable
`database.index_namespace`, then run a full source scan with deletion
reconciliation. If indexes are not transferred, configure the same tenant
profile and run authoritative ingestion; the service rebuilds only that
tenant's isolated pgvector table and BM25 file.

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
