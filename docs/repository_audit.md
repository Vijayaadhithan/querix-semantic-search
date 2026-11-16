# Repository-wide maintainability audit

Review date: 2026-11-16. Scope: the complete checkout, including all
177 source modules, 18 Python operator scripts, 38 test modules, six
shell operator scripts, documentation links, and build/deployment configuration.
Generated data, secrets, databases, caches, and model assets are outside source
cleanup and were not deleted or migrated.

## Method and results

Every Python module was included in syntax, import/reference, duplicate-body,
unused-symbol, size, lint, and formatting scans. Large modules received targeted
responsibility extraction. Existing tests and deterministic comparisons cover
behavior across the refactored boundaries; this is a maintainability review,
not a claim that static analysis can prove the absence of every possible bug.

- Removed the unused analytics count method, default-adapter alias, and obsolete
  dashboard-output and gibberish-pattern configuration.
- Centralized analytics SQLite connection policy, provider HTTP transport and
  metrics state, provider-operation grouping, and incremental ingestion batching.
- Split both API factories into focused route groups and request/access services.
- Separated tenant models, database/vector configuration, loading, and registries.
- Separated query normalization, constraints, matching, fast paths, and enrichment.
- Separated Gainr card projection, SQL predicates, relation hydration, page
  execution, and filter diagnostics.
- Separated pgvector filter construction and query strategies from collection
  lifecycle and CRUD.
- Separated snapshot schema, query pagination/reading, serialization, password
  policy, and account/session schema from analytics storage orchestration.
- Replaced the four monolithic analytics reports with named metric groups and a
  typed provider-attempt summary. All 94 report outputs match the original code
  on the deterministic analytics fixture.
- Separated retrieval-evaluation snapshots, relevance labels, and fixed-candidate
  evaluation from the CLI; separated workload environment selection from file I/O.
- Fixed BM25 connection leaks on model-resolution, source-read, embedding, and
  rebuild failures. Four regression cases verify that failed ingestion releases
  its real SQLite connection. Existing incremental-ingestion behavior tests pass.

## Remaining large cohesive modules

These were reviewed and retained intentionally, rather than left as unreviewed
refactoring candidates:

| Component | Reason to retain its remaining orchestration |
|---|---|
| Search request service | Owns capacity/coalescing, sessions, usage submission, and request lifetime; pure timeline and telemetry calculations are extracted. |
| Search engine | Owns retrieval-to-ranking orchestration and shared state; related-result expansion is extracted and replaceable I/O boundaries remain explicit. |
| Gainr compatibility service | Owns request coordination, scoped recent searches, cache lifetime, and its diagnostics worker; card, SQL, execution, and diagnostic calculations are extracted. |
| Analytics authentication store | Account and session changes share transaction/locking policy; password mechanics and schema migrations are extracted. |
| Pgvector collection | CRUD and index lifecycle implement one collection interface; predicates and query strategies are extracted. |
| Tenant loader and schema migrations | Remaining branches enforce one ordered validation or migration contract. Moving individual conditionals would obscure failure order. |
| Business insights and ranking | Remaining functions implement a single policy calculation; report families, independent mapping, and vocabulary have their own modules. |
| Database provisioning and deployment scripts | Explicit operator workflows and grants stay together; syntax and configuration checks do not execute production operations. |
| Large test modules | Cohesive component regressions retain their fixtures and scenarios; new resource tests live in the matching package. |
| Rule/configuration modules | Primarily declarative vocabulary and defaults, so line count is not code complexity. |

The duplicate-body scan's remaining matches are deliberate: provider constructors
with different public default arguments, tiny queue-flush entry points on two
independent writer implementations, and protocol method placeholders. Framework
callbacks, validators, and documented compatibility/test exports remain intact.

## Validation

- Full suite: 501 tests passed, including seven new connection-lifecycle cases
  across analytics storage and ingestion.
- Ruff lint and formatting, Python compilation, Markdown targets, service-env
  validation, Docker Compose validation, and all six shell syntax checks pass.
- Search and analytics OpenAPI documents match the captured original documents.
- Twenty analytics HTTP response/status/cache-header comparisons match.
- Four hundred telemetry/timeline comparisons match.
- All 94 complete report outputs, loaded tenant configuration fields, and workload
  environment selections match their original implementations on local fixtures.
