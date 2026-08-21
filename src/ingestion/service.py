import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from core.settings import (
    EMBED_MODEL,
    EMBED_MODEL_REVISION,
    MYSQL_BM25_COLUMN,
    MYSQL_CONTENT_COLUMN,
    MYSQL_DATABASE,
    MYSQL_TABLE,
)
from core.tenant_config import TenantIngestionConfig, TenantProfile
from ingestion.documents import metadata_hash, prepare_bm25_index_row, prepare_mysql_row
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


@dataclass(frozen=True)
class DatabaseRowChanges:
    current_ids: set[str]
    metadata_only_ids: set[str]


def database_row_changes(
    collection,
    ids: list[str],
    documents: list[str],
    metadatas: list[dict],
    embedding_model_revision: str = EMBED_MODEL_REVISION,
) -> DatabaseRowChanges:
    """Classify rows without re-embedding metadata-only source changes."""
    if not ids:
        return DatabaseRowChanges(set(), set())
    existing = collection.get(ids=ids, include=["documents", "metadatas"])
    expected = {
        doc_id: {
            "document": document,
            "hash": metadata.get("source_content_hash"),
            "metadata": metadata,
        }
        for doc_id, document, metadata in zip(ids, documents, metadatas)
    }
    current: set[str] = set()
    metadata_only: set[str] = set()
    for doc_id, document, metadata in zip(
        existing["ids"], existing["documents"], existing["metadatas"]
    ):
        if metadata.get("embedding_model") != EMBED_MODEL:
            continue
        stored_revision = metadata.get("embedding_model_revision")
        if stored_revision not in {None, embedding_model_revision}:
            continue
        expected_row = expected.get(doc_id, {})
        content_is_current = metadata.get("source_content_hash") == expected_row.get(
            "hash"
        ) or document == expected_row.get("document")
        if not content_is_current:
            continue
        expected_metadata = expected_row.get("metadata", {})
        expected_metadata_hash = expected_metadata.get("source_metadata_hash")
        stored_metadata_hash = metadata.get("source_metadata_hash") or metadata_hash(
            metadata
        )
        if (
            stored_revision == embedding_model_revision
            and stored_metadata_hash == expected_metadata_hash
            and metadata.get("source_metadata_hash") == expected_metadata_hash
        ):
            current.add(doc_id)
        else:
            metadata_only.add(doc_id)
    return DatabaseRowChanges(current, metadata_only)


def database_current_ids(
    collection,
    ids: list[str],
    documents: list[str],
    metadatas: list[dict],
    embedding_model_revision: str = EMBED_MODEL_REVISION,
) -> set[str]:
    changes = database_row_changes(
        collection,
        ids,
        documents,
        metadatas,
        embedding_model_revision,
    )
    return changes.current_ids | changes.metadata_only_ids


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
            print(f"Deleted {len(existing['ids'])} existing chunks for {source_name}.")
        bm25_index.clear()
        print("Cleared the persistent BM25 product index.")

    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []
    bm25_rows: list[dict] = []
    indexed = 0
    processed = 0
    skipped_empty = 0
    skipped_current = 0
    metadata_updated = 0
    seen_ids: set[str] = set()
    started_at = time.monotonic()

    def flush_batch() -> None:
        nonlocal \
            ids, \
            documents, \
            metadatas, \
            bm25_rows, \
            indexed, \
            skipped_current, \
            metadata_updated
        if not documents:
            return
        batch_start = processed - len(documents) + 1
        batch_end = processed
        total_label = planned_rows if planned_rows else "unknown"
        if force_reembed:
            metadata_only_ids: set[str] = set()
            upsert_ids = ids
            upsert_documents = documents
            upsert_metadatas = metadatas
        else:
            changes = database_row_changes(
                collection,
                ids,
                documents,
                metadatas,
                active_embedding_revision,
            )
            current_ids = changes.current_ids
            metadata_only_ids = changes.metadata_only_ids
            skipped_current += len(current_ids)
            upsert_ids = []
            upsert_documents = []
            upsert_metadatas = []
            for doc_id, document, metadata in zip(ids, documents, metadatas):
                if doc_id in current_ids or doc_id in metadata_only_ids:
                    continue
                upsert_ids.append(doc_id)
                upsert_documents.append(document)
                upsert_metadatas.append(metadata)

        if upsert_documents or metadata_only_ids:
            ensure_consistency_gate()

        if metadata_only_ids:
            metadata_ids = []
            updated_metadatas = []
            for doc_id, metadata in zip(ids, metadatas):
                if doc_id in metadata_only_ids:
                    metadata_ids.append(doc_id)
                    updated_metadatas.append(metadata)
            collection.update_metadatas(
                ids=metadata_ids,
                metadatas=updated_metadatas,
            )
            metadata_updated += len(metadata_ids)

        if not upsert_documents:
            bm25_index.upsert(bm25_rows)
            print(
                f"  Rows {batch_start}-{batch_end}/{total_label} "
                "unchanged for embeddings; "
                f"metadata-updated {len(metadata_only_ids)}.",
                flush=True,
            )
            ids = []
            documents = []
            metadatas = []
            bm25_rows = []
            return

        print(
            f"  Preparing rows {batch_start}-{batch_end}/{total_label} for "
            "pgvector "
            f"({len(upsert_documents)} changed/new)",
            flush=True,
        )
        embeddings = embed_for_upsert(
            upsert_documents,
            embed_batch_size,
            progress_prefix=f"    rows {batch_start}-{batch_end}",
        )
        collection.upsert(
            ids=upsert_ids,
            documents=upsert_documents,
            embeddings=embeddings,
            metadatas=upsert_metadatas,
        )
        bm25_index.upsert(bm25_rows)
        indexed += len(upsert_documents)
        elapsed = time.monotonic() - started_at
        rate = processed / elapsed if elapsed else 0
        remaining = max(planned_rows - processed, 0)
        eta = remaining / rate if rate else 0
        print(
            f"  Indexed/updated {indexed} rows; skipped unchanged {skipped_current}; "
            f"processed {processed}/{total_label}; ETA {format_duration(eta)}",
            flush=True,
        )
        ids = []
        documents = []
        metadatas = []
        bm25_rows = []

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
        ids.append(doc_id)
        documents.append(document)
        metadatas.append(metadata)
        bm25_row = prepare_bm25_index_row(
            index_row,
            bm25_column,
            detected_primary_key,
            mysql_config=mysql_config,
            company_id=tenant.company_id if tenant else None,
        )
        if bm25_row is not None:
            bm25_rows.append(bm25_row)
        processed += 1
        if len(documents) >= batch_size:
            flush_batch()

    flush_batch()
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
    bm25_index.close()
    print(
        f"\n{database_backend(mysql_config).title()} ingestion complete. "
        f"Indexed/updated {indexed} rows; "
        f"metadata-updated {metadata_updated} rows; "
        f"skipped unchanged {skipped_current} rows; "
        f"skipped empty {skipped_empty} rows. "
        f"Collection contains {collection.count()} chunks. "
        f"BM25 index contains {bm25_count} products."
    )


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
) -> None:
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
    index.close()
    print(f"\nBM25 rebuild complete. Indexed {count} products.")
