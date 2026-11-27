import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.tenant_config import discover_tenant_profiles
from storage.index_generations import profile_for_slot
from storage.pgvector import PgVectorCollection
from storage.vector import get_tenant_vector_collection
from storage.vector_city_membership import ensure_city_membership


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create online pgvector indexes for adaptive filtered search."
    )
    parser.add_argument(
        "--company",
        required=True,
        help="tenant profile slug under configs/tenants",
    )
    parser.add_argument(
        "--city-membership",
        action="store_true",
        help="Backfill city lookups and install synchronization for existing slots",
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

    if args.city_membership:
        if not profile.storage.pgvector_city_membership:
            parser.error("City membership is disabled for this tenant")
        for slot in ("a", "b"):
            storage = profile_for_slot(profile, slot).storage
            config = storage.pgvector_database
            table = storage.pgvector_table
            if PgVectorCollection.table_exists(config, table):
                ensure_city_membership(config, table)
                print(f"Company {profile.company_id}: prepared city lookup on {table}")
        return

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
