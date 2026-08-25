# Company analytics service

The analytics backend is a separate deployable service. It is not imported by
the semantic-search API image and it never calculates dashboards on an HTTP
request.

## Service and image

Docker Compose service: `analytics-api`.

Dockerfile: `Dockerfile.analytics`.

Default local address: `http://127.0.0.1:8010`.

The image has two entrypoints:

```bash
# Long-running read-only snapshot API.
python -m analytics_service

# One-shot SQL extraction, calculation, and atomic publication.
python -m analytics_service.refresh --company acme
```

The snapshot database defaults to
`storage/analytics/snapshots.sqlite3`. It uses WAL mode, versioned query
records, and an atomic active-version switch. A failed refresh records a
failed run but does not replace or delete the last completed dashboard.

## Data and audience boundaries

Company dashboard modules:

- Search Intelligence (17 curated metrics)
- Individual Queries
- Deep Analytics (19 curated metrics)
- Market Intelligence (9 curated metrics)

Internal dashboard modules:

- API Performance (16 curated metrics);
- Individual Query History for the explicitly selected company;
- provider and model attempts;
- token usage, result counts, and cache state;
- failures, fallbacks, execution paths, total latency, and measured stage
  timings.

The internal service does not aggregate analytics across companies. The
companies endpoint is only a tenant/snapshot inventory. Every dashboard and
query request names exactly one company.

The original exploratory reports remain in the domain source for future
validation, but only the curated catalogue in `metrics.py` is persisted or
returned. Misleading estimates, duplicated reports, provider-specific reports,
and tenant-specific questions are excluded unless explicitly validated and
enabled for that tenant.

Each tenant can override the curated defaults without changing code. Company
and internal profiles are separate. Internal profiles accept only operational
`api_performance` metrics; Search Intelligence, Deep Analytics, and Market
Intelligence remain company-facing. An empty list hides a configurable module:

```yaml
analytics:
  metrics:
    company:
      search_intelligence:
        - q1_category_distribution
        - q3_trending_terms
        - q7_zero_results
      market_intelligence: []
    internal:
      api_performance:
        - q21_success_rate
        - q23_latency_stats
        - q33_failure_reasons
```

Metric names and modules are validated when the service loads the tenant
configuration. A typo or attempt to expose an internal-only module fails
configuration rather than silently weakening the audience boundary.

## Company analytics adapters

The company-facing dashboard, query-list, and status responses pass through a
tenant adapter selected by `analytics.adapter`. The built-in `default` adapter
preserves the canonical response contract:

```yaml
analytics:
  enabled: true
  endpoint_slug: acme
  adapter: default
```

When a company needs a different response shape, add its adapter beside
`analytics_service/adapters.py`, register one factory there, and select it in
that company's YAML. Shared authentication, endpoint-to-company resolution,
snapshot storage, and audience filtering remain outside the adapter. Internal
admin endpoints deliberately keep one stable operational response contract.

Company query records include query classification and search outcome. They do
not include provider names, model names, execution paths, tokens, attempts,
failure reasons, or internal latency diagnostics. Internal query records retain
the full operational projection. Each internal record exposes stable
`performance` and `token_usage` objects plus ordered provider/model `attempts`:

Filter-only requests with an empty text term are catalogue browsing activity,
not failed or malformed text searches. Query records classify every request as
`text_search`, `filtered_browse`, or `catalogue_browse`. Browse records retain
the original empty `normalized_query` for auditability while `query` contains a
readable label such as `Filtered browse: Camera, Chennai`. Company fulfillment,
language, category-demand, and zero-result metrics use only `text_search`
records. Internal request success, latency, provider, and token metrics continue
to include every request. This keeps HTTP success separate from search outcome:
a request can complete successfully and still have a `zero_result` outcome.
Failed text requests are reported separately and excluded from zero-result-rate
denominators because a processing failure is not evidence of unmet catalogue
demand.

```json
{
  "query": "camera rent",
  "request_kind": "text_search",
  "outcome": "fulfilled",
  "performance": {
    "server_duration_ms": 500.123,
    "total_server_duration_ms": 500.123,
    "measurement_scope": "server_search_processing",
    "timing_semantics": "stages_may_overlap_do_not_sum",
    "execution_path": "semantic",
    "cache": {
      "plan_hit": true,
      "result_hit": false
    },
    "stages_ms": {
      "total_server_ms": 500.123,
      "planning_ms": 72.1,
      "embedding_ms": 31.4,
      "retrieval_ms": 190.2,
      "reranking_ms": 84.7,
      "hydration_ms": 54.8,
      "response_mapping_ms": 6.2,
      "session_storage_ms": 0.3
    },
    "downstream_api_calls": 3,
    "attempt_count": 3,
    "successful_attempt_count": 3,
    "failed_attempt_count": 0
  },
  "token_usage": {
    "input_tokens": 100,
    "output_tokens": 20,
    "thought_tokens": 0,
    "total_tokens": 120,
    "tokens_per_result": 6.0
  },
  "attempts": [
    {
      "attempt_number": 1,
      "provider": "groq",
      "model": "groq:openai/gpt-oss-20b",
      "operation": "query_planning",
      "status": "success",
      "duration_ms": 200.456,
      "input_tokens": 100,
      "output_tokens": 20,
      "total_tokens": 120
    }
  ]
}
```

