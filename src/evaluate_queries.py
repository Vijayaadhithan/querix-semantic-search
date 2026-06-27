import argparse
import json
from pathlib import Path

from bm25_index import PersistentBM25Index
from query_planner import (
    build_query_filter_catalog,
    enrich_query_plan,
    extract_query_plan,
    query_filter_value_index,
)
from settings import PROJECT_ROOT

DEFAULT_CASES_PATH = PROJECT_ROOT / "eval" / "query_cases.json"


def nested_value(document: dict, path: str):
    value = document
    for key in path.split("."):
        value = value[key]
    return value


def evaluate_case(case: dict, value_index: dict, catalog: dict) -> tuple[dict, list]:
    plan = extract_query_plan(case["query"], catalog)
    plan = enrich_query_plan(case["query"], plan, value_index)
    failures = []
    for path, expected in case["expected"].items():
        actual = nested_value(plan, path)
        if actual != expected:
            failures.append(
                {
                    "path": path,
                    "expected": expected,
                    "actual": actual,
                }
            )
    return plan, failures


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate query extraction against semantic-search scenarios."
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_PATH,
        help=f"query case JSON file (default: {DEFAULT_CASES_PATH})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="run only the first N cases",
    )
    args = parser.parse_args()

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.limit is not None:
        cases = cases[: args.limit]

    index = PersistentBM25Index()
    try:
        value_index = query_filter_value_index(index)
        catalog = build_query_filter_catalog(value_index)
        failed = 0
        for case in cases:
            plan, failures = evaluate_case(case, value_index, catalog)
            if failures:
                failed += 1
                print(f"FAIL {case['name']}")
                print(json.dumps(failures, ensure_ascii=False, indent=2))
                print("Plan: " + json.dumps(plan, ensure_ascii=False, default=str))
            else:
                print(f"PASS {case['name']}")
    finally:
        index.close()

    print(f"\n{len(cases) - failed}/{len(cases)} query cases passed.")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
