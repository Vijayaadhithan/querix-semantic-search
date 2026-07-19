# Retrieval evaluation gates

Run the reviewed Gainr cases with:

```bash
.venv/bin/python src/evaluate_retrieval.py \
  --company gainr \
  --cases eval/gainr_semantic_cases.generated.json
```

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
  "forbidden_ids": ["999"]
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

Before approving a ranking release, record the case-file revision, pass count,
MRR, precision gates, wall time, and whether any run was degraded. Do not treat
one historical benchmark as a permanent production guarantee.