`total_server_duration_ms` (and its compatibility alias `server_duration_ms`)
is measured with a monotonic high-resolution clock around
server-side search processing and stored to three decimal places. It includes
planning, retrieval/database work, result hydration, response mapping, usage
aggregation, and search-session storage. It does not include internet transit,
browser rendering, or FastAPI response serialization after the service returns.
Attempt and stage durations can overlap because retrieval is parallel and some
work is speculative; never sum them to reconstruct total server latency. A
stage value of `null` means it was not measured for that row (including older
rows), while `0` is a measured zero. Cache values are also nullable for older
telemetry. The legacy `api` object remains in the internal response during the
frontend rollout.

Authenticated search-processing failures are written to the same durable
history with `outcome: failure`. Only the exception class is retained as the
internal attempt failure reason; exception messages and provider response
bodies are excluded. Authentication failures and invalid request payloads are
not analytics searches and are not added to query history.

The analytics service reads business data and tenant-facing query history from
the company database. API telemetry can use the company database initially or
a separate internal database. `request_id` correlates the two sources.

## Two-hour refresh

All modules refresh every two hours at `:30` in `Asia/Kolkata`. The independent
`semantic-search-analytics.timer` starts
`scripts/run_scheduled_analytics.sh`, which performs these operations:

1. upload pending search analytics;
2. build and atomically publish the company analytics snapshot;
3. prune expired analytics login sessions.

If step 2 fails, the script records the failure and keeps the prior snapshot
active. Search ingestion remains on its separate daily 03:00 timer and does not
run or restart as part of analytics refreshes.

The production deployment builds both images. It performs an analytics refresh
only when no prior snapshot exists, then starts the analytics API and verifies
its readiness. Ordinary deployments do not recalculate an existing snapshot.

## API endpoints

Company endpoints accept either a company-bound analytics login session or the
tenant's `X-API-Key`. The API-key path exists for server-to-server callers; a
browser frontend must use the login session and must not contain the API key.
Analytics uses a separate product key such as `ACME_ANALYTICS_API_KEY`; it does
not reuse or alter the semantic-search API's `ACME_API_KEY`.

```text
GET /api/v1/{company}/analytics/dashboard
GET /api/v1/{company}/analytics/queries
GET /api/v1/{company}/analytics/status
```

Role-specific browser authentication endpoints:

```text
POST /api/v1/analytics/company/auth/login
GET  /api/v1/analytics/company/auth/me
POST /api/v1/analytics/company/auth/logout

POST /api/v1/analytics/internal/auth/login
GET  /api/v1/analytics/internal/auth/me
POST /api/v1/analytics/internal/auth/logout
```

The following shared endpoints are deprecated but remain available during the
frontend rollout:

```text
POST /api/v1/analytics/auth/login
GET  /api/v1/analytics/auth/me
POST /api/v1/analytics/auth/logout
```

Internal endpoints require an `internal_admin` analytics session:

```text
GET /api/v1/admin/analytics/companies
GET /api/v1/admin/analytics/{company}/dashboard
GET /api/v1/admin/analytics/{company}/queries
```

Dashboard activity filters:

```text
period (24h, 7d, 30d, 90d, all, or custom)
request_scope (all, text_search, or browse)
from / to (custom period boundaries)
outcome
category
language
city
city_id (authoritative structured location ID)
ad_type
execution_path (internal dashboard only)
provider (internal dashboard only)
operation (internal dashboard only)
```

Dashboard filters apply to the `filtered_overview` search/API activity data,
including its main time-series graph. `request_scope` makes the difference
between typed text searches and catalogue/filter browsing explicit. Catalogue,
user, supply, and market
questions are snapshot metrics and are intentionally not rewritten by a
search time filter. Graph buckets and naive custom boundaries use the tenant's
configured `analytics.timezone` (for example, `Asia/Kolkata`). City and city ID
come from the actual resolved request filter captured by the search API; they
are never inferred from query text. The response provides readable city options
paired with their IDs. Records written before filter-context capture are
excluded when a city is selected.