- All 1,152 project-local import contracts resolve across source, scripts, and tests.
- An isolated import using only the packages copied by the analytics Docker image
  passes, preserving its separation from the search runtime.
- All seven extracted MySQL schema-management functions retain identical ASTs.
- Dependency manifests are unchanged. The existing environment was used;
  `uv lock --check` and `uv sync` were not rerun because `uv` is unavailable here.
- The suite reports the existing FastAPI/Starlette test-client deprecation warning.

No production deployment, ingestion, schema migration, or index rebuild was run.

## Large-module changes

| Original module | Before | After |
|---|---:|---:|
| `src/analytics_service/api.py` | 1084 | 115 |
| `src/analytics_service/auth.py` | 1046 | 777 |
| `src/analytics_service/store.py` | 1046 | 655 |
| `src/api/__init__.py` | 792 | 117 |
| `src/api/service.py` | 1177 | 819 |
| `src/cli/evaluate_retrieval.py` | 739 | 279 |
| `src/core/tenant_config.py` | 999 | 234 |
| `src/providers/gemini.py` | 913 | 399 |
| `src/search/engine.py` | 884 | 778 |
| `src/search/planner.py` | 782 | 178 |
| `src/search/planner_catalog.py` | 1016 | 364 |
| `src/storage/pgvector.py` | 1012 | 751 |
| `src/storage/search_analytics.py` | 1015 | 555 |
| `src/tenants/gainr/compatibility.py` | 1189 | 838 |
| `src/tenants/gainr/policy.py` | 845 | 489 |
| `src/tenants/gainr/repository.py` | 1054 | 689 |
| `src/verticals/marketplace/analytics/dashboard_filters.py` | 856 | 329 |

## Complete Python inventory

"Retained" means the module was included in the structural/reference checks and
keeps its existing responsibility. "Refactored" and "Added" identify this change's
implementation and extracted components. Function size includes nested handlers
inside route registration functions and therefore is an audit signal only.

