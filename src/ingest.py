import argparse

from ingestion_service import (
    EMBED_BATCH_SIZE,
    MYSQL_BATCH_SIZE,
    check_mysql_source,
    ingest_mysql_source,
    rebuild_mysql_bm25_index,
)
from tenant_config import discover_tenant_profiles
from vector_store import (
    clear_tenant_vectors,
    delete_tenant_source,
    list_tenant_vectors,
)


def confirm(message: str, assume_yes: bool = False) -> bool:
    return assume_yes or input(f"{message} [y/N]: ").strip().casefold() in {
        "y",
        "yes",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Manage a tenant's configured database, pgvector, and BM25 indexes."
        )
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--check",
        action="store_true",
        help="validate source files without calling Ollama",
    )
    actions.add_argument(
        "--list",
        action="store_true",
        help="list indexed source files and chunk counts",
    )
    actions.add_argument(
        "--delete",
        metavar="FILENAME",
        help="delete one source file from the index",
    )
    actions.add_argument(
        "--clear",
        action="store_true",
        help="delete the entire vector collection",
    )
    parser.add_argument(
        "--company",
        required=True,
        help="tenant profile slug under configs/tenants",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip confirmation for delete operations",
    )
    parser.add_argument(
        "--database",
        "--mysql",
        dest="database",
        action="store_true",
        help="ingest the configured MySQL or PostgreSQL source table",
    )
    parser.add_argument(
        "--bm25-only",
        "--mysql-bm25-only",
        dest="bm25_only",
        action="store_true",
        help="rebuild only BM25 from the configured database; no embeddings",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="limit database rows for smoke-test ingestion",
    )
    parser.add_argument(
        "--mysql-batch-size",
        type=int,
        default=MYSQL_BATCH_SIZE,
        help=(
            "database rows per vector/BM25 batch "
            f"(default: {MYSQL_BATCH_SIZE})"
        ),
    )
    parser.add_argument(
        "--embed-batch-size",
        type=int,
        default=EMBED_BATCH_SIZE,
        help=f"texts to send per Ollama embedding request (default: {EMBED_BATCH_SIZE})",
    )
    parser.add_argument(
        "--mysql-primary-key",
        help="override the detected database primary-key column",
    )
    parser.add_argument(
        "--mysql-replace-source",
        action="store_true",
        help=(
            "clear this company's vector/BM25 source before authoritative rebuild"
        ),
    )
    parser.add_argument(
        "--mysql-force-reembed",
        action="store_true",
        help="re-embed rows even when the existing content hash matches",
    )
    parser.add_argument(
        "--mysql-reconcile-deletions",
        action="store_true",
        help=(
            "after a successful full scan, remove tenant vector/BM25 rows "
            "that no longer exist in the configured source table"
        ),
    )
    args = parser.parse_args()
    profiles = discover_tenant_profiles()
    try:
        tenant = profiles[args.company]
    except KeyError as exc:
        available = ", ".join(sorted(profiles)) or "none"
        raise SystemExit(
            f"Unknown company {args.company!r}; available: {available}"
        ) from exc

    if args.list:
        list_tenant_vectors(tenant)
        return
    if args.delete:
        if not confirm(
            f"Delete tenant vectors for source {args.delete!r}?", args.yes
        ):
            print("Cancelled.")
            return
        deleted = delete_tenant_source(tenant, args.delete)
        print(f"Deleted {deleted} vectors.")
        return
    if args.clear:
        if not confirm(
            f"Delete all vectors for company {tenant.company_id!r}?", args.yes
        ):
            print("Cancelled.")
            return
        deleted = clear_tenant_vectors(tenant)
        print(f"Deleted {deleted} vectors.")
        return
    if args.database and args.check:
        raise SystemExit(
            0
            if check_mysql_source(
                args.limit,
                args.mysql_primary_key,
                tenant=tenant,
            )
            else 1
        )
    if args.bm25_only:
        rebuild_mysql_bm25_index(
            limit=args.limit,
            batch_size=args.mysql_batch_size,
            primary_key_column=args.mysql_primary_key,
            tenant=tenant,
        )
        return
    if args.database:
        ingest_mysql_source(
            limit=args.limit,
            batch_size=args.mysql_batch_size,
            embed_batch_size=args.embed_batch_size,
            primary_key_column=args.mysql_primary_key,
            replace_source=args.mysql_replace_source,
            force_reembed=args.mysql_force_reembed,
            reconcile_deletions=args.mysql_reconcile_deletions,
            tenant=tenant,
        )
        return

    raise SystemExit("Choose --database, --bm25-only, --list, --delete, or --clear.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        raise SystemExit(f"Error: {exc}") from exc