The internal filtered overview reports provider/operation attempts, measured
stage latency, and separate provider-reported token totals for LLM planning and
reranking. A zero token total can mean either no usage or unavailable provider
telemetry, so the response also includes attempt and API-call counts.

Individual-query filters:

```text
limit
cursor
sort_by (`created_at`, `results`, or `outcome`; internal also supports
`execution_path`, `duration`, and `tokens`)
sort_direction (`asc` or `desc`)
query
outcome
category
execution_path (internal endpoint only)
language
request_kind
city_id
subcategory_id
ad_type
diagnostic_code (internal endpoint only)
has_filters
include_filtered_results
from
to
```

Sorting is performed by the service before pagination, so “highest results” or
“slowest duration” applies to the complete filtered result set rather than only
the rows already loaded in the browser. A cursor is valid only for the sort
field and direction that created it.

The query endpoint uses a stable descending `(created_at, request_id)` cursor.
It never uses offset pagination.

Analytics responses set `Cache-Control: private, no-store` and must remain
behind authenticated TLS termination.

Example:

```bash
curl -fsS \
  "http://127.0.0.1:8010/api/v1/acme/analytics/queries?outcome=zero_result&limit=50" \
  -H "X-API-Key: $ACME_ANALYTICS_API_KEY" | jq
```

Create the first internal user interactively:

```console
docker compose run --rm analytics-api \
  python -m analytics_service.users create \
    --username analytics-admin \
    --role internal_admin
```

Create a company-bound dashboard user:

```console
docker compose run --rm analytics-api \
  python -m analytics_service.users create \
    --username acme-owner \
    --role company_user \
    --company acme
```

Passwords are read from a hidden interactive prompt, never from the command
line. `--password-stdin` is available for secret-manager automation. User
management also supports `list`, `set-password`, `set-active`, and
`prune-sessions`.

Change an analytics password through the hidden interactive prompt:

```console
docker compose run --rm analytics-api \
  python -m analytics_service.users set-password \
    --username <analytics-username>
```

The plaintext password is never stored in `.env` or `.env.keys`. Changing it
updates the salted hash in `storage/analytics/snapshots.sqlite3` and revokes
every active session for that account.

For operator-managed recoverability, use a dedicated credential file per
portal/account. These files match `.env.analytics.*.credentials`, are ignored
by Git, excluded from Docker build context, and must remain mode `0600`. They
are not loaded by either long-running API container.

Generate a strong company credential file without printing its password:

```console
docker compose run --rm --no-deps --user root \
  -v "$PWD:/credentials" analytics-api \
  python -m analytics_service.users generate-credentials \
    --file /credentials/.env.analytics.acme.credentials \
    --username acme-owner \
    --role company_user \
    --company acme
```

Generate the internal file similarly:

```console
docker compose run --rm --no-deps --user root \
  -v "$PWD:/credentials" analytics-api \
  python -m analytics_service.users generate-credentials \
    --file /credentials/.env.analytics.internal.credentials \
    --username analytics-admin \
    --role internal_admin
```

Apply either file to SQLite through an ephemeral container:

```console
docker compose run --rm --no-deps \
  --env-from-file .env.analytics.acme.credentials \
  analytics-api \
  python -m analytics_service.users sync-credentials
```

Sync creates the account if absent. For an existing account it verifies the
stored role/company binding, hashes the new password, and revokes all prior
sessions. It refuses to change an existing account's role or tenant. Changing
the file alone does not change the live password; run sync explicitly. Move
the password into the approved password manager without printing or sending it
through chat. To rotate again, rerun `generate-credentials` with the same
arguments plus `--replace`, then run `sync-credentials`.

Production uses two independent, host-only browser cookies:

- `__Host-querix_company_analytics` for `company_user` accounts;
- `__Host-querix_internal_analytics` for `internal_admin` accounts.

Both cookies are `Secure`, `HttpOnly`, `SameSite=Lax`, and `Path=/`, with no
`Domain` attribute. Company routes read only the company cookie. Admin routes
read only the internal cookie. Logout revokes and deletes only the selected
portal session, so both portals can remain signed in in one browser profile.

For local plain HTTP, set `ANALYTICS_SESSION_COOKIE_SECURE=false` and override
both role-specific cookie names with distinct names that do not use the
`__Host-` prefix. Browsers require every `__Host-` cookie to be Secure.

Login returns a cryptographically random opaque identifier. SQLite stores only
its SHA-256 digest together with portal type, role, company binding, creation,
last activity, idle expiration, absolute expiration, and revocation state.
Passwords use salted scrypt hashes. Five failed logins lock the account for 15
minutes by default. Password changes and account disablement revoke active
sessions. The search Redis adapter is deliberately not used for authentication:
it is a fail-open cache, while analytics authentication must fail closed.

