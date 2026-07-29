# Durable search analytics

MySQL tenants can opt into durable, per-request search analytics with the
tenant profile `analytics` section. Gainr enables this feature and stores two
tables in its configured company database:

- `semantic_search_history` stores one row per search request: UTC timestamp,
  company and optional user ID, normalized query, route, filters, page,
  result counts, cache state, total latency, aggregate provider calls, and
  aggregate token counts.
- `semantic_search_api_usage` stores the planner, embedding, and reranker
  attempts associated with that request, including provider, model, operation,
  status, duration, API-call count, and provider-reported tokens.

The tables deliberately do not store API keys, authorization headers, IP
addresses, or product payloads. Query text and user IDs can contain personal
information, so database access and backups should follow the company's data
retention and access policy.

Create or verify the tables with:

```bash
python scripts/migrate_search_analytics.py --company gainr
```

The migration is idempotent and uses the selected tenant profile's own MySQL
credentials. Production deployment runs it after building the API image and
before restarting services. Enable analytics separately in each tenant
profile; Gainr is the only enabled tenant currently.

`SEARCH_ANALYTICS_DELIVERY_MODE=immediate` is the local-development default.
Searches enqueue one small in-memory event and do not wait for a MySQL insert.
A bounded worker writes the parent and child rows in one transaction.

Production uses `SEARCH_ANALYTICS_DELIVERY_MODE=daily_spool`. The same bounded
request-path queue writes to `storage/search_analytics_spool.sqlite3` using
SQLite WAL. The existing daily 03:00 IST ingestion job runs:

```bash
python scripts/flush_search_analytics.py --company gainr
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
FROM semantic_search_history
GROUP BY DATE(created_at), execution_path
ORDER BY search_date DESC, execution_path;
```

Example provider usage:

```sql
SELECT
    DATE(created_at) AS usage_date,
    provider,
    model,
    operation,
    status,
    SUM(api_calls) AS api_calls,
    SUM(total_tokens) AS total_tokens
FROM semantic_search_api_usage
GROUP BY DATE(created_at), provider, model, operation, status
ORDER BY usage_date DESC, provider, operation;
```

Each history row represents one incoming search request, so `COUNT(*)` is the
incoming request count. `SUM(api_call_count)` is the number of downstream
planner, embedding, and reranker calls. The child table retains every provider
attempt and its provider-reported token counts. Ollama embedding calls are
counted even though its embedding endpoint does not report token usage.
