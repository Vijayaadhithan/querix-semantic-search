import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ollama_client import preload_ollama_embedding
from reranker import load_reranker
from settings import EMBED_MODEL, RERANK_MODEL


def main() -> None:
    print(f"Warming Ollama embedding model: {EMBED_MODEL}")
    preload_ollama_embedding()
    print(f"Downloading/loading reranker: {RERANK_MODEL}")
    load_reranker()
    print("Model prefetch complete.")


if __name__ == "__main__":
    main()
