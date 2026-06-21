import argparse
import hashlib
import logging
from collections import Counter
from pathlib import Path

import chromadb
import requests
from pypdf import PdfReader

from settings import (
    CHROMA_DIR,
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    COLLECTION_NAME,
    EMBED_MODEL,
    OLLAMA_BASE_URL,
    RAW_DOCS_DIR,
)

EMBED_BATCH_SIZE = 32


class _PypdfRepairFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("Ignoring wrong pointing object")


logging.getLogger("pypdf._reader").addFilter(_PypdfRepairFilter())


def embed_texts(texts: list[str]) -> list[list[float]]:
    try:
        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": texts},
            timeout=300,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Cannot get embeddings from Ollama at {OLLAMA_BASE_URL}. "
            f"Start Ollama and confirm '{EMBED_MODEL}' is installed."
        ) from exc

    embeddings = response.json().get("embeddings")
    if not embeddings or len(embeddings) != len(texts):
        raise RuntimeError("Ollama returned an invalid embedding response.")
    return embeddings


def read_pdf(path: Path) -> tuple[list[dict], int, int]:
    reader = PdfReader(path)
    pages = []
    empty_pages = 0

    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append({"page": page_number, "text": text})
        else:
            empty_pages += 1

    return pages, empty_pages, len(reader.pages)


def chunk_text(text: str, chunk_size: int = 512, overlap: int = 80) -> list[str]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be at least zero and smaller than chunk_size")

    words = text.split()
    step = chunk_size - overlap
    return [
        " ".join(words[start : start + chunk_size])
        for start in range(0, len(words), step)
    ]


def chunk_id(filename: str, page: int, index: int) -> str:
    value = f"{filename}\0{page}\0{index}".encode()
    return hashlib.sha256(value).hexdigest()


def prepare_pdf(path: Path) -> tuple[list[str], list[str], list[dict], int, int]:
    pages, empty_pages, page_count = read_pdf(path)
    ids = []
    documents = []
    metadatas = []

    for page in pages:
        for index, text in enumerate(
            chunk_text(page["text"], CHUNK_SIZE, CHUNK_OVERLAP)
        ):
            ids.append(chunk_id(path.name, page["page"], index))
            documents.append(text)
            metadatas.append(
                {
                    "source_file": path.name,
                    "page": page["page"],
                    "chunk_index": index,
                    "embedding_model": EMBED_MODEL,
                }
            )

    return ids, documents, metadatas, empty_pages, page_count


def find_pdfs() -> list[Path]:
    return sorted(RAW_DOCS_DIR.glob("*.pdf"), key=lambda path: path.name.lower())


def get_collection(create: bool = False):
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    if create:
        collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        return client, collection
    try:
        return client, client.get_collection(COLLECTION_NAME)
    except Exception as exc:
        raise RuntimeError("No vector collection found. Run: python src/ingest.py") from exc


def list_indexed_documents() -> None:
    _, collection = get_collection()
    data = collection.get(include=["metadatas"])
    counts = Counter(metadata["source_file"] for metadata in data["metadatas"])

    print(f"Collection: {COLLECTION_NAME} ({collection.count()} chunks)\n")
    for filename, count in sorted(counts.items(), key=lambda item: item[0].lower()):
        print(f"{count:>5} chunks  {filename}")


def confirm(message: str, assume_yes: bool = False) -> bool:
    return assume_yes or input(f"{message} [y/N]: ").strip().lower() in {"y", "yes"}


def delete_indexed_document(filename: str, assume_yes: bool = False) -> None:
    _, collection = get_collection()
    existing = collection.get(where={"source_file": filename}, include=[])
    count = len(existing["ids"])
    if not count:
        raise RuntimeError(f"'{filename}' is not present in the collection.")
    if not confirm(
        f"Delete {count} indexed chunks for '{filename}'? The PDF will be kept.",
        assume_yes,
    ):
        print("Cancelled.")
        return
    collection.delete(where={"source_file": filename})
    print(f"Deleted {count} chunks for '{filename}'.")


