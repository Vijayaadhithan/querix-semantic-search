# Durable search analytics

MySQL tenants can opt into durable, per-request search analytics with the
tenant profile `analytics` section. An enabled tenant stores two tables in its
configured company database:

- `semantic_search_history` is the tenant-facing table. It stores only the
  normalized query and UTC timestamp. `id` is its table key and `request_id`
  is retained solely for idempotent delivery and correlation.
- `semantic_search_api_usage` is the operator-facing table, with one row per
  search request. It stores company, execution path, result counts, status,
  final end-to-end latency, aggregate API calls, aggregate token counts,
  nullable plan/result cache flags, an allowlisted `timings_json` object,
  an allowlisted resolved-filter `context_json` object, and UTC timestamp.
  Planner, embedding, and reranker attempts—including
  provider, model, per-attempt tokens/duration, and failure reason—are
  preserved in `attempts_json`.

The tables deliberately do not store API keys, authorization headers, IP
addresses, user IDs, query hashes, route reasons, arbitrary request payloads,
or product payloads. `context_json` contains only resolved category, location,
rental-duration, price-bound, and target-ad-type fields needed for dashboard
filters. Query text can contain personal information, so database access and
backups should follow the company's data retention and access policy.

Both tables currently use the enabled tenant's configured MySQL database. The
operator-facing table uses `request_id` as its unique idempotency and
correlation key, without a foreign key to the tenant table. Provider attempts
remain ordered inside `attempts_json`. This lets the operator table move to a
separately configured internal database later without changing the event
format or losing correlation.

Create or verify the tables with:

```bash
python scripts/migrate_search_analytics.py --company acme
```

The migration is idempotent and uses the selected tenant profile's own MySQL
credentials. Production deployment runs it after building the API image and
before restarting services. Enable analytics separately in each tenant
profile; enable it independently for each tenant that needs durable analytics.

`SEARCH_ANALYTICS_DELIVERY_MODE=immediate` is the local-development default.
Searches enqueue one small in-memory event and do not wait for a MySQL insert.
A bounded worker writes the parent and child rows in one transaction.

Production uses `SEARCH_ANALYTICS_DELIVERY_MODE=daily_spool`. The same bounded
request-path queue writes to `storage/search_analytics_spool.sqlite3` using
SQLite WAL. New spool records use the same minimized field set and do not
retain user IDs, route reasons, or arbitrary request fields. The independent
two-hour analytics timer runs:

```bash
python scripts/flush_search_analytics.py --company acme
```

The uploader takes a stable snapshot and commits idempotent batches to the
selected tenant's external MySQL database. Only rows confirmed by MySQL are
removed locally. Failed batches remain for the next scheduled or manual retry
and do not stop ingestion. After successful deletion, the uploader truncates
the WAL and runs incremental vacuum so local disk space is reclaimed. Searches
arriving during an upload remain in the spool for the next run.

Graceful API shutdown waits briefly for queued events to reach either MySQL or
the local spool. Queue counters are process-local operational diagnostics, not
a replacement for database monitoring.

Example daily totals:

```sql
SELECT
    DATE(created_at) AS search_date,
    execution_path,
    COUNT(*) AS searches,
    SUM(api_call_count) AS external_api_calls,
    SUM(total_tokens) AS total_tokens,
    ROUND(AVG(duration_ms), 1) AS average_latency_ms
FROM semantic_search_api_usage
GROUP BY DATE(created_at), execution_path
ORDER BY search_date DESC, execution_path;
```

Example tenant search history:

```sql
SELECT
    id,
    query_text,
    created_at
FROM semantic_search_history
ORDER BY created_at DESC;
```

Each internal usage row represents one incoming search request, so `COUNT(*)`
is the request count and `SUM(api_call_count)` is the number of downstream
planner, embedding, and reranker calls. Its top-level `duration_ms` is the
server-side search-processing latency measured by a monotonic high-resolution
clock. For a tenant using a compatibility adapter, it covers that workflow
through result mapping;
it does not include internet transit or frontend rendering. Durations inside
`attempts_json` apply only to individual provider attempts; they can overlap
and must not be summed to infer endpoint latency. The same applies to values in
`timings_json`: retrieval can run in parallel and speculative work can overlap
other stages. `duration_ms`/`total_server_ms` is the authoritative total.
Nullable cache or timing values mean the value was unavailable, particularly
for rows written before this additive schema version. Ollama embedding calls
are counted even though its embedding endpoint does not report token usage.

An empty `query_text` can be a valid filter-only catalogue request carrying
city, category, duration, price, or wanted/offer context. These requests remain
in operational API totals and are classified as browsing by the analytics
snapshot. They are excluded from text-query demand and text-query zero-result
rates, preventing catalogue browsing from appearing as blank search terms or
inflating unmet textual demand.

Text-search zero-result rates use only requests whose API status is successful.
Failed text requests remain visible in operational failure totals and as a
separate failed-text-request count, but are not interpreted as unmet demand.

Authenticated requests that fail after search processing starts are retained
with `status = 'failure'`, zero results, and one internal failure attempt. The
attempt stores only the exception class in `failure_reason`, never the
exception message or provider response. This makes daily success/failure
metrics representative without storing additional sensitive diagnostics.
