import time
from concurrent.futures import ThreadPoolExecutor

from core.settings import (
    MYSQL_BM25_COLUMN,
    MYSQL_CONTENT_COLUMN,
    MYSQL_DATABASE,
    MYSQL_TABLE,
)
from core.tenant_config import TenantIngestionConfig, TenantProfile
from ingestion.batch import IngestionBatch
from ingestion.change_detection import (
    DatabaseRowChanges,
    database_current_ids,
    database_row_changes,
)
from ingestion.documents import prepare_bm25_index_row, prepare_mysql_row
from ingestion.mapping import canonicalize_search_ready_row
from ingestion.state import (
    begin_ingestion,
    complete_ingestion,
    fail_ingestion,
    ingestion_state_path,
    read_ingestion_state,
)
from providers.ollama import embed_texts, resolve_embedding_model_revision
from search.bm25 import PersistentBM25Index
from storage.database import (
    count_database_rows,
    database_backend,
    database_source_name,
    detect_database_primary_key,
    fetch_database_columns,
    iter_database_rows,
)
from storage.vector import get_tenant_vector_collection

__all__ = (
    "DatabaseRowChanges",
    "EMBED_BATCH_SIZE",
    "MYSQL_BATCH_SIZE",
    "check_mysql_source",
    "database_current_ids",
    "database_row_changes",
    "embed_for_upsert",
    "format_duration",
    "ingest_mysql_source",
    "rebuild_mysql_bm25_index",
    "reconcile_deleted_documents",
)

EMBED_BATCH_SIZE = 32
MYSQL_BATCH_SIZE = 500


def check_mysql_source(
    limit: int | None = None,
    primary_key_column: str | None = None,
    tenant: TenantProfile | None = None,
) -> bool:
    mysql_config = tenant.database if tenant else None
    content_column = (
        mysql_config.content_column if mysql_config else MYSQL_CONTENT_COLUMN
    )
    database = mysql_config.database if mysql_config else MYSQL_DATABASE
    table = mysql_config.search_table if mysql_config else MYSQL_TABLE
    columns = fetch_database_columns(mysql_config)
    if content_column not in columns:
        print(f"ERROR: column '{content_column}' was not found in {database}.{table}.")
        print(f"Available columns: {', '.join(columns)}")
        return False

    detected_primary_key = detect_database_primary_key(
        columns,
        primary_key_column,
        mysql_config,
    )
    row_count = count_database_rows(content_column, mysql_config)
    planned_rows = min(row_count, limit) if limit is not None else row_count

    print(f"OK: {database_backend(mysql_config)} table {database}.{table}")
    if tenant:
        print(f"Company: {tenant.company_id}")
    print(f"Content column: {content_column}")
    print(f"Primary key column: {detected_primary_key or 'none detected'}")
    print(f"Rows with embedding text: {row_count}")
    print(f"Rows planned for ingestion: {planned_rows}")
    print("No embeddings were generated during this check.")
    return True


def embed_for_upsert(
    documents: list[str],
    embed_batch_size: int = EMBED_BATCH_SIZE,
    progress_prefix: str = "",
) -> list[list[float]]:
    batches = [
        documents[start : start + embed_batch_size]
        for start in range(0, len(documents), embed_batch_size)
    ]
    if not batches:
        return []

    def embed_batch(batch: list[str]) -> list[list[float]]:
        for attempt in range(1, 4):
            try:
                return embed_texts(batch)
            except RuntimeError:
                if attempt == 3:
                    raise
                time.sleep(attempt * 2)
        raise AssertionError("unreachable")

    embeddings = []
    with ThreadPoolExecutor(max_workers=min(4, len(batches))) as executor:
        for batch_number, batch_embeddings in enumerate(
            executor.map(embed_batch, batches),
            start=1,
        ):
            batch = batches[batch_number - 1]
            embeddings.extend(batch_embeddings)
            if progress_prefix:
                completed = min(
                    (batch_number - 1) * embed_batch_size + len(batch),
                    len(documents),
                )
                print(
                    f"{progress_prefix} embedding {completed}/{len(documents)} texts",
                    flush=True,
                )
    return embeddings


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def reconcile_deleted_documents(
    collection,
    bm25_index: PersistentBM25Index,
    source_name: str,
    seen_ids: set[str],
) -> tuple[int, int]:
    vector_rows = collection.get(
        where={"source_file": source_name},
        include=[],
    )
    vector_ids = {str(doc_id) for doc_id in vector_rows.get("ids", [])}
    stale_vector_ids = vector_ids - seen_ids
    stale_bm25_ids = bm25_index.doc_ids() - seen_ids

    if stale_vector_ids:
        collection.delete(ids=sorted(stale_vector_ids))
    deleted_bm25 = bm25_index.delete_doc_ids(stale_bm25_ids)
    return len(stale_vector_ids), deleted_bm25


