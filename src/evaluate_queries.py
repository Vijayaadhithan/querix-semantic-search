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
from tenant_config import discover_tenant_profiles

DEFAULT_CASES_PATH = PROJECT_ROOT / "eval" / "query_cases.json"


def nested_value(document: dict, path: str):
    value = document
    for key in path.split("."):
        value = value[key]
    return value


def evaluate_case(
    case: dict,
    value_index: dict,
    catalog: dict,
    prompt_context: str = "",
    query_aliases: dict[str, str] | None = None,
) -> tuple[dict, list]:
    plan = extract_query_plan(
        case["query"],
        catalog,
        prompt_context=prompt_context,
        query_aliases=query_aliases,
    )
    plan = enrich_query_plan(
        case["query"],
        plan,
        value_index,
        query_aliases,
    )
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
    parser.add_argument(
        "--company",
        help="tenant profile slug; uses that company's isolated BM25 index",
    )
    args = parser.parse_args()

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.limit is not None:
        cases = cases[: args.limit]

    profile = None
    if args.company:
        profiles = discover_tenant_profiles()
        try:
            profile = profiles[args.company]
        except KeyError as exc:
            available = ", ".join(sorted(profiles)) or "none"
            raise SystemExit(
                f"Unknown company {args.company!r}; available: {available}"
            ) from exc
    index = PersistentBM25Index(
        profile.storage.bm25_path if profile else None
    ) if profile else PersistentBM25Index()
    try:
        value_index = query_filter_value_index(index)
        catalog = build_query_filter_catalog(value_index)
        failed = 0
        for case in cases:
            plan, failures = evaluate_case(
                case,
                value_index,
                catalog,
                profile.planner_prompt_context if profile else "",
                profile.planner_query_aliases if profile else None,
            )
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