def clear_collection(assume_yes: bool = False) -> None:
    client, collection = get_collection()
    count = collection.count()
    if not confirm(
        f"Delete the entire '{COLLECTION_NAME}' collection ({count} chunks)?",
        assume_yes,
    ):
        print("Cancelled.")
        return
    client.delete_collection(COLLECTION_NAME)
    print(f"Deleted collection '{COLLECTION_NAME}'. Original PDFs were kept.")


def source_is_current(
    collection, filename: str, ids: list[str], documents: list[str]
) -> bool:
    existing = collection.get(
        where={"source_file": filename},
        include=["documents", "metadatas"],
    )
    if len(existing["ids"]) != len(ids):
        return False

    stored_documents = dict(zip(existing["ids"], existing["documents"]))
    expected_documents = dict(zip(ids, documents))
    models_match = all(
        metadata.get("embedding_model") == EMBED_MODEL
        for metadata in existing["metadatas"]
    )
    return models_match and stored_documents == expected_documents


def check_documents(pdf_files: list[Path]) -> bool:
    valid = True
    total_pages = 0
    total_chunks = 0
    for path in pdf_files:
        try:
            ids, _, _, empty_pages, page_count = prepare_pdf(path)
            total_pages += page_count
            total_chunks += len(ids)
            print(
                f"OK: {path.name} | {page_count} pages | {len(ids)} chunks | "
                f"{empty_pages} empty pages"
            )
        except Exception as exc:
            valid = False
            print(f"ERROR: {path.name} | {type(exc).__name__}: {exc}")

    print(f"\nChecked {len(pdf_files)} PDFs, {total_pages} pages, {total_chunks} chunks.")
    return valid


def ingest_documents(pdf_files: list[Path]) -> None:
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    _, collection = get_collection(create=True)

    for path in pdf_files:
        print(f"Processing: {path.name}")
        try:
            ids, documents, metadatas, empty_pages, _ = prepare_pdf(path)
            if not documents:
                print("  Skipped: no extractable text (OCR may be required).")
                continue
            if source_is_current(collection, path.name, ids, documents):
                print(f"  Unchanged: keeping {len(documents)} existing chunks.")
                continue

            embeddings = []
            for start in range(0, len(documents), EMBED_BATCH_SIZE):
                batch = documents[start : start + EMBED_BATCH_SIZE]
                embeddings.extend(embed_texts(batch))
                completed = min(start + len(batch), len(documents))
                print(
                    f"  Embedded {completed}/{len(documents)} chunks",
                    end="\r",
                    flush=True,
                )
            print()

            # Remove stale chunks only after extraction and embedding have succeeded.
            collection.delete(where={"source_file": path.name})
            collection.upsert(
                ids=ids,
                documents=documents,
                embeddings=embeddings,
                metadatas=metadatas,
            )
            print(f"  Added {len(documents)} chunks; skipped {empty_pages} empty pages.")
        except RuntimeError:
            raise
        except Exception as exc:
            print(f"  ERROR: {type(exc).__name__}: {exc}")

    print(f"\nIngestion complete. Collection contains {collection.count()} chunks.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest local PDFs into Chroma.")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--check", action="store_true", help="validate PDFs without calling Ollama"
    )
    actions.add_argument(
        "--list", action="store_true", help="list indexed documents and chunk counts"
    )
    actions.add_argument(
        "--delete", metavar="FILENAME", help="delete one document from the index"
    )
    actions.add_argument(
        "--clear", action="store_true", help="delete the entire vector collection"
    )
    parser.add_argument(
        "--yes", action="store_true", help="skip confirmation for delete operations"
    )
    args = parser.parse_args()

    if args.list:
        list_indexed_documents()
        return
    if args.delete:
        delete_indexed_document(args.delete, args.yes)
        return
    if args.clear:
        clear_collection(args.yes)
        return

    pdf_files = find_pdfs()
    if not pdf_files:
        raise SystemExit(f"No PDFs found in {RAW_DOCS_DIR}")

    if args.check:
        raise SystemExit(0 if check_documents(pdf_files) else 1)
    ingest_documents(pdf_files)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        raise SystemExit(f"Error: {exc}") from exc
