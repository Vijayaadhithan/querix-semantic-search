# Durable search analytics

MySQL tenants can opt into durable, per-request search analytics with the
tenant profile `analytics` section. Gainr enables this feature and stores two
tables in its configured company database:

- `semantic_search_history` is the tenant-facing table. It stores only the
  normalized query and UTC timestamp. `id` is its table key and `request_id`
  is retained solely for idempotent delivery and correlation.
- `semantic_search_api_usage` is the operator-facing table, with one row per
  search request. It stores company, execution path, result counts, status,
  final end-to-end latency, aggregate API calls, aggregate token counts, and
  UTC timestamp. Planner, embedding, and reranker attempts—including
  provider, model, per-attempt tokens/duration, and failure reason—are
  preserved in `attempts_json`.

The tables deliberately do not store API keys, authorization headers, IP
addresses, user IDs, query hashes, route reasons, resolved filters, or product
payloads. Query text can contain personal information, so database access and
backups should follow the company's data retention and access policy.

Both tables currently use Gainr's configured MySQL database. The
operator-facing table uses `(request_id, attempt_number)` instead of a foreign
key to the tenant table, so it can move to a separately configured internal
database later without changing the event format or losing correlation.

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
SQLite WAL. New spool records use the same minimized field set and do not
retain user IDs, route reasons, or resolved filters. The existing daily 03:00
IST ingestion job runs:

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
final end-to-end API latency measured by the Gainr compatibility endpoint.
Durations inside `attempts_json` apply only to individual provider attempts;
they can overlap and must not be summed to infer endpoint latency. Ollama
embedding calls are counted even though its embedding endpoint does not report
token usage.
