import argparse
import json
from pathlib import Path

from search_engine import ProductSearchEngine
from settings import PROJECT_ROOT

DEFAULT_CASES_PATH = PROJECT_ROOT / "eval" / "retrieval_cases.json"


def reciprocal_rank(result_ids: list[str], relevant_ids: set[str]) -> float:
    for rank, product_id in enumerate(result_ids, start=1):
        if product_id in relevant_ids:
            return 1.0 / rank
    return 0.0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run labeled end-to-end semantic retrieval cases."
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_PATH,
        help=f"retrieval case JSON file (default: {DEFAULT_CASES_PATH})",
    )
    parser.add_argument("--limit", type=int, help="run only the first N cases")
    args = parser.parse_args()

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.limit is not None:
        cases = cases[: args.limit]

    engine = ProductSearchEngine()
    passed = 0
    reciprocal_ranks = []
    try:
        for case in cases:
            result = engine.search(case["query"])
            result_ids = [str(value) for value in result["product_ids"]]
            if case.get("expected_empty"):
                success = not result_ids
                rr = 1.0 if success else 0.0
            else:
                relevant_ids = {str(value) for value in case["relevant_ids"]}
                rr = reciprocal_rank(result_ids, relevant_ids)
                success = rr > 0
            reciprocal_ranks.append(rr)
            passed += int(success)
            label = "PASS" if success else "FAIL"
            print(f"{label} {case['name']}: {result_ids}")
    finally:
        engine.close()

    mean_reciprocal_rank = (
        sum(reciprocal_ranks) / len(reciprocal_ranks)
        if reciprocal_ranks
        else 0.0
    )
    print(
        f"\n{passed}/{len(cases)} retrieval cases passed; "
        f"MRR={mean_reciprocal_rank:.3f}"
    )
    raise SystemExit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    main()