def _ingest_mysql_source(
    limit: int | None = None,
    batch_size: int = MYSQL_BATCH_SIZE,
    embed_batch_size: int = EMBED_BATCH_SIZE,
    primary_key_column: str | None = None,
    replace_source: bool = False,
    force_reembed: bool = False,
    reconcile_deletions: bool = False,
    tenant: TenantProfile | None = None,
) -> None:
    if batch_size <= 0:
        raise RuntimeError("--mysql-batch-size must be greater than zero.")
    if embed_batch_size <= 0:
        raise RuntimeError("--embed-batch-size must be greater than zero.")
    if limit is not None and limit <= 0:
        raise RuntimeError("--limit must be greater than zero.")
    if reconcile_deletions and limit is not None:
        raise RuntimeError(
            "Deletion reconciliation requires a full scan; remove --limit."
        )

    mysql_config = tenant.database if tenant else None
    content_column = (
        mysql_config.content_column if mysql_config else MYSQL_CONTENT_COLUMN
    )
    database = mysql_config.database if mysql_config else MYSQL_DATABASE
    table = mysql_config.search_table if mysql_config else MYSQL_TABLE
    columns = fetch_database_columns(mysql_config)
    if content_column not in columns:
        raise RuntimeError(
            f"Column '{content_column}' was not found in {database}.{table}."
        )
    detected_primary_key = detect_database_primary_key(
        columns,
        primary_key_column,
        mysql_config,
    )
    bm25_column = (
        mysql_config.bm25_column
        if mysql_config and mysql_config.bm25_column in columns
        else (MYSQL_BM25_COLUMN if MYSQL_BM25_COLUMN in columns else content_column)
    )
    row_count = count_database_rows(content_column, mysql_config)
    planned_rows = min(row_count, limit) if limit is not None else row_count

    if tenant is None:
        raise RuntimeError("Tenant profile is required for pgvector ingestion.")
    collection = get_tenant_vector_collection(tenant, create=True)
    bm25_index = PersistentBM25Index(tenant.storage.bm25_path)
    try:
        source_name = database_source_name(mysql_config)
        active_embedding_revision = resolve_embedding_model_revision()
        consistency_path = ingestion_state_path(tenant.storage.bm25_path)
        consistency_active = read_ingestion_state(consistency_path) is not None
        if consistency_active:
            begin_ingestion(consistency_path, company_id=tenant.company_id)

        def ensure_consistency_gate() -> None:
            nonlocal consistency_active
            if consistency_active:
                return
            begin_ingestion(consistency_path, company_id=tenant.company_id)
            consistency_active = True

        print(f"Processing {database_backend(mysql_config)} table: {database}.{table}")
        if tenant:
            print(f"Company: {tenant.company_id}")
        print(f"Content column: {content_column}")
        print(f"BM25 column: {bm25_column}")
        print(f"Primary key column: {detected_primary_key or 'none detected'}")
        print(f"Rows planned for ingestion: {planned_rows}")

        if replace_source:
            ensure_consistency_gate()
            existing = collection.get(where={"source_file": source_name}, include=[])
            if existing["ids"]:
                collection.delete(where={"source_file": source_name})
                print(
                    f"Deleted {len(existing['ids'])} existing chunks for {source_name}."
                )
            bm25_index.clear()
            print("Cleared the persistent BM25 product index.")

        processed = 0
        skipped_empty = 0
        seen_ids: set[str] = set()
        started_at = time.monotonic()
        batch = IngestionBatch(
            collection,
            bm25_index,
            ensure_consistency=ensure_consistency_gate,
            row_changes=database_row_changes,
            embedder=embed_for_upsert,
            format_duration=format_duration,
            embedding_revision=active_embedding_revision,
            force_reembed=force_reembed,
            embed_batch_size=embed_batch_size,
            started_at=started_at,
        )

        for row in iter_database_rows(
            content_column,
            detected_primary_key,
            limit,
            mysql_config,
            fetch_batch_size=batch_size,
        ):
            index_row = canonicalize_search_ready_row(
                row,
                getattr(tenant, "ingestion", TenantIngestionConfig()),
            )
            prepared = prepare_mysql_row(
                index_row,
                content_column,
                detected_primary_key,
                mysql_config=mysql_config,
                company_id=tenant.company_id if tenant else None,
                embedding_model_revision=active_embedding_revision,
            )
            if prepared is None:
                skipped_empty += 1
                continue

            doc_id, document, metadata = prepared
            seen_ids.add(str(doc_id))
            bm25_row = prepare_bm25_index_row(
                index_row,
                bm25_column,
                detected_primary_key,
                mysql_config=mysql_config,
                company_id=tenant.company_id if tenant else None,
            )
            batch.add(doc_id, document, metadata, bm25_row)
            processed += 1
            if len(batch) >= batch_size:
                batch.flush(processed, planned_rows)

        batch.flush(processed, planned_rows)
        deleted_vectors = 0
        deleted_bm25 = 0
        if reconcile_deletions:
            ensure_consistency_gate()
            deleted_vectors, deleted_bm25 = reconcile_deleted_documents(
                collection,
                bm25_index,
                source_name,
                seen_ids,
            )
            print(
                "Deletion reconciliation complete. "
                f"Removed {deleted_vectors} vectors and "
                f"{deleted_bm25} BM25 rows.",
                flush=True,
            )
        bm25_count = bm25_index.count()
        print(
            f"\n{database_backend(mysql_config).title()} ingestion complete. "
            f"Indexed/updated {batch.indexed} rows; "
            f"metadata-updated {batch.metadata_updated} rows; "
            f"skipped unchanged {batch.skipped_current} rows; "
            f"skipped empty {skipped_empty} rows. "
            f"Collection contains {collection.count()} chunks. "
            f"BM25 index contains {bm25_count} products."
        )
    finally:
        bm25_index.close()


