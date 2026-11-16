"""Bounded incremental ingestion batch writing and progress accounting."""

import time


class IngestionBatch:
    """Apply bounded vector/metadata/BM25 batches and retain progress counts."""

    def __init__(
        self,
        collection,
        bm25_index,
        *,
        ensure_consistency,
        row_changes,
        embedder,
        format_duration,
        embedding_revision: str,
        force_reembed: bool,
        embed_batch_size: int,
        started_at: float,
    ):
        self.collection = collection
        self.bm25_index = bm25_index
        self.ensure_consistency = ensure_consistency
        self.row_changes = row_changes
        self.embedder = embedder
        self.format_duration = format_duration
        self.embedding_revision = embedding_revision
        self.force_reembed = force_reembed
        self.embed_batch_size = embed_batch_size
        self.started_at = started_at
        self.ids: list[str] = []
        self.documents: list[str] = []
        self.metadatas: list[dict] = []
        self.bm25_rows: list[dict] = []
        self.indexed = 0
        self.skipped_current = 0
        self.metadata_updated = 0

    def add(
        self, doc_id: str, document: str, metadata: dict, bm25_row: dict | None
    ) -> None:
        self.ids.append(doc_id)
        self.documents.append(document)
        self.metadatas.append(metadata)
        if bm25_row is not None:
            self.bm25_rows.append(bm25_row)

    def __len__(self) -> int:
        return len(self.documents)

    def flush(self, processed: int, planned_rows: int) -> None:
        if not self.documents:
            return
        batch_start = processed - len(self.documents) + 1
        batch_end = processed
        total_label = planned_rows if planned_rows else "unknown"
        if self.force_reembed:
            metadata_only_ids: set[str] = set()
            upsert_ids = self.ids
            upsert_documents = self.documents
            upsert_metadatas = self.metadatas
        else:
            changes = self.row_changes(
                self.collection,
                self.ids,
                self.documents,
                self.metadatas,
                self.embedding_revision,
            )
            current_ids = changes.current_ids
            metadata_only_ids = changes.metadata_only_ids
            self.skipped_current += len(current_ids)
            upsert_ids = []
            upsert_documents = []
            upsert_metadatas = []
            for doc_id, document, metadata in zip(
                self.ids, self.documents, self.metadatas
            ):
                if doc_id in current_ids or doc_id in metadata_only_ids:
                    continue
                upsert_ids.append(doc_id)
                upsert_documents.append(document)
                upsert_metadatas.append(metadata)

        if upsert_documents or metadata_only_ids:
            self.ensure_consistency()

        if metadata_only_ids:
            metadata_ids = []
            updated_metadatas = []
            for doc_id, metadata in zip(self.ids, self.metadatas):
                if doc_id in metadata_only_ids:
                    metadata_ids.append(doc_id)
                    updated_metadatas.append(metadata)
            self.collection.update_metadatas(
                ids=metadata_ids,
                metadatas=updated_metadatas,
            )
            self.metadata_updated += len(metadata_ids)

        if not upsert_documents:
            self.bm25_index.upsert(self.bm25_rows)
            print(
                f"  Rows {batch_start}-{batch_end}/{total_label} "
                "unchanged for embeddings; "
                f"metadata-updated {len(metadata_only_ids)}.",
                flush=True,
            )
            self.ids = []
            self.documents = []
            self.metadatas = []
            self.bm25_rows = []
            return

        print(
            f"  Preparing rows {batch_start}-{batch_end}/{total_label} for "
            "pgvector "
            f"({len(upsert_documents)} changed/new)",
            flush=True,
        )
        embeddings = self.embedder(
            upsert_documents,
            self.embed_batch_size,
            progress_prefix=f"    rows {batch_start}-{batch_end}",
        )
        self.collection.upsert(
            ids=upsert_ids,
            documents=upsert_documents,
            embeddings=embeddings,
            metadatas=upsert_metadatas,
        )
        self.bm25_index.upsert(self.bm25_rows)
        self.indexed += len(upsert_documents)
        elapsed = time.monotonic() - self.started_at
        rate = processed / elapsed if elapsed else 0
        remaining = max(planned_rows - processed, 0)
        eta = remaining / rate if rate else 0
        print(
            f"  Indexed/updated {self.indexed} rows; "
            f"skipped unchanged {self.skipped_current}; "
            f"processed {processed}/{total_label}; ETA {self.format_duration(eta)}",
            flush=True,
        )
        self.ids = []
        self.documents = []
        self.metadatas = []
        self.bm25_rows = []
