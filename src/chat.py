import json

import requests
import chromadb
from rank_bm25 import BM25Okapi
from flashrank import Ranker, RerankRequest

from settings import (
    BM25_TOP_K,
    CHROMA_DIR,
    COLLECTION_NAME,
    EMBED_MODEL,
    LLM_THINK,
    LLM_MODEL,
    OLLAMA_BASE_URL,
    RERANK_TOP_K,
    TEMPERATURE,
    VECTOR_TOP_K,
)


def embed_text(text: str):
    try:
        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": text},
            timeout=120,
        )
        response.raise_for_status()
        return response.json()["embeddings"][0]
    except (requests.RequestException, KeyError, IndexError) as exc:
        raise RuntimeError(
            f"Cannot get embeddings from Ollama at {OLLAMA_BASE_URL}. "
            f"Start Ollama and confirm '{EMBED_MODEL}' is installed."
        ) from exc


def ask_ollama(prompt: str):
    try:
        with requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json={
                "model": LLM_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a technical assistant. Answer only using the given "
                            "context. If the answer is absent, say you do not have enough "
                            "information. Always cite source file and page number."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "stream": True,
                "think": LLM_THINK,
                "keep_alive": "30m",
                "options": {"temperature": TEMPERATURE},
            },
            timeout=300,
            stream=True,
        ) as response:
            response.raise_for_status()
            received_content = False
            for line in response.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                content = chunk.get("message", {}).get("content", "")
                if content:
                    received_content = True
                    yield content
            if not received_content:
                raise RuntimeError("Ollama completed without returning answer text.")
    except (requests.RequestException, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Cannot chat with Ollama at {OLLAMA_BASE_URL}. "
            f"Start Ollama and confirm '{LLM_MODEL}' is installed."
        ) from exc


def load_collection():
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    try:
        return client.get_collection(COLLECTION_NAME)
    except Exception as exc:
        raise RuntimeError("No vector collection found. Run: python src/ingest.py") from exc


def get_all_docs(collection):
    data = collection.get(include=["documents", "metadatas"])
    docs = []

    for doc_id, text, meta in zip(data["ids"], data["documents"], data["metadatas"]):
        docs.append({
            "id": doc_id,
            "text": text,
            "metadata": meta
        })

    return docs


def bm25_search(query, docs, top_k=15):
    tokenized_docs = [d["text"].lower().split() for d in docs]
    bm25 = BM25Okapi(tokenized_docs)

    scores = bm25.get_scores(query.lower().split())
    ranked = sorted(
        zip(docs, scores),
        key=lambda x: x[1],
        reverse=True
    )

    return [
        {
            "id": item[0]["id"],
            "text": item[0]["text"],
            "metadata": item[0]["metadata"],
            "score": float(item[1]),
            "source": "bm25"
        }
        for item in ranked[:top_k]
    ]


def vector_search(query, collection, top_k=15):
    query_embedding = embed_text(query)

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(top_k, collection.count()),
        include=["documents", "metadatas", "distances"]
    )

    output = []

    for doc_id, text, meta, dist in zip(
        results["ids"][0],
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0]
    ):
        output.append({
            "id": doc_id,
            "text": text,
            "metadata": meta,
            "score": float(dist),
            "source": "vector"
        })

    return output


def merge_results(vector_results, bm25_results):
    merged = {}

    for item in vector_results + bm25_results:
        if item["id"] not in merged:
            merged[item["id"]] = item
        else:
            merged[item["id"]]["source"] += "+" + item["source"]

    return list(merged.values())


def rerank(query, candidates, ranker, top_k=6):
    passages = [
        {
            "id": c["id"],
            "text": c["text"],
            "metadata": c["metadata"]
        }
        for c in candidates
    ]

    request = RerankRequest(query=query, passages=passages)
    results = ranker.rerank(request)

    top = results[:top_k]

    return [
        {
            "id": r["id"],
            "text": r["text"],
            "metadata": r["metadata"],
            "score": r["score"]
        }
        for r in top
    ]


def build_prompt(question, contexts):
    context_text = ""

    for i, c in enumerate(contexts, start=1):
        meta = c["metadata"]
        context_text += f"\n[Source {i}]\n"
        context_text += f"File: {meta.get('source_file')}\n"
        context_text += f"Page: {meta.get('page')}\n"
        context_text += f"Content:\n{c['text']}\n"

    prompt = f"""
Question:
{question}

Context:
{context_text}

Instructions:
- Answer only from the context.
- If context is insufficient, say so.
- Cite sources like: (source_file, page number).
- Give a clear technical explanation.
"""
    return prompt


def main():
    collection = load_collection()
    docs = get_all_docs(collection)

    if not docs:
        print("No documents found. Run: python src/ingest.py")
        return

    print("Loading reranker...")
    ranker = Ranker()
    print("\nLocal RAG ready. Type 'exit' to quit.\n")

    while True:
        question = input("Ask: ").strip()

        if question.lower() in ["exit", "quit"]:
            break
        if not question:
            continue

        print("Searching and reranking...", end="", flush=True)
        vector_results = vector_search(question, collection, VECTOR_TOP_K)
        bm25_results = bm25_search(question, docs, BM25_TOP_K)

        merged = merge_results(vector_results, bm25_results)

        reranked = rerank(question, merged, ranker, RERANK_TOP_K)
        print(" done.", flush=True)

        prompt = build_prompt(question, reranked)

        print(f"\nAnswer (generating with {LLM_MODEL}):\n", flush=True)
        for content in ask_ollama(prompt):
            print(content, end="", flush=True)
        print()

        print("\nRetrieved sources:\n")
        for i, r in enumerate(reranked, start=1):
            meta = r["metadata"]
            print(
                f"{i}. {meta.get('source_file')} | page {meta.get('page')} | score {r.get('score')}"
            )

        print("\n" + "-" * 80 + "\n")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        raise SystemExit(f"Error: {exc}") from exc