Default session policies are configurable and enforced server-side:

| Portal | Idle timeout | Absolute timeout |
|---|---:|---:|
| Company | 24 hours | 7 days |
| Internal | 8 hours | 12 hours |

Authenticated activity slides the idle expiration and refreshes the matching
cookie, but never moves the effective expiration beyond the absolute limit.
Login and `/me` return that effective expiration.

Browser login example:

```console
curl -c /tmp/querix-analytics-cookies \
  -H "Content-Type: application/json" \
  -d '{"username":"acme-owner","password":"your password"}' \
  http://127.0.0.1:8010/api/v1/analytics/company/auth/login

curl -b /tmp/querix-analytics-cookies \
  http://127.0.0.1:8010/api/v1/acme/analytics/dashboard
```

Internal users use the independent internal login endpoint, then call one
company-scoped internal route:

```console
curl -c /tmp/querix-internal-analytics-cookies \
  -H "Content-Type: application/json" \
  -d '{"username":"analytics-admin","password":"your password"}' \
  http://127.0.0.1:8010/api/v1/analytics/internal/auth/login

curl -b /tmp/querix-internal-analytics-cookies \
  http://127.0.0.1:8010/api/v1/admin/analytics/acme/dashboard
```

## Authentication rollout order

1. Deploy the additive backend endpoints and role-specific cookies.
2. Verify the deprecated shared endpoints still support the deployed frontend.
3. Update the frontend company portal to use `/company/auth/*` and the internal
   portal to use `/internal/auth/*`.
4. Test both sessions concurrently in one browser profile, including isolated
   logout and cross-tenant denial.
5. After the frontend rollout is stable, deprecate operational use of the
   shared cookie and remove the legacy endpoints only in a separate approved
   change.

Do not remove the shared endpoints as part of the additive backend rollout.

## Tenant SQL configuration

Analytics-enabled tenant YAML files define normalized source tables:

```yaml
analytics:
  enabled: true
  endpoint_slug: acme
  api_key_envs:
    - ACME_ANALYTICS_API_KEY
  history_days: 90
  tables:
    search_history: semantic_search_history
    api_usage: semantic_search_api_usage
    ads: ads
    users: users
    categories: categories
    sub_categories: sub_categories
    states: states
    location: location
    attributes: attributes
    attribute_values: attribute_values
    ads_attributes: ads_attributes
  telemetry:
    use_company_database: true
```

For a company with different source column names, add canonical-to-source
mappings:

```yaml
analytics:
  columns:
    ads:
      id: product_id
      user_id: seller_id
      category_id: subcategory_id
      title: product_name
      rental_fee: price
      created_at: listed_at
```

The SQL loader selects only the columns used by the analytics contract.
MySQL and PostgreSQL company sources are supported. Source credentials should
be read-only because the snapshot builder does not modify company tables.
`history_days` limits query history and API telemetry at the SQL source; the
catalogue and user tables remain available for longer market trends.

To move API telemetry to an internal database:

```yaml
analytics:
  telemetry:
    use_company_database: false
    database:
      backend: postgres
      host_env: ACME_ANALYTICS_HOST
      port_env: ACME_ANALYTICS_PORT
      database_env: ACME_ANALYTICS_DATABASE
      user_env: ACME_ANALYTICS_USER
      password_env: ACME_ANALYTICS_PASSWORD
      schema: public
      tls:
        mode: verify-full
        ca_file_env: ACME_ANALYTICS_TLS_CA_FILE
```

## Local verification

Build the standalone image:

```bash
docker compose build analytics-api
```

Test with the existing standalone CSV export:

```bash
docker compose run --rm --no-deps \
  -v /absolute/path/to/Analytrics:/analytics-data:ro \
  -e ANALYTICS_SNAPSHOT_DB_PATH=/tmp/analytics-test.sqlite3 \
  analytics-api \
  python -m analytics_service.refresh \
    --company acme \
    --csv-data-dir /analytics-data
```

Run against the configured company SQL database and publish to persistent
storage:

```bash
docker compose run --rm --no-deps analytics-api \
  python -m analytics_service.refresh --company acme
```

Start the API:

```bash
docker compose up -d --no-deps analytics-api
curl -fsS http://127.0.0.1:8010/api/v1/ready | jq
```

The service reports `not_ready` until every configured analytics company has a
completed snapshot.

## Snapshot backup

`scripts/backup_production.sh` backs up every SQLite database below
`storage/` through SQLite's online backup API, the pgvector database through
`pg_dump`, and the index-generation control files required to select the active
search slot after recovery. The company MySQL source remains under company/ETL
backup ownership and is intentionally excluded from this backend backup. The
completed backup includes checksums for the pgvector and SQLite artifacts.
