# Semantic Advertisement Search Architecture

## 1. Purpose

This document is the stable technical description of the implemented search
system. It explains component boundaries, routing, ranking, consistency,
multi-company isolation, failure behavior, and scaling choices. Operational
commands belong in `production_search_operations.md`; presentation material
belongs in `hackathon_technical_guide.tex`.

## 2. Design Goals

- Understand explicit catalogue queries and functional, need-based language.
- Preserve browse/filter behavior for broad queries instead of reducing the
  catalogue to an arbitrary semantic top-K.
- Keep exact constraints separate from semantic meaning.
- Return current canonical database rows rather than copies from a vector
  index.
- Avoid model cost and latency when deterministic rules are sufficient.
- Degrade predictably when Redis or a hosted model is unavailable.
- Isolate every company's data, credentials, limits, and API contract.
- Make routing, provider choice, fallbacks, and latency observable without
  logging private query text.

## 3. Non-Goals

- The query model does not generate advertisements or final answers.
- The vector store is not the canonical product database.
- The system is not an unbounded deep-pagination engine for semantic rankings.
- A tenant prompt cannot make arbitrary database fields safe automatically.
- The current CLI ingestion workflow is not a distributed streaming platform.

## 4. Data Boundaries

The system deliberately separates four data roles.

| Data role | Store | Contents | Authority |
|---|---|---|---|
| Search-ready source | MySQL/PostgreSQL | Preprocessed retrieval text, filter metadata, stable ID | Input to indexes |
| Vector index | Chroma/pgvector | Embeddings, retrieval text, filter metadata, stable ID | Candidate retrieval only |
| Lexical/filter index | SQLite FTS5/BM25 | BM25 text, structured fields, relationship catalogue, revision | Candidate retrieval and browse/filter |
| Result table | MySQL/PostgreSQL | Current public advertisement fields and current type | Canonical response data |

The final response is hydrated from the result table. Indexes can determine
which IDs are relevant, but they cannot directly supply the canonical card.

This boundary provides a useful consistency property: a display-only update in
the result table is visible immediately. A content or filter update that should
change retrieval order requires ingestion.

## 5. Request Boundary

```text
HTTP request
    |
    v
tenant profile lookup
    |
    +-- endpoint slug exists?
    +-- API key belongs to the same company?
    +-- request fields match the company mapping?
    +-- rate-limit token available?
    |
    v
company-scoped SearchService
```

The tenant registry is loaded from `configs/tenants/`. Startup validation
rejects unsafe or shared endpoint slugs, API keys, vector namespaces, pgvector
tables, and BM25 paths.

Each profile controls:

- company ID and endpoint slug
- API-key environment variables
- database backend, tables, columns, timeouts, pool, and TLS
- Chroma directory/collection or pgvector table
- BM25 database path
- allowed filter fields and types
- incoming request-field mapping
- public response-field allowlist and outgoing field mapping
- rate limit
- planner enablement and bounded domain context
- optional compatibility adapter

## 6. Three Execution Paths

### 6.1 Deterministic Filter Path

This path is selected only when all meaningful query terms can be explained as:

- an indexed main category or subcategory
- state, city, or locality
- rental duration
- minimum or maximum fee
- offer/wanted perspective
- supported price ordering
- harmless catalogue request language

Examples:

```text
bike
bike in Chennai
bike in Chennai under 1000
1000 rent car
camera per day
someone looking for generators
```

The grammar is compositional. It does not store complete example queries.
Category and location typo recovery uses the current BM25 filter catalogue and
requires a unique high-confidence match. A standalone number becomes a maximum
budget only in a validated simple catalogue query; likely quantities, years,
and specifications such as `2 cars`, `2020 car`, and `1000 cc car` are guarded.

Execution:

1. Normalize whitespace and conservative aliases/typos.
2. Resolve explicit values against indexed filter values.
3. Reject the fast path if unexplained descriptive terms remain.
4. Query the structured BM25 product table with validated filters and sort.
5. Fetch current offer/wanted type values from the company database.
6. Hydrate the selected IDs from the canonical result table.

The hosted planner, query embedding, vector search, relevance BM25 search, and
reranker are skipped.

### 6.2 Semantic Hybrid Path

This path is selected when descriptive, functional, brand/model, or ambiguous
meaning remains:

```text
red bike with ABS
portable camera for distant subjects
vehicle for recreational driving on rough terrain
equipment for lifting material to a second floor
```

