import argparse
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tenant_config import discover_tenant_profiles  # noqa: E402
from vector_store import get_tenant_vector_collection  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create online pgvector indexes for adaptive filtered search."
    )
    parser.add_argument(
        "--company",
        required=True,
        help="tenant profile slug under configs/tenants",
    )
    args = parser.parse_args()

    profiles = discover_tenant_profiles()
    try:
        profile = profiles[args.company]
    except KeyError as exc:
        choices = ", ".join(sorted(profiles)) or "none"
        raise SystemExit(
            f"Unknown company {args.company!r}; configured companies: {choices}"
        ) from exc

    collection = get_tenant_vector_collection(profile)
    started = time.perf_counter()
    indexes = collection.ensure_filter_indexes(concurrently=True)
    elapsed = time.perf_counter() - started
    print(
        f"Company {profile.company_id}: ensured {len(indexes)} filtered-search "
        f"indexes on {collection.name} in {elapsed:.1f}s"
    )


if __name__ == "__main__":
    main()