| Module | Review result | Lines | Largest function |
|---|---|---:|---:|
| [scripts/analytics_retry.py](../scripts/analytics_retry.py) | Retained | 112 | 32 |
| [scripts/benchmark_analytics_memory.py](../scripts/benchmark_analytics_memory.py) | Retained | 115 | 36 |
| [scripts/check_markdown.py](../scripts/check_markdown.py) | Retained | 112 | 31 |
| [scripts/check_search_analytics_schema.py](../scripts/check_search_analytics_schema.py) | Retained | 34 | 14 |
| [scripts/doctor.py](../scripts/doctor.py) | Retained | 337 | 71 |
| [scripts/ensure_service_credentials.py](../scripts/ensure_service_credentials.py) | Retained | 175 | 80 |
| [scripts/flush_search_analytics.py](../scripts/flush_search_analytics.py) | Retained | 56 | 32 |
| [scripts/generate_company_api_key.py](../scripts/generate_company_api_key.py) | Retained | 51 | 31 |
| [scripts/generate_semantic_cases.py](../scripts/generate_semantic_cases.py) | Retained | 275 | 63 |
| [scripts/load_test.py](../scripts/load_test.py) | Retained | 203 | 126 |
| [scripts/migrate_runtime_storage.py](../scripts/migrate_runtime_storage.py) | Retained | 74 | 21 |
| [scripts/migrate_search_analytics.py](../scripts/migrate_search_analytics.py) | Retained | 63 | 39 |
| [scripts/optimize_pgvector_filters.py](../scripts/optimize_pgvector_filters.py) | Retained | 44 | 28 |
| [scripts/provision_database_roles.py](../scripts/provision_database_roles.py) | Retained | 422 | 171 |
| [scripts/render_service_env.py](../scripts/render_service_env.py) | Refactored | 215 | 47 |
| [scripts/run_shadow_ingestion.py](../scripts/run_shadow_ingestion.py) | Retained | 40 | 19 |
| [scripts/warm_hnsw.py](../scripts/warm_hnsw.py) | Retained | 90 | 44 |
| [scripts/warm_search_paths.py](../scripts/warm_search_paths.py) | Retained | 130 | 43 |
| [src/analytics_service/__init__.py](../src/analytics_service/__init__.py) | Retained | 5 | 0 |
| [src/analytics_service/__main__.py](../src/analytics_service/__main__.py) | Retained | 20 | 8 |
| [src/analytics_service/access.py](../src/analytics_service/access.py) | Added | 281 | 46 |
| [src/analytics_service/activity_records.py](../src/analytics_service/activity_records.py) | Added | 90 | 84 |
| [src/analytics_service/adapters.py](../src/analytics_service/adapters.py) | Refactored | 102 | 13 |
| [src/analytics_service/adapters_base.py](../src/analytics_service/adapters_base.py) | Retained | 80 | 16 |
| [src/analytics_service/api.py](../src/analytics_service/api.py) | Refactored | 115 | 94 |
| [src/analytics_service/auth.py](../src/analytics_service/auth.py) | Refactored | 777 | 178 |
| [src/analytics_service/auth_schema.py](../src/analytics_service/auth_schema.py) | Added | 223 | 71 |
| [src/analytics_service/config.py](../src/analytics_service/config.py) | Retained | 554 | 126 |
| [src/analytics_service/contracts.py](../src/analytics_service/contracts.py) | Retained | 67 | 31 |
| [src/analytics_service/dashboard.py](../src/analytics_service/dashboard.py) | Added | 214 | 59 |
| [src/analytics_service/dashboard_cache.py](../src/analytics_service/dashboard_cache.py) | Retained | 49 | 18 |
| [src/analytics_service/filters.py](../src/analytics_service/filters.py) | Retained | 26 | 0 |
| [src/analytics_service/mark_interrupted.py](../src/analytics_service/mark_interrupted.py) | Retained | 41 | 26 |
| [src/analytics_service/memory.py](../src/analytics_service/memory.py) | Retained | 37 | 25 |
| [src/analytics_service/metrics.py](../src/analytics_service/metrics.py) | Refactored | 88 | 39 |
| [src/analytics_service/passwords.py](../src/analytics_service/passwords.py) | Added | 60 | 20 |
| [src/analytics_service/prepared_views.py](../src/analytics_service/prepared_views.py) | Retained | 205 | 63 |
| [src/analytics_service/query_facets.py](../src/analytics_service/query_facets.py) | Retained | 58 | 50 |
| [src/analytics_service/query_pagination.py](../src/analytics_service/query_pagination.py) | Added | 89 | 27 |
| [src/analytics_service/query_reader.py](../src/analytics_service/query_reader.py) | Added | 197 | 181 |
| [src/analytics_service/refresh.py](../src/analytics_service/refresh.py) | Retained | 90 | 41 |
| [src/analytics_service/routes/__init__.py](../src/analytics_service/routes/__init__.py) | Added | 1 | 0 |
| [src/analytics_service/routes/auth.py](../src/analytics_service/routes/auth.py) | Added | 149 | 131 |
| [src/analytics_service/routes/company.py](../src/analytics_service/routes/company.py) | Added | 216 | 200 |
| [src/analytics_service/routes/internal.py](../src/analytics_service/routes/internal.py) | Added | 211 | 194 |
| [src/analytics_service/routes/system.py](../src/analytics_service/routes/system.py) | Added | 56 | 40 |
| [src/analytics_service/schedule.py](../src/analytics_service/schedule.py) | Retained | 1 | 0 |
| [src/analytics_service/serialization.py](../src/analytics_service/serialization.py) | Added | 24 | 7 |
| [src/analytics_service/service.py](../src/analytics_service/service.py) | Retained | 265 | 166 |
| [src/analytics_service/snapshot_schema.py](../src/analytics_service/snapshot_schema.py) | Added | 80 | 75 |
| [src/analytics_service/source.py](../src/analytics_service/source.py) | Retained | 407 | 53 |
| [src/analytics_service/source_schema.py](../src/analytics_service/source_schema.py) | Retained | 31 | 7 |
| [src/analytics_service/sqlite.py](../src/analytics_service/sqlite.py) | Added | 19 | 10 |
| [src/analytics_service/store.py](../src/analytics_service/store.py) | Refactored | 655 | 156 |
| [src/analytics_service/users.py](../src/analytics_service/users.py) | Retained | 324 | 111 |
| [src/api/__init__.py](../src/api/__init__.py) | Refactored | 117 | 45 |
| [src/api/__main__.py](../src/api/__main__.py) | Retained | 13 | 0 |
| [src/api/context.py](../src/api/context.py) | Added | 287 | 64 |
| [src/api/contracts.py](../src/api/contracts.py) | Retained | 245 | 15 |
| [src/api/lifecycle.py](../src/api/lifecycle.py) | Retained | 255 | 68 |
| [src/api/monitoring.py](../src/api/monitoring.py) | Added | 184 | 179 |
| [src/api/readiness.py](../src/api/readiness.py) | Retained | 81 | 46 |
| [src/api/routes/__init__.py](../src/api/routes/__init__.py) | Added | 1 | 0 |
| [src/api/routes/admin.py](../src/api/routes/admin.py) | Added | 218 | 205 |
| [src/api/routes/company.py](../src/api/routes/company.py) | Added | 188 | 174 |
| [src/api/routes/system.py](../src/api/routes/system.py) | Added | 66 | 53 |
| [src/api/service.py](../src/api/service.py) | Refactored | 819 | 145 |
| [src/api/telemetry.py](../src/api/telemetry.py) | Added | 218 | 71 |
| [src/api/tenants.py](../src/api/tenants.py) | Retained | 535 | 105 |
| [src/cli/__init__.py](../src/cli/__init__.py) | Retained | 1 | 0 |
| [src/cli/chat.py](../src/cli/chat.py) | Retained | 149 | 56 |
| [src/cli/evaluate_queries.py](../src/cli/evaluate_queries.py) | Retained | 137 | 70 |
| [src/cli/evaluate_retrieval.py](../src/cli/evaluate_retrieval.py) | Refactored | 279 | 192 |
| [src/cli/ingest.py](../src/cli/ingest.py) | Retained | 220 | 188 |
| [src/cli/retrieval_evaluation.py](../src/cli/retrieval_evaluation.py) | Added | 279 | 118 |
| [src/cli/retrieval_ground_truth.py](../src/cli/retrieval_ground_truth.py) | Added | 154 | 44 |
| [src/cli/retrieval_snapshots.py](../src/cli/retrieval_snapshots.py) | Added | 99 | 24 |
| [src/core/__init__.py](../src/core/__init__.py) | Retained | 1 | 0 |
| [src/core/rate_limit.py](../src/core/rate_limit.py) | Retained | 77 | 23 |
| [src/core/request_limit.py](../src/core/request_limit.py) | Retained | 110 | 20 |
| [src/core/settings.py](../src/core/settings.py) | Retained | 537 | 10 |
| [src/core/tenant_config.py](../src/core/tenant_config.py) | Refactored | 234 | 81 |
| [src/core/tenant_database.py](../src/core/tenant_database.py) | Added | 291 | 123 |
| [src/core/tenant_loader.py](../src/core/tenant_loader.py) | Added | 338 | 233 |
| [src/core/tenant_models.py](../src/core/tenant_models.py) | Added | 245 | 35 |
| [src/ingestion/__init__.py](../src/ingestion/__init__.py) | Retained | 1 | 0 |
| [src/ingestion/batch.py](../src/ingestion/batch.py) | Added | 149 | 97 |
| [src/ingestion/change_detection.py](../src/ingestion/change_detection.py) | Added | 83 | 49 |
| [src/ingestion/documents.py](../src/ingestion/documents.py) | Retained | 393 | 62 |
| [src/ingestion/generations.py](../src/ingestion/generations.py) | Retained | 323 | 159 |
| [src/ingestion/mapping.py](../src/ingestion/mapping.py) | Retained | 20 | 13 |
| [src/ingestion/service.py](../src/ingestion/service.py) | Refactored | 450 | 166 |
| [src/ingestion/state.py](../src/ingestion/state.py) | Retained | 72 | 12 |
| [src/observability/__init__.py](../src/observability/__init__.py) | Retained | 1 | 0 |
| [src/observability/admin_logs.py](../src/observability/admin_logs.py) | Retained | 157 | 40 |
| [src/operations/__init__.py](../src/operations/__init__.py) | Added | 1 | 0 |
| [src/operations/service_environment.py](../src/operations/service_environment.py) | Added | 419 | 182 |
| [src/providers/__init__.py](../src/providers/__init__.py) | Retained | 1 | 0 |
| [src/providers/gemini.py](../src/providers/gemini.py) | Refactored | 399 | 226 |
| [src/providers/google_query.py](../src/providers/google_query.py) | Added | 195 | 159 |
| [src/providers/groq_query.py](../src/providers/groq_query.py) | Added | 176 | 122 |
| [src/providers/ollama.py](../src/providers/ollama.py) | Retained | 158 | 29 |
| [src/providers/query_transport.py](../src/providers/query_transport.py) | Added | 214 | 22 |
| [src/search/__init__.py](../src/search/__init__.py) | Retained | 1 | 0 |
| [src/search/bm25.py](../src/search/bm25.py) | Retained | 595 | 97 |
| [src/search/catalog_matching.py](../src/search/catalog_matching.py) | Added | 439 | 78 |
| [src/search/engine.py](../src/search/engine.py) | Refactored | 778 | 271 |
| [src/search/engine_support.py](../src/search/engine_support.py) | Retained | 621 | 170 |
| [src/search/fast_paths.py](../src/search/fast_paths.py) | Added | 286 | 153 |
| [src/search/helpers.py](../src/search/helpers.py) | Retained | 12 | 12 |
| [src/search/planner.py](../src/search/planner.py) | Refactored | 178 | 112 |
| [src/search/planner_catalog.py](../src/search/planner_catalog.py) | Refactored | 364 | 83 |
| [src/search/planner_rules.py](../src/search/planner_rules.py) | Retained | 478 | 0 |
| [src/search/policy.py](../src/search/policy.py) | Retained | 154 | 8 |
| [src/search/policy_registry.py](../src/search/policy_registry.py) | Retained | 23 | 9 |
| [src/search/query_constraints.py](../src/search/query_constraints.py) | Added | 140 | 58 |
| [src/search/query_enrichment.py](../src/search/query_enrichment.py) | Added | 329 | 301 |
| [src/search/query_intent.py](../src/search/query_intent.py) | Added | 43 | 6 |
| [src/search/query_terms.py](../src/search/query_terms.py) | Added | 172 | 31 |
| [src/search/ranking.py](../src/search/ranking.py) | Retained | 183 | 132 |
| [src/search/related_results.py](../src/search/related_results.py) | Added | 152 | 143 |
| [src/search/reranker.py](../src/search/reranker.py) | Retained | 467 | 78 |
| [src/search/retrieval.py](../src/search/retrieval.py) | Retained | 469 | 142 |
| [src/storage/__init__.py](../src/storage/__init__.py) | Retained | 1 | 0 |
| [src/storage/database.py](../src/storage/database.py) | Retained | 364 | 54 |
| [src/storage/index_generations.py](../src/storage/index_generations.py) | Retained | 277 | 64 |
| [src/storage/index_layout.py](../src/storage/index_layout.py) | Retained | 21 | 5 |
| [src/storage/mysql.py](../src/storage/mysql.py) | Retained | 437 | 74 |
| [src/storage/pgvector.py](../src/storage/pgvector.py) | Refactored | 751 | 130 |
| [src/storage/postgres.py](../src/storage/postgres.py) | Retained | 341 | 64 |
| [src/storage/redis.py](../src/storage/redis.py) | Retained | 366 | 66 |
| [src/storage/search_analytics.py](../src/storage/search_analytics.py) | Refactored | 555 | 158 |
| [src/storage/search_analytics_schema.py](../src/storage/search_analytics_schema.py) | Added | 489 | 274 |
| [src/storage/search_analytics_spool.py](../src/storage/search_analytics_spool.py) | Retained | 448 | 92 |
| [src/storage/usage.py](../src/storage/usage.py) | Retained | 194 | 66 |
| [src/storage/vector.py](../src/storage/vector.py) | Retained | 96 | 33 |
| [src/storage/vector_filters.py](../src/storage/vector_filters.py) | Added | 142 | 45 |
| [src/storage/vector_query.py](../src/storage/vector_query.py) | Added | 198 | 103 |
| [src/tenants/__init__.py](../src/tenants/__init__.py) | Retained | 1 | 0 |
| [src/tenants/compatibility.py](../src/tenants/compatibility.py) | Retained | 78 | 15 |
| [src/tenants/default/__init__.py](../src/tenants/default/__init__.py) | Retained | 1 | 0 |
| [src/tenants/default/plugin.py](../src/tenants/default/plugin.py) | Retained | 34 | 7 |
| [src/tenants/gainr/__init__.py](../src/tenants/gainr/__init__.py) | Retained | 1 | 0 |
| [src/tenants/gainr/analytics.py](../src/tenants/gainr/analytics.py) | Retained | 37 | 0 |
| [src/tenants/gainr/analytics_schema.py](../src/tenants/gainr/analytics_schema.py) | Retained | 212 | 0 |
| [src/tenants/gainr/cards.py](../src/tenants/gainr/cards.py) | Added | 130 | 80 |
| [src/tenants/gainr/compatibility.py](../src/tenants/gainr/compatibility.py) | Refactored | 838 | 227 |
| [src/tenants/gainr/filter_diagnostics.py](../src/tenants/gainr/filter_diagnostics.py) | Added | 128 | 115 |
| [src/tenants/gainr/filtered_page.py](../src/tenants/gainr/filtered_page.py) | Added | 209 | 170 |
| [src/tenants/gainr/models.py](../src/tenants/gainr/models.py) | Retained | 208 | 15 |
| [src/tenants/gainr/plugin.py](../src/tenants/gainr/plugin.py) | Retained | 84 | 11 |
| [src/tenants/gainr/policy.py](../src/tenants/gainr/policy.py) | Refactored | 489 | 110 |
| [src/tenants/gainr/policy_rules.py](../src/tenants/gainr/policy_rules.py) | Added | 409 | 0 |
| [src/tenants/gainr/relations.py](../src/tenants/gainr/relations.py) | Added | 269 | 178 |
| [src/tenants/gainr/repository.py](../src/tenants/gainr/repository.py) | Refactored | 689 | 147 |
| [src/tenants/gainr/sql_filters.py](../src/tenants/gainr/sql_filters.py) | Added | 177 | 144 |
| [src/tenants/plugin.py](../src/tenants/plugin.py) | Retained | 75 | 11 |
| [src/tenants/registry.py](../src/tenants/registry.py) | Retained | 70 | 14 |
| [src/verticals/__init__.py](../src/verticals/__init__.py) | Retained | 1 | 0 |
| [src/verticals/marketplace/__init__.py](../src/verticals/marketplace/__init__.py) | Retained | 1 | 0 |
| [src/verticals/marketplace/analytics/adapter.py](../src/verticals/marketplace/analytics/adapter.py) | Retained | 261 | 54 |
| [src/verticals/marketplace/analytics/dashboard_charts.py](../src/verticals/marketplace/analytics/dashboard_charts.py) | Added | 121 | 70 |
| [src/verticals/marketplace/analytics/dashboard_filters.py](../src/verticals/marketplace/analytics/dashboard_filters.py) | Refactored | 329 | 97 |
| [src/verticals/marketplace/analytics/dashboard_records.py](../src/verticals/marketplace/analytics/dashboard_records.py) | Added | 247 | 86 |
| [src/verticals/marketplace/analytics/dashboard_summaries.py](../src/verticals/marketplace/analytics/dashboard_summaries.py) | Added | 259 | 160 |
| [src/verticals/marketplace/analytics/domain/__init__.py](../src/verticals/marketplace/analytics/domain/__init__.py) | Retained | 19 | 0 |
| [src/verticals/marketplace/analytics/domain/api_bottleneck_metrics.py](../src/verticals/marketplace/analytics/domain/api_bottleneck_metrics.py) | Added | 163 | 87 |
| [src/verticals/marketplace/analytics/domain/api_provider_metrics.py](../src/verticals/marketplace/analytics/domain/api_provider_metrics.py) | Added | 291 | 116 |
| [src/verticals/marketplace/analytics/domain/api_request_metrics.py](../src/verticals/marketplace/analytics/domain/api_request_metrics.py) | Added | 272 | 100 |
| [src/verticals/marketplace/analytics/domain/business_insights.py](../src/verticals/marketplace/analytics/domain/business_insights.py) | Retained | 312 | 207 |
| [src/verticals/marketplace/analytics/domain/config.py](../src/verticals/marketplace/analytics/domain/config.py) | Refactored | 426 | 0 |
| [src/verticals/marketplace/analytics/domain/listing_metrics.py](../src/verticals/marketplace/analytics/domain/listing_metrics.py) | Added | 101 | 92 |
| [src/verticals/marketplace/analytics/domain/market_growth_metrics.py](../src/verticals/marketplace/analytics/domain/market_growth_metrics.py) | Added | 169 | 80 |
| [src/verticals/marketplace/analytics/domain/market_supply_metrics.py](../src/verticals/marketplace/analytics/domain/market_supply_metrics.py) | Added | 113 | 106 |
| [src/verticals/marketplace/analytics/domain/part_a_search.py](../src/verticals/marketplace/analytics/domain/part_a_search.py) | Refactored | 30 | 21 |
| [src/verticals/marketplace/analytics/domain/part_b_api.py](../src/verticals/marketplace/analytics/domain/part_b_api.py) | Refactored | 66 | 46 |
| [src/verticals/marketplace/analytics/domain/part_c_deep.py](../src/verticals/marketplace/analytics/domain/part_c_deep.py) | Refactored | 40 | 29 |
| [src/verticals/marketplace/analytics/domain/part_d_market.py](../src/verticals/marketplace/analytics/domain/part_d_market.py) | Refactored | 40 | 31 |
| [src/verticals/marketplace/analytics/domain/pricing_metrics.py](../src/verticals/marketplace/analytics/domain/pricing_metrics.py) | Added | 76 | 69 |
| [src/verticals/marketplace/analytics/domain/report_utils.py](../src/verticals/marketplace/analytics/domain/report_utils.py) | Added | 18 | 5 |
| [src/verticals/marketplace/analytics/domain/scope.py](../src/verticals/marketplace/analytics/domain/scope.py) | Retained | 31 | 9 |
| [src/verticals/marketplace/analytics/domain/search/__init__.py](../src/verticals/marketplace/analytics/domain/search/__init__.py) | Retained | 27 | 0 |
| [src/verticals/marketplace/analytics/domain/search/classification.py](../src/verticals/marketplace/analytics/domain/search/classification.py) | Retained | 185 | 27 |
| [src/verticals/marketplace/analytics/domain/search/insights.py](../src/verticals/marketplace/analytics/domain/search/insights.py) | Retained | 240 | 52 |
| [src/verticals/marketplace/analytics/domain/search/records.py](../src/verticals/marketplace/analytics/domain/search/records.py) | Retained | 590 | 115 |
| [src/verticals/marketplace/analytics/domain/search_demand_metrics.py](../src/verticals/marketplace/analytics/domain/search_demand_metrics.py) | Added | 265 | 127 |
| [src/verticals/marketplace/analytics/domain/search_quality_metrics.py](../src/verticals/marketplace/analytics/domain/search_quality_metrics.py) | Added | 174 | 108 |
| [src/verticals/marketplace/analytics/domain/search_segment_metrics.py](../src/verticals/marketplace/analytics/domain/search_segment_metrics.py) | Added | 144 | 129 |
| [src/verticals/marketplace/analytics/domain/supply_metrics.py](../src/verticals/marketplace/analytics/domain/supply_metrics.py) | Added | 233 | 168 |
| [src/verticals/marketplace/analytics/domain/user_metrics.py](../src/verticals/marketplace/analytics/domain/user_metrics.py) | Added | 155 | 145 |
| [src/verticals/marketplace/analytics/domain/utils.py](../src/verticals/marketplace/analytics/domain/utils.py) | Retained | 67 | 26 |
| [src/verticals/marketplace/analytics/metric_catalog.py](../src/verticals/marketplace/analytics/metric_catalog.py) | Retained | 171 | 33 |
| [src/verticals/marketplace/analytics/metrics.py](../src/verticals/marketplace/analytics/metrics.py) | Retained | 211 | 0 |
| [src/verticals/marketplace/analytics/query_spool.py](../src/verticals/marketplace/analytics/query_spool.py) | Retained | 35 | 6 |
| [src/verticals/marketplace/policy.py](../src/verticals/marketplace/policy.py) | Retained | 117 | 15 |
| [tests/analytics/test_activity_spool.py](../tests/analytics/test_activity_spool.py) | Retained | 131 | 46 |
| [tests/analytics/test_analytics_service.py](../tests/analytics/test_analytics_service.py) | Retained | 1954 | 221 |
| [tests/analytics/test_analytics_users.py](../tests/analytics/test_analytics_users.py) | Retained | 145 | 55 |
| [tests/analytics/test_dashboard_cache.py](../tests/analytics/test_dashboard_cache.py) | Retained | 98 | 41 |
| [tests/analytics/test_dashboard_filters.py](../tests/analytics/test_dashboard_filters.py) | Retained | 200 | 37 |
| [tests/analytics/test_portal_sessions.py](../tests/analytics/test_portal_sessions.py) | Retained | 751 | 121 |
| [tests/analytics/test_sqlite.py](../tests/analytics/test_sqlite.py) | Added | 52 | 14 |
| [tests/analytics/test_tenant_computation_isolation.py](../tests/analytics/test_tenant_computation_isolation.py) | Retained | 187 | 83 |
| [tests/analytics/test_tenant_plugins.py](../tests/analytics/test_tenant_plugins.py) | Retained | 35 | 18 |
| [tests/api/test_api.py](../tests/api/test_api.py) | Retained | 1919 | 147 |
| [tests/cli/test_chat.py](../tests/cli/test_chat.py) | Retained | 1629 | 66 |
| [tests/cli/test_evaluate_retrieval.py](../tests/cli/test_evaluate_retrieval.py) | Retained | 245 | 71 |
| [tests/conftest.py](../tests/conftest.py) | Retained | 9 | 0 |
| [tests/core/test_tenant_config.py](../tests/core/test_tenant_config.py) | Retained | 508 | 55 |
| [tests/ingestion/test_generation_promotion.py](../tests/ingestion/test_generation_promotion.py) | Retained | 238 | 57 |
| [tests/ingestion/test_ingest.py](../tests/ingestion/test_ingest.py) | Retained | 549 | 139 |
| [tests/ingestion/test_resource_cleanup.py](../tests/ingestion/test_resource_cleanup.py) | Added | 72 | 61 |
| [tests/observability/test_admin_logs.py](../tests/observability/test_admin_logs.py) | Retained | 138 | 46 |
| [tests/operations/test_check_markdown.py](../tests/operations/test_check_markdown.py) | Retained | 36 | 12 |
| [tests/operations/test_doctor.py](../tests/operations/test_doctor.py) | Retained | 42 | 21 |
| [tests/operations/test_runtime_isolation.py](../tests/operations/test_runtime_isolation.py) | Retained | 349 | 57 |
| [tests/operations/test_warm_search_paths.py](../tests/operations/test_warm_search_paths.py) | Retained | 58 | 17 |
| [tests/providers/test_gemini.py](../tests/providers/test_gemini.py) | Retained | 1084 | 69 |
| [tests/providers/test_ollama.py](../tests/providers/test_ollama.py) | Retained | 123 | 28 |
| [tests/search/test_engine.py](../tests/search/test_engine.py) | Retained | 3185 | 126 |
| [tests/search/test_policy.py](../tests/search/test_policy.py) | Retained | 108 | 15 |
| [tests/search/test_reranker.py](../tests/search/test_reranker.py) | Retained | 447 | 41 |
| [tests/search/test_retrieval.py](../tests/search/test_retrieval.py) | Retained | 74 | 25 |
| [tests/storage/test_connections.py](../tests/storage/test_connections.py) | Retained | 343 | 72 |
| [tests/storage/test_index_generations.py](../tests/storage/test_index_generations.py) | Retained | 80 | 36 |
| [tests/storage/test_pgvector.py](../tests/storage/test_pgvector.py) | Retained | 882 | 105 |
| [tests/storage/test_redis.py](../tests/storage/test_redis.py) | Retained | 180 | 35 |
| [tests/storage/test_search_analytics.py](../tests/storage/test_search_analytics.py) | Retained | 411 | 85 |
| [tests/storage/test_usage.py](../tests/storage/test_usage.py) | Retained | 44 | 41 |
| [tests/tenants/gainr/test_compatibility.py](../tests/tenants/gainr/test_compatibility.py) | Retained | 1656 | 98 |
| [tests/tenants/test_plugins.py](../tests/tenants/test_plugins.py) | Retained | 166 | 29 |
| [tests/test_analytics_retry.py](../tests/test_analytics_retry.py) | Retained | 180 | 64 |
| [tests/test_production_backup.py](../tests/test_production_backup.py) | Retained | 20 | 7 |