Execution:

1. Check the normalized query-plan cache.
2. If needed, ask the hosted fallback chain for schema-constrained JSON.
3. Deterministically validate the returned plan.
4. Separate hard filters from inferred category hints.
5. Run vector and lexical retrieval concurrently.
6. Fuse ranked candidate lists.
7. Remove offer/wanted mismatches using current database type values.
8. Rerank a bounded candidate set.
9. Append a structured related tail when a larger result window is required.
10. Hydrate current canonical rows.
11. Cache ordered IDs and interpretation metadata.

### 6.3 Result-Cache Path

For an identical normalized request within the configured TTL:

1. Redis returns ordered IDs, ranked/related boundaries, and interpreted
   metadata.
2. Planning, embedding, retrieval, fusion, and reranking are skipped.
3. Current canonical rows are fetched again.

The cache intentionally does not store full rows. Its key includes company,
normalized query, requested/ranking windows, reranker configuration, and BM25
revision/count. Any BM25 ingestion mutation changes revision metadata and makes
old result keys unreachable.

## 7. Query Plan Contract

The hosted planner must return JSON matching a closed schema:

```json
{
  "semantic_query": "portable equipment for recording a distant wedding",
  "keyword_query": "portable camera recorder microphone wedding",
  "target_ad_type": "offer",
  "filters": {
    "main_category": null,
    "subcategory": null,
    "state": null,
    "city": null,
    "locality": null,
    "rental_duration": null,
    "min_rental_fee": null,
    "max_rental_fee": null
  }
}
```

The schema disallows extra fields. The deterministic validator then:

- normalizes duration and price
- corrects offer/wanted perspective
- resolves values only against the indexed catalogue
- completes a parent only from a unique stored relationship
- moves functional category guesses to `inferred_categories`
- records unresolved values rather than silently applying invalid filters

Hosted models are tried in configured order. Timeouts, connection failures,
HTTP 408/429, and 5xx failures advance to the next model. A complete provider
failure produces a conservative default semantic plan rather than invented
filters.

## 8. Hard Filters and Soft Hints

Hard filters are claims that the system can safely enforce. Examples:

- explicit `in Chennai`
- explicit `per day`
- explicit `under 1000`
- explicit `show bike listings`
- an unambiguous parent derived from an indexed child relationship

Soft hints are possible categories inferred from the user's described need.
For example, `something that records a wedding from far away` may imply
`Camera`, but enforcing that as a filter could remove drones, microphones,
recorders, lenses, and gimbals.

This is the core intent-safety rule:

```text
explicit constraint -> hard filter
functional inference -> ranking hint
```

Explicit UI filters take precedence over query-derived values. Automatic
extraction fills only missing filter fields.

## 9. Hybrid Retrieval

### 9.1 Vector Retrieval

Ollama generates a normalized `embeddinggemma:latest` query vector. Chroma or
pgvector retrieves semantically similar search-ready records. Vector search is
useful for paraphrases, functional needs, and vocabulary mismatch.

For the current large Chroma collection, the implementation can retrieve a
bounded unfiltered HNSW window and enforce metadata constraints in memory. This
avoids a slow metadata-filtered scan while retaining the same hard-filter
contract.

### 9.2 Lexical Retrieval

SQLite FTS5/BM25 retrieves literal names, codes, brands, model numbers, and
rare keywords. The same database stores structured product rows and unique
taxonomy/location relationships used by the planner.

### 9.3 Reciprocal Rank Fusion

Vector and lexical scores are not directly comparable. The implementation
fuses ranks:

```text
RRF(d) = w_v / (k + rank_v(d)) + w_b / (k + rank_b(d)) + soft_hint(d)
```

The current default is `k = 60` with equal vector and BM25 weights. A small
soft-category boost can be added when candidate metadata matches an inferred
category.

RRF is robust because it depends on ordering rather than the calibration of two
different score systems.

## 10. Reranking and Result Tiers

The bounded fused window is reranked in this order:

```text
Jina -> Voyage rerank-2.5 -> Voyage rerank-2.5-lite
```

Providers are skipped when their key is absent. Provider failure advances to
the next configured provider. An optional local transformer can be placed in
the order, but is not the production default.

The reranker receives the original query plus useful search concepts and soft
category context. Documents are truncated to a configured character limit to
bound cost. Exact duplicate titles are deferred within the primary window to
improve visible diversity.