def ingest_mysql_source(
    limit: int | None = None,
    batch_size: int = MYSQL_BATCH_SIZE,
    embed_batch_size: int = EMBED_BATCH_SIZE,
    primary_key_column: str | None = None,
    replace_source: bool = False,
    force_reembed: bool = False,
    reconcile_deletions: bool = False,
    tenant: TenantProfile | None = None,
) -> None:
    state_path = ingestion_state_path(tenant.storage.bm25_path) if tenant else None
    try:
        _ingest_mysql_source(
            limit=limit,
            batch_size=batch_size,
            embed_batch_size=embed_batch_size,
            primary_key_column=primary_key_column,
            replace_source=replace_source,
            force_reembed=force_reembed,
            reconcile_deletions=reconcile_deletions,
            tenant=tenant,
        )
    except Exception as exc:
        if state_path is not None and state_path.exists():
            fail_ingestion(state_path, exc)
        raise
    else:
        if state_path is not None and state_path.exists():
            complete_ingestion(state_path)


def rebuild_mysql_bm25_index(
    limit: int | None = None,
    batch_size: int = MYSQL_BATCH_SIZE,
    primary_key_column: str | None = None,
    tenant: TenantProfile | None = None,
    *,
    inactive_generation: bool = False,
) -> None:
    if not inactive_generation:
        raise RuntimeError(
            "BM25-only rebuild is blocked for active indexes. Use the shadow "
            "ingestion workflow so the inactive generation is validated and "
            "atomically promoted."
        )
    if batch_size <= 0:
        raise RuntimeError("--mysql-batch-size must be greater than zero.")
    if limit is not None and limit <= 0:
        raise RuntimeError("--limit must be greater than zero.")
    if tenant is None:
        raise RuntimeError("Tenant profile is required for BM25 ingestion.")

    mysql_config = tenant.database if tenant else None
    content_column = (
        mysql_config.content_column if mysql_config else MYSQL_CONTENT_COLUMN
    )
    database = mysql_config.database if mysql_config else MYSQL_DATABASE
    table = mysql_config.search_table if mysql_config else MYSQL_TABLE
    columns = fetch_database_columns(mysql_config)
    if content_column not in columns:
        raise RuntimeError(
            f"Column '{content_column}' was not found in {database}.{table}."
        )
    detected_primary_key = detect_database_primary_key(
        columns,
        primary_key_column,
        mysql_config,
    )
    bm25_column = (
        mysql_config.bm25_column
        if mysql_config and mysql_config.bm25_column in columns
        else (MYSQL_BM25_COLUMN if MYSQL_BM25_COLUMN in columns else content_column)
    )
    row_count = count_database_rows(content_column, mysql_config)
    planned_rows = min(row_count, limit) if limit is not None else row_count

    index = PersistentBM25Index(tenant.storage.bm25_path)
    try:
        index.clear()
        batch = []
        processed = 0

        print(
            f"Rebuilding BM25 index from {database_backend(mysql_config)} "
            f"{database}.{table}"
        )
        if tenant:
            print(f"Company: {tenant.company_id}")
        print(f"BM25 column: {bm25_column}")
        print(f"Rows planned: {planned_rows}")

        for row in iter_database_rows(
            content_column,
            detected_primary_key,
            limit,
            mysql_config,
            fetch_batch_size=batch_size,
        ):
            entry = prepare_bm25_index_row(
                row,
                bm25_column,
                detected_primary_key,
                mysql_config=mysql_config,
                company_id=tenant.company_id if tenant else None,
            )
            if entry is None:
                continue
            batch.append(entry)
            processed += 1
            if len(batch) >= batch_size:
                index.upsert(batch)
                batch = []
                print(f"  Indexed {processed}/{planned_rows}", end="\r", flush=True)

        index.upsert(batch)
        count = index.count()
        print(f"\nBM25 rebuild complete. Indexed {count} products.")
    finally:
        index.close()
