# Retrieval evaluation gates

Run the reviewed Gainr cases with:

```bash
.venv/bin/python src/evaluate_retrieval.py \
  --company gainr \
  --cases eval/retrieval_cases.json \
  --runs 3 \
  --plan-snapshot /tmp/gainr-retrieval-plans.json \
  --report /tmp/gainr-retrieval-report.json
```

The evaluator plans each query once, retrieves one fused candidate set, and
reranks that exact candidate set for every requested run. It reports the
candidate fingerprint, candidate recall, every reciprocal rank, median MRR,
the selected planner/reranker providers, and every fallback attempt. A plan
snapshot is accepted only when the company and full planner/catalog
fingerprint match; use `--refresh-plans` after an intentional planner or
catalog change.

Each case can use the existing `relevant_ids`, `expected_filters`,
`source_filters`, or `acceptable_filters` labels. Optional production gates:

```json
{
  "name": "reviewed_vehicle_query",
  "query": "comfortable vehicle for a long trip",
  "relevant_ids": ["123", "456"],
  "result_limit": 40,
  "min_result_count": 40,
  "min_reciprocal_rank": 0.2,
  "min_precision_at_3": 0.67,
  "min_hit_rate_at_10": 1.0,
  "min_candidate_recall": 0.5,
  "forbidden_ids": ["999"]
}
```

The case file may also be an object with suite gates:

```json
{
  "reranker_runs": 3,
  "minimum_median_mrr": 0.75,
  "max_reranker_fallbacks": 0,
  "cases": []
}
```

`result_limit: 40` checks the ranked first page plus continuation inventory.
Only add thresholds and forbidden IDs after a human has reviewed the labels;
generated category matches alone are not a reliable relevance judgment.

The reviewed set should contain all routing classes:

- exact category and simple-filter queries expected to use deterministic
  database lookup;
- descriptive English semantic queries;
- typos, colloquial language, and multilingual or romanized queries;
- explicit location, price, duration, and offer/wanted constraints;
- negative pairs where a similar spelling must not become a hard filter, such
  as `escort` versus `resort`;
- tenant aliases that must improve only the configured company's semantics.

For relevance checks, verify that unrelated ads are absent or below relevant
ads, not merely that one expected ID appears somewhere in a large result set.
Routing and payload-contract tests should run alongside retrieval evaluation.

Evaluation reads the existing indexes. It does not require re-ingestion after
an API-only, pagination, caching, fallback, Docker, or documentation change.
Run ingestion first only when the evaluated source/index contract changed.

Before approving a ranking release, record the case-file revision, plan
snapshot fingerprint, candidate hashes, pass count, every run MRR, median MRR,
precision gates, wall time, and every provider fallback. A degraded/fallback
run is an infrastructure failure when the suite allows zero fallbacks; it must
not silently become the new accuracy baseline.
