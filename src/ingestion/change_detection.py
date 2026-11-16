"""Classify unchanged vectors, metadata-only updates, and changed documents."""

from dataclasses import dataclass

from core.settings import (
    EMBED_MODEL,
    EMBED_MODEL_REVISION,
)
from ingestion.documents import metadata_hash


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
