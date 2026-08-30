# Querix Analytics API handoff

Last verified: 21 August 2026
Service: separate Querix analytics product  
Production/test base URL: `https://api.querix.co`  
Local analytics base URL: `http://127.0.0.1:8010`

## 1. Product and tenant model

Analytics is a separate Docker image and process named `analytics-api`. The
existing semantic-search API, its routes, image, and tenant search API keys are not
modified or reused.

Every snapshot, login, API key, dashboard, and query record is company-scoped:

- A company user is bound to exactly one `company_id`.
- A company API key is valid only on that company's endpoint slug.
- An internal administrator first lists companies and then opens one company's
  dashboard or query page.
- There is no cross-company analytics overview or combined calculation.
- External dashboards never contain API/provider/model/token/attempt details.
- Internal dashboards may include those fields, but still for one selected
  company only.

The dashboard frontend can therefore use:

1. one shared analytics login screen;
2. a company user redirect directly to its own dashboard;
3. an internal company selector followed by a one-company dashboard page.

## 2. Data and refresh behavior

The snapshot builder reads each configured company's SQL database with
normalized table/column mappings. Company business data and search history
come from the company database. Internal API telemetry may use the same
database today or a separate Querix database later.

Every two hours at `:30` Asia/Kolkata, the independent analytics timer:

1. flushes pending search telemetry;
2. reads SQL and atomically publishes a new analytics snapshot;
3. prunes expired analytics sessions.

Search ingestion and warm-start remain on their independent daily 03:00 timer.

Dashboard HTTP requests only read the local SQLite snapshot. They do not query
the company database or recalculate pandas reports. If refresh fails, the last
completed snapshot remains live.

Current production history window: 90 days for individual queries and API
telemetry. Catalogue and user datasets remain available for business trends.

## 3. Authentication

### Browser/frontend authentication

Use the role-specific username/password endpoint for each portal. Successful
logins set independent opaque host-only cookies:

- company: `__Host-querix_company_analytics`;
- internal: `__Host-querix_internal_analytics`.