Responses label results:

- `ranked`: primary cross-encoder-ranked result
- `related`: structured tail after the primary semantic window
- `filtered`: deterministic browse/filter result

The related tail is not presented as equally semantically scored. The tier
makes that distinction explicit.

## 11. Broad Queries and Pagination

`top_k` is a ranking-window parameter, not the catalogue size.

A broad query such as `bike` uses the deterministic path so structured
pagination and filters can cover the relevant catalogue. Semantic requests use
a bounded combined window because stable deep pagination across mutable vector,
BM25, provider, and database state is expensive and misleading.

The generic API creates an opaque company-bound cursor over a bounded result
session. A cursor:

- belongs to one company
- carries no reusable cross-company authority
- has a TTL
- returns HTTP 410 after expiry
- cannot be supplied together with a new query

Gainr's compatibility endpoint keeps its existing page-number response
contract.

## 12. Ingestion

Database ingestion is incremental by content hash:

```text
read bounded source page
    -> close DB cursor/connection
    -> prepare text and metadata
    -> upsert BM25
    -> embed only new/changed records
    -> upsert vector store
```

Closing the database page before CPU embedding prevents a long-running Ollama
batch from holding an idle streaming socket.

Modes:

- check: source validation only
- incremental: new/changed vector and BM25 upsert
- reconcile: remove indexed IDs absent after a successful full scan
- BM25-only: no embedding call
- force re-embed: rebuild vectors even when hashes match
- replace source: clear/rebuild one tenant's indexes

Source and result database tables are read-only from this repository.
`replace-source` affects retrieval indexes only.

## 13. Consistency Model

| Change | Visible without ingestion? | Reason |
|---|---|---|
| Public title/description card field only | Yes | Canonical row is hydrated per response |
| Soft-delete/current type checked at hydration | Yes | Current DB value is read |
| Retrieval text | No | Vector/BM25 content must be refreshed |
| Category/location/duration/fee used by retrieval | No | Structured index must be refreshed |
| New/deleted advertisement | Requires refresh/reconciliation | Candidate indexes must change |

The result cache does not weaken this model because only IDs and ordering are
cached.

## 14. Multi-Company Isolation

Isolation is enforced at configuration load, request routing, cache keys, index
paths, database connections, cursors, response mapping, and usage records.

```text
company A key -> company A endpoint -> company A engine
                               -> company A DB
                               -> company A vector namespace
                               -> company A BM25 file
                               -> company A Redis namespaces

company B key cannot enter that path
```

The application rejects a valid key used against another company's endpoint
with HTTP 403. Unknown endpoints return HTTP 404. Missing/invalid keys return
HTTP 401.

## 15. Security Model

Implemented controls:

- closed request models with unexpected-field rejection
- API-key-to-company binding
- separate admin key
- tenant rate limits
- database identifier/config validation
- bounded database pools and timeouts
- TLS configuration support
- public-field allowlists
- tenant-scoped cursor and cache state
- raw-query omission from normal logs and admin event data
- no provider or database secrets in responses

Deployment responsibilities:

- HTTPS termination
- secret-manager injection and rotation
- network restrictions
- read-only source/result database grants
- `verify-full` TLS for remote databases
- trusted server-side derivation of user identity headers
- backups and restore tests for durable stores
- provider quota/cost alerts

## 16. Resilience and Failure Behavior

| Failure | Behavior |
|---|---|
| Redis unavailable | Query plans fall back to bounded process memory; result cache/rate coordination is reduced |
| First query-planner model unavailable | Next configured model is attempted |
| All query-planner models fail | Conservative semantic fallback with no invented filter |
| Primary reranker unavailable | Next configured provider is attempted |
| All rerankers fail | Request fails with an observable 503 rather than silently claiming ranked quality |
| Vector/BM25 index missing | Company health/search reports 503; ingestion/doctor is required |
| Database unavailable | Canonical hydration cannot be guaranteed; request fails |
| Expired cursor | HTTP 410 |
| Tenant rate exceeded | HTTP 429 |

The choice to fail when every reranker fails is deliberate: returning fused
candidates under the same response semantics would conceal a ranking-quality
degradation. A future product policy could introduce an explicitly labelled
degraded mode.

## 17. Observability

One trace ID connects the stage log:

```text
search start
  -> result-cache lookup
  -> deterministic or hosted plan
  -> vector || BM25 retrieval
  -> fusion and type validation
  -> provider rerank attempts
  -> related tail
  -> database mapping
  -> completion
```

Recorded data includes:

- execution path
- model/provider and fallback reason
- cache hits
- candidate/result counts
- stage and total durations
- embedding load/total time
- error type
- per-company provider token usage

Normal logs and protected event endpoints omit raw query text, filter values,
product contents, exception messages, and credentials.

## 18. Performance Strategy

- Skip all inference for deterministic queries.
- Run vector and BM25 retrieval concurrently.
- Cache normalized plans.
- Cache only ordered result IDs.
- Load the shared reranker once per process.
- Bound retrieval, reranking, document length, database pool size, and active
  searches.
- Page source ingestion and close DB resources before embedding.
- Use a structured tail for browse depth after the expensive semantic window.

Performance must be measured by route. A workload dominated by result-cache
hits does not represent uncached semantic throughput.

Recommended test classes:

- unique deterministic queries
- unique semantic queries
- repeated semantic queries
- provider timeout/fallback queries
- mixed filters and compatibility endpoints
- concurrent requests at the planned host limit

## 19. Scaling Path

Current default: one API worker on a single host with tenant-isolated Chroma
directories and shared local model/provider limit state.

Reasonable next steps:

1. Move durable vector retrieval to pgvector or another shared service before
   adding API replicas.
2. Keep Redis as shared cache, rate-limit, and cursor state.
3. Replace in-process search event retention with durable telemetry.
4. Build index generations and atomically promote a complete vector/BM25 pair.
5. Move ingestion to a durable job queue with progress and retries.
6. Add load-based autoscaling only after provider and database concurrency
   budgets are defined.

Adding Uvicorn workers without addressing local Chroma access, model memory,
and per-process provider budgets is not a valid scaling plan.

## 20. Alternatives and Tradeoffs

### Vector Only

Advantages: simple; strong paraphrase matching.

Rejected as the only retriever because exact model names, IDs, and rare terms
often require lexical matching.

### BM25 Only

Advantages: fast; explainable; strong exact matching.

Rejected as the only retriever because functional need and vocabulary mismatch
are central requirements.

### LLM for Every Query

Advantages: one conceptual path.

Rejected because broad explicit catalogue queries do not justify extra
latency, cost, availability dependence, or filter hallucination risk.

### Relational Database Search Only

Advantages: one source of truth.

Rejected for relevance retrieval because portable semantic similarity and
hybrid rank fusion are not the relational serving database's role. The
database remains authoritative for final rows.

### Cache Full Responses

Advantages: lowest hit latency.

Rejected because rows become stale and payloads consume more Redis memory.
IDs-only caching preserves fresh canonical fields.

### Hard-Filter Inferred Categories

Advantages: smaller candidate set.

Rejected because situation-based queries can map to multiple useful product
types. Inferred categories remain soft hints.

## 21. Verification Strategy

The project has four complementary verification layers:

1. Unit/integration-style tests for planner, API, cache, tenant, compatibility,
   ingestion, provider, and usage behavior.
2. Query-plan cases that assert routing and extracted constraints.
3. End-to-end labelled retrieval cases that measure rank behavior.
4. Environment doctor and concurrent load test on the target deployment.

The suite verified on 2026-07-03 contains 137 passing tests. That is a
regression statement, not a claim that the small labelled set represents all
production traffic.

## 22. Ownership Map

| Concern | Primary module |
|---|---|
| API/auth/pagination/monitoring | `src/api.py` |
| Complete search orchestration | `src/search_engine.py` |
| Deterministic grammar and plan safety | `src/query_planner.py` |
| Hosted structured planning | `src/gemini_client.py` |
| Local embeddings | `src/ollama_client.py` |
| Vector/BM25 retrieval and fusion | `src/retrieval.py` |
| Reranker providers/fallback | `src/reranker.py` |
| Redis cache/rate primitive | `src/redis_cache.py` |
| Tenant profiles and isolation | `src/tenant_config.py` |
| Database/vector dispatch | `src/database_store.py`, `src/vector_store.py` |
| Ingestion orchestration | `src/ingestion_service.py`, `src/ingest.py` |
| Gainr legacy API adapter | `src/gainr_compat.py` |

Architectural retrieval changes should land in `ProductSearchEngine` rather
than creating a separate chat-only or endpoint-only ranking flow.
