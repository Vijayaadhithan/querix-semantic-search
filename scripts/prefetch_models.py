import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ollama_client import preload_ollama_embedding
from reranker import load_reranker
from settings import EMBED_MODEL, RERANK_LOCAL_MODEL, RERANK_PROVIDER_ORDER


def main() -> None:
    print(f"Warming Ollama embedding model: {EMBED_MODEL}")
    preload_ollama_embedding()
    print(
        "Downloading/loading configured reranker chain: "
        + " -> ".join(RERANK_PROVIDER_ORDER)
    )
    print(f"Local reranker model, if enabled: {RERANK_LOCAL_MODEL}")
    ranker = load_reranker()
    print(f"Resolved reranker chain: {ranker.model_label}")
    print("Model prefetch complete.")


if __name__ == "__main__":
    main()