Both are `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, and omit `Domain`.
Company sessions use a 24-hour idle and seven-day absolute timeout. Internal
sessions use an eight-hour idle and twelve-hour absolute timeout. Activity
slides the idle expiration without exceeding the absolute expiration.

Passwords are stored as salted scrypt hashes. Five failed login attempts lock
the account for 15 minutes by default. Password changes and account disablement
revoke active sessions. Session identifiers are never stored directly; the
SQLite authentication store keeps only their SHA-256 digests and fails closed
when it is unavailable.

### Server-to-server authentication

Send the company-specific analytics key:

```http
X-API-Key: <company analytics key>
```

For each tenant this value is stored only as its tenant-specific analytics key
in the server's secret store. It is separate from the tenant's search API key.
The key does **not** change on a deploy, image rebuild, container recreation, or
server restart. It changes only when an operator deliberately rotates it using
the approved secret-management workflow.

Do not put this key in browser JavaScript, source control, screenshots, tickets,
or chat. A browser dashboard should use the login cookie.

Do not use shell commands that print credentials or keys. Provision and rotate
analytics passwords through the hidden-prompt user-management command and
store operator credentials only in the approved password manager.

## 4. Endpoint summary

Health, business, and authentication routes are identical locally and in
production. An external gateway must preserve these paths if it fronts the
analytics service.

| Method | Production path | Authentication | Purpose |
|---|---|---|---|
| `GET` | `/api/v1/live` | none | Process liveness |
| `GET` | `/api/v1/ready` | none | Snapshot readiness |
| `POST` | `/api/v1/{company}/analytics/auth/login` | username/password | Start a tenant-bound company session |
| `GET` | `/api/v1/analytics/company/auth/me` | company cookie | Read company principal |
| `POST` | `/api/v1/analytics/company/auth/logout` | company cookie | Revoke company session only |
| `POST` | `/api/v1/analytics/internal/auth/login` | username/password | Start internal session |
| `GET` | `/api/v1/analytics/internal/auth/me` | internal cookie | Read internal principal |
| `POST` | `/api/v1/analytics/internal/auth/logout` | internal cookie | Revoke internal session only |
| `GET` | `/api/v1/{company}/analytics/dashboard` | company cookie or API key | Company-safe dashboard |
| `GET` | `/api/v1/{company}/analytics/queries` | company cookie or API key | Company-safe search history |
| `GET` | `/api/v1/{company}/analytics/query-facets` | company cookie or API key | Versioned Query Explorer facets |
| `GET` | `/api/v1/{company}/analytics/status` | company cookie or API key | Company snapshot status |
| `GET` | `/api/v1/admin/analytics/companies` | internal cookie | Company/snapshot inventory |
| `GET` | `/api/v1/admin/analytics/{company}/dashboard` | internal cookie | One company's internal dashboard |
| `GET` | `/api/v1/admin/analytics/{company}/queries` | internal cookie | One company's internal query history |
| `GET` | `/api/v1/admin/analytics/{company}/query-facets` | internal cookie | One company's internal Query Explorer facets |

Local liveness and readiness are `/api/v1/live` and `/api/v1/ready`.

There is intentionally no `/api/v1/admin/analytics/overview`; it returns 404.

## 5. Requests and responses

### `GET /api/v1/live`

Response `200`:

```json
{"status": "ok"}
```

### `GET /api/v1/ready`

Response `200` when every configured company has a completed snapshot and the
snapshot/authentication database is readable and writable:

```json
{
  "status": "ok",
  "configured_companies": 1,
  "companies_with_snapshots": 1,
  "refresh_schedule": "every 2 hours at :30 Asia/Kolkata"
}
```

Response `503` uses the same shape with `"status": "not_ready"`.

### Role-specific login

Company: `POST /api/v1/{company}/analytics/auth/login`

Internal: `POST /api/v1/analytics/internal/auth/login`

Request:

```json
{
  "username": "acme-analytics",
  "password": "<password>"
}
```

The company endpoint resolves the requested tenant before password verification
and permits only a `company_user` bound to that company. The internal endpoint permits
only `internal_admin`. A wrong-role credential receives the same generic `401`
as any other invalid credential. Company identity always comes from the bound
account.

Company response `200`:

```json
{
  "user": {
    "username": "acme-analytics",
    "role": "company_user",
    "company_id": "acme"
  },
  "expires_at": "2026-07-30T21:00:00+00:00"
}
```

Internal response uses `"role": "internal_admin"` and
`"company_id": null`. The session token is returned only in `Set-Cookie`.

Response `401`:

```json
{"detail": "Invalid username or password."}
```

### Role-specific `/me`

Company: `GET /api/v1/analytics/company/auth/me`

Internal: `GET /api/v1/analytics/internal/auth/me`

Request: session cookie only.

Response `200` has the same `user` and `expires_at` shape as login.

Response `401`:

```json
{"detail": "Authentication required."}
```

Each endpoint reads only its matching cookie. The two cookies may coexist.

### Role-specific logout

Company: `POST /api/v1/analytics/company/auth/logout`

Internal: `POST /api/v1/analytics/internal/auth/logout`

Request: session cookie.

Response `200`:

```json
{"logged_out": true}
```

The server revokes and expires only that portal's session. Logout is
idempotent.

### Authentication boundary

Shared `/api/v1/analytics/auth/*` routes are unavailable. Company login is
tenant-bound, while internal login remains role-bound to `internal_admin`.

### `GET /api/v1/{company}/analytics/dashboard`

Request authentication: company session cookie or `X-API-Key`.

Response `200`:

```json
{
  "metadata": {
    "schema_version": "2.0",
    "company_id": "acme",
    "generated_at": "2026-07-30T17:00:00+00:00",
    "refresh_schedule": "every 2 hours at :30 Asia/Kolkata",
    "source_rows": {
      "search_history": 365,
      "api_usage": 365,
      "ads": 250117,
      "users": 340601
    },
    "audience": "company",
    "modules": [
      "search_intelligence",
      "individual_queries",
      "deep_analytics",
      "market_intelligence"
    ],
    "metric_counts": {
      "search_intelligence": 17,
      "deep_analytics": 19,
      "market_intelligence": 9
    },
    "individual_query_count": 365
  },
  "search_intelligence": {
    "q1_category_distribution": {
      "title": "Searches by Category",
      "chart_type": "bar",
      "labels": ["Vehicles"],
      "values": [42]
    }
  },
  "deep_analytics": {},
  "market_intelligence": {},
  "snapshot": {
    "generated_at": "2026-07-30T17:00:00+00:00",
    "source_watermark": "2026-07-29T23:59:59+00:00",
    "source_rows": {},
    "refresh_schedule": "every 2 hours at :30 Asia/Kolkata"
  }
}
```

The module objects contain the configured metric IDs. Metric payloads commonly
contain `title`, `chart_type`, and chart-specific values such as `labels`,
`values`, `series`, `avg`, `total`, or `percentage`.

The company dashboard never returns `api_performance`.

### `GET /api/v1/admin/analytics/{company}/dashboard`

Request authentication: internal-admin session cookie only.

The response uses the same dashboard envelope with `"audience": "internal"`,
but its module list is only `individual_queries` and `api_performance`:

```json
{
  "api_performance": {
    "q21_success_rate": {
      "title": "API Success Rate",
      "chart_type": "stat",
      "success_rate": 99.1
    }
  }
}
```

It is still a dashboard for one named company, not a global aggregate.

### Individual-query endpoints

Company:

```text
GET /api/v1/{company}/analytics/queries
```

Internal:

```text
GET /api/v1/admin/analytics/{company}/queries
```

Both accept:

| Parameter | Type/rule | Meaning |
|---|---|---|
| `limit` | integer, 1–200; default 50 | Page size |
| `cursor` | opaque string | Next-page cursor from the prior response |
| `query` | string, max 1000 | Case-insensitive query-text search |
| `outcome` | `fulfilled`, `zero_result`, `failure`, or `telemetry_missing` | Search outcome |
| `category` | string, max 191 | Category filter |
| `execution_path` | string, max 128; internal only | Exact, case-insensitive execution-path filter |
| `language` | string, max 64 | Language filter |
| `include_facets` | boolean; default `true` | Set `false` after the first page to omit repeated snapshot-wide facets |
| `from` | ISO-8601 datetime | Inclusive lower time boundary |
| `to` | ISO-8601 datetime | Inclusive upper time boundary |

The stable cursor is based on descending `(created_at, request_id)`. Do not
construct or edit it in the frontend.

The dedicated company and internal `query-facets` endpoints return the same
facet choices with their immutable `snapshot_version`. They support ETag
revalidation; send the prior `ETag` in `If-None-Match` and retain cached facets
when the response is `304`. Facet responses use
`Cache-Control: private, max-age=300, must-revalidate`.

Company response `200`:

```json
{
  "company_id": "acme",
  "items": [
    {
      "search_id": 123,
      "request_id": "request-uuid",
      "query": "camera rent chennai",
      "normalized_query": "camera rent chennai",
      "request_kind": "text_search",
      "created_at": "2026-07-29T11:00:00+00:00",
      "word_count": 3,
      "categories": ["Electronics"],
      "brands": [],
      "locations": ["Chennai"],
      "language": "English",
      "rental_duration": null,
      "flags": {},
      "outcome": "fulfilled",
      "search": {
        "status": "success",
        "result_count": 10,
        "total_results": 20
      }
    }
  ],
  "returned": 1,
  "has_more": true,
  "next_cursor": "<opaque cursor>"
}
```

`request_kind` is `text_search`, `filtered_browse`, or `catalogue_browse`.
Filter-only requests keep an empty `normalized_query`, while `query` contains a
readable analytics label such as `Filtered browse: Camera, Chennai`. Company
text-search fulfillment and zero-result metrics exclude both browse kinds;
internal API request totals continue to include them.

The internal response uses the same envelope, but each item additionally
contains operational `performance`, `token_usage`, `api`, and `attempts`
projections. `performance.total_server_duration_ms` is the authoritative
server processing duration. `performance.cache` reports nullable plan/result
cache hits, and `performance.stages_ms` reports allowlisted planning, model,
retrieval, reranking, database/hydration, response mapping, usage-recording,
session-storage, and recent-search timings. Stage and provider timings can overlap and must not
be summed. `null` means that stage was unavailable or was written before
detailed telemetry existed; it is distinct from a measured `0`. These fields
are intentionally removed from the company projection.

### `GET /api/v1/{company}/analytics/status`

Response `200`:

```json
{
  "company_id": "acme",
  "has_snapshot": true,
  "snapshot": {
    "generated_at": "2026-07-30T17:00:00+00:00",
    "source_watermark": "2026-07-29T23:59:59+00:00",
    "source_rows": {
      "search_history": 365,
      "api_usage": 365,
      "ads": 250117,
      "users": 340601
    }
  },
  "refresh_schedule": "every 2 hours at :30 Asia/Kolkata"
}
```

### `GET /api/v1/admin/analytics/companies`

Request authentication: internal-admin session cookie.

Response `200`:

```json
{
  "companies": [
    {
      "company_id": "acme",
      "endpoint_slug": "acme",
      "has_snapshot": true,
      "snapshot": {},
      "latest_run": {},
      "refresh_schedule": "every 2 hours at :30 Asia/Kolkata"
    }
  ],
  "refresh_schedule": "every 2 hours at :30 Asia/Kolkata"
}
```

This is an inventory for the company selector; it is not a combined analytics
response.

## 6. Curl test commands

Set non-secret variables:

```bash
export ANALYTICS_BASE_URL="https://api.querix.co"
export ANALYTICS_COMPANY="acme"
```

Load the server-to-server key into the shell without printing it:

```bash
read -s ACME_ANALYTICS_API_KEY
export ACME_ANALYTICS_API_KEY
```

Liveness/readiness:

```bash
curl -fsS "$ANALYTICS_BASE_URL/api/v1/live" | jq
curl -fsS "$ANALYTICS_BASE_URL/api/v1/ready" | jq
```

Company server-to-server calls:

```bash
curl -fsS \
  -H "X-API-Key: $ACME_ANALYTICS_API_KEY" \
  "$ANALYTICS_BASE_URL/api/v1/$ANALYTICS_COMPANY/analytics/dashboard" | jq

curl -fsS \
  -H "X-API-Key: $ACME_ANALYTICS_API_KEY" \
  "$ANALYTICS_BASE_URL/api/v1/$ANALYTICS_COMPANY/analytics/status" | jq

curl -fsS -G \
  -H "X-API-Key: $ACME_ANALYTICS_API_KEY" \
  --data-urlencode "outcome=zero_result" \
  --data-urlencode "limit=50" \
  "$ANALYTICS_BASE_URL/api/v1/$ANALYTICS_COMPANY/analytics/queries" | jq
```

Company browser-session flow:

```bash
export ANALYTICS_COOKIE_JAR="/tmp/querix-company-analytics.cookies"

curl -fsS -c "$ANALYTICS_COOKIE_JAR" \
  -H "Content-Type: application/json" \
  -d '{"username":"acme-analytics","password":"<password>"}' \
  "$ANALYTICS_BASE_URL/api/v1/$ANALYTICS_COMPANY/analytics/auth/login" | jq

curl -fsS -b "$ANALYTICS_COOKIE_JAR" \
  "$ANALYTICS_BASE_URL/api/v1/analytics/company/auth/me" | jq

curl -fsS -b "$ANALYTICS_COOKIE_JAR" \
  "$ANALYTICS_BASE_URL/api/v1/acme/analytics/dashboard" | jq

curl -fsS -b "$ANALYTICS_COOKIE_JAR" -X POST \
  "$ANALYTICS_BASE_URL/api/v1/analytics/company/auth/logout" | jq
```

Internal browser-session flow:

```bash
export ANALYTICS_ADMIN_COOKIE_JAR="/tmp/querix-internal-analytics.cookies"

curl -fsS -c "$ANALYTICS_ADMIN_COOKIE_JAR" \
  -H "Content-Type: application/json" \
  -d '{"username":"analytics-admin","password":"<password>"}' \
  "$ANALYTICS_BASE_URL/api/v1/analytics/internal/auth/login" | jq

curl -fsS -b "$ANALYTICS_ADMIN_COOKIE_JAR" \
  "$ANALYTICS_BASE_URL/api/v1/analytics/internal/auth/me" | jq

curl -fsS -b "$ANALYTICS_ADMIN_COOKIE_JAR" \
  "$ANALYTICS_BASE_URL/api/v1/admin/analytics/companies" | jq

curl -fsS -b "$ANALYTICS_ADMIN_COOKIE_JAR" \
  "$ANALYTICS_BASE_URL/api/v1/admin/analytics/acme/dashboard" | jq

curl -fsS -b "$ANALYTICS_ADMIN_COOKIE_JAR" \
  "$ANALYTICS_BASE_URL/api/v1/admin/analytics/acme/queries?limit=50" | jq

curl -fsS -b "$ANALYTICS_ADMIN_COOKIE_JAR" -X POST \
  "$ANALYTICS_BASE_URL/api/v1/analytics/internal/auth/logout" | jq
```

Expected security checks:

```bash
# Existing search-product key must return 403, not 200.
curl -sS -o /dev/null -w "%{http_code}\n" \
  -H "X-API-Key: $ACME_API_KEY" \
  "$ANALYTICS_BASE_URL/api/v1/acme/analytics/dashboard"

# Company session must return 403 for another company.
curl -sS -o /dev/null -w "%{http_code}\n" \
  -b "$ANALYTICS_COOKIE_JAR" \
  "$ANALYTICS_BASE_URL/api/v1/another-company/analytics/dashboard"
```

## 7. Per-company dashboard configuration

Each tenant YAML can select company-facing business questions and internal
operational API questions independently:

```yaml
company:
  id: acme

analytics:
  enabled: true
  endpoint_slug: acme
  api_key_envs:
    - ACME_ANALYTICS_API_KEY
  history_days: 90

  metrics:
    company:
      search_intelligence:
        - q1_category_distribution
        - q3_trending_terms
        - q7_zero_results
      deep_analytics:
        - q47_supply_by_category
        - q50_low_supply_categories
      market_intelligence:
        - q75_marketplace_overview
        - q78_pricing_benchmark

    internal:
      api_performance:
        - q21_success_rate
        - q23_latency_stats
        - q29_provider_reliability
        - q33_failure_reasons
```

Rules:

- Omitted modules use the safe curated defaults.
- An empty list hides that module for that audience.
- Internal dashboards contain only `individual_queries` and
  `api_performance`; company business modules are rejected in an internal
  profile.
- `api_performance` is accepted only under `internal`.
- Unknown modules, metric names, duplicate metrics, and accidental string
  values fail configuration validation.
- Adding a tenant creates a separate company snapshot and endpoint; it never
  merges rows with another tenant.

The current safe default contains 45 company metrics:

- Search Intelligence: 17
- Deep Analytics: 19
- Market Intelligence: 9

Internal adds 16 API Performance metrics. The remaining exploratory reports are
available for deliberate per-company selection, but should be enabled only
when that company's data semantics and business requirement justify them.

## 8. Adding another company

1. Create `configs/tenants/<company>.yaml`.
2. Configure the company SQL connection using environment-variable names.
3. Use a read-only SQL user where possible.
4. Configure normalized table and optional column mappings.
5. Create a unique `<COMPANY>_ANALYTICS_API_KEY` in `.env.keys`.
6. Select the external/internal metric profiles.
7. Run the analytics source migration if the search-history tables are absent.
8. Build and run one refresh.
9. Create a `company_user` bound to that company.
10. Verify that its key/session cannot access any other company.

Example refresh:

```bash
docker compose run --rm --no-deps analytics-api \
  python -m analytics_service.refresh --company acme
```

## 9. Key rotation

Rotate during a coordinated client change. Generate at least 256 random bits,
update only the analytics key variable, recreate only `analytics-api`, verify
the new key, and then remove the old client secret.

Because a tenant configuration normally names one analytics key variable,
rotation is an atomic cutover. For zero-downtime overlap, temporarily
add a second key environment variable to `api_key_envs`, deploy, migrate
clients, then remove the old variable in a later deployment.

Recreate only analytics:

```bash
cd /root/Peronsal_rag
docker compose build analytics-api
docker compose up -d --no-deps --force-recreate analytics-api
curl -fsS http://127.0.0.1:8010/api/v1/ready | jq
```

Do not rebuild or restart `api`, `pgvector`, `redis`, or Ollama for an
analytics-only key rotation.

## 10. Errors and security headers

Common responses:

| Status | Meaning |
|---|---|
| `401` | Missing/expired session or missing API key; invalid login |
| `403` | Wrong company key, wrong-company session, or non-admin on internal route |
| `404` | Unknown company endpoint or route |
| `422` | Invalid query filter, date, limit, cursor, or request body |
| `503` | No completed snapshot is available |

Analytics responses set:

```text
Cache-Control: private, no-store
Pragma: no-cache
X-Content-Type-Options: nosniff
```

Production must terminate TLS. CORS must list exact frontend origins; wildcard
CORS is rejected while credentialed sessions are enabled.

## 11. Performance and operational isolation

The analytics change does not add pandas, analytics calculations, SQL scans,
or new middleware to the semantic-search image or search HTTP path.

Isolation controls:

- separate `Dockerfile.analytics`;
- separate `analytics-api` container and port 8010;
- separate process memory limit of 1 GiB;
- dashboard/query requests read a local snapshot;
- two-hour calculations run outside user requests;
- analytics-only deployments recreate only `analytics-api`.

Realistic caveat: each refresh must read source SQL. That creates a brief
database workload every two hours even though it does not affect search
application code or restart the search API. As data grows, use a database
replica and incremental watermarks/indexes to isolate that read load further.

Production verification should confirm that the tenant database uses verified
TLS when it is remote. A database that cannot provide TLS must be upgraded at
the database layer before the connection is treated as fully hardened.

Current known host considerations:

- The existing search concurrency setting was left unchanged because analytics
  is a separate product and this deployment must not retune search behavior.

## 12. Operator checks

```bash
cd /root/Peronsal_rag

docker compose ps
docker compose logs --tail=100 analytics-api
curl -fsS http://127.0.0.1:8010/api/v1/live | jq
curl -fsS http://127.0.0.1:8010/api/v1/ready | jq

# Manual refresh for one company.
docker compose run --rm --no-deps analytics-api \
  python -m analytics_service.refresh --company acme

# Session cleanup.
docker compose run --rm --no-deps analytics-api \
  python -m analytics_service.users prune-sessions

# List analytics users without exposing password hashes.
docker compose run --rm analytics-api \
  python -m analytics_service.users list
```

Backup coverage: `storage/analytics/snapshots.sqlite3` is included in the
existing production SQLite backup workflow.
