import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

load_dotenv(PROJECT_ROOT / ".env")


def _load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as config_file:
        return yaml.safe_load(config_file) or {}


CONFIG = _load_config()

RAW_DOCS_DIR = PROJECT_ROOT / "data" / "raw_docs"
CHROMA_DIR = PROJECT_ROOT / "storage" / "chroma"
COLLECTION_NAME = CONFIG.get("collection_name", "project_docs")

OLLAMA_BASE_URL = os.getenv(
    "OLLAMA_BASE_URL", "http://localhost:11434"
).rstrip("/")
EMBED_MODEL = CONFIG.get("embedding", {}).get("model", "embeddinggemma:latest")
LLM_MODEL = CONFIG.get("llm", {}).get("model", "gemma4:12b")
TEMPERATURE = float(CONFIG.get("llm", {}).get("temperature", 0.2))
LLM_THINK = bool(CONFIG.get("llm", {}).get("think", False))

CHUNK_SIZE = int(CONFIG.get("chunking", {}).get("chunk_size", 512))
CHUNK_OVERLAP = int(CONFIG.get("chunking", {}).get("chunk_overlap", 80))

VECTOR_TOP_K = int(CONFIG.get("retrieval", {}).get("vector_top_k", 15))
BM25_TOP_K = int(CONFIG.get("retrieval", {}).get("bm25_top_k", 15))
RERANK_TOP_K = int(CONFIG.get("retrieval", {}).get("final_top_k", 6))
