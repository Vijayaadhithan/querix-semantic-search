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

SOURCE_FILES_DIR = PROJECT_ROOT / "data" / "raw_docs"
RAW_DOCS_DIR = SOURCE_FILES_DIR
CHROMA_DIR = PROJECT_ROOT / "storage" / "chroma"
BM25_INDEX_PATH = PROJECT_ROOT / "storage" / "bm25.sqlite3"
APP_NAME = CONFIG.get("app_name", "Local Data Assistant")
COLLECTION_NAME = CONFIG.get("collection_name", "local_data")

OLLAMA_BASE_URL = os.getenv(
    "OLLAMA_BASE_URL", "http://localhost:11434"
).rstrip("/")
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE")
if OLLAMA_KEEP_ALIVE is None:
    OLLAMA_KEEP_ALIVE = CONFIG.get("ollama", {}).get("keep_alive", -1)
EMBED_MODEL = CONFIG.get("embedding", {}).get("model", "embeddinggemma:latest")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_API_BASE_URL = os.getenv(
    "GEMINI_API_BASE_URL",
    "https://generativelanguage.googleapis.com/v1beta",
).rstrip("/")
QUERY_EXTRACT_CONFIG = CONFIG.get("query_extraction", {})
_query_extract_models = QUERY_EXTRACT_CONFIG.get("models")
if not _query_extract_models:
    _query_extract_models = [
        QUERY_EXTRACT_CONFIG.get("model", "gemma-4-26b-a4b-it")
    ]
elif isinstance(_query_extract_models, str):
    _query_extract_models = [_query_extract_models]
QUERY_EXTRACT_MODELS = tuple(
    str(model).strip()
    for model in _query_extract_models
    if str(model).strip()
)
if not QUERY_EXTRACT_MODELS:
    raise ValueError("query_extraction.models must contain at least one model.")
QUERY_EXTRACT_MODEL = QUERY_EXTRACT_MODELS[0]
QUERY_EXTRACT_TEMPERATURE = float(
    QUERY_EXTRACT_CONFIG.get("temperature", 0)
)

CHUNK_SIZE = int(CONFIG.get("chunking", {}).get("chunk_size", 512))
CHUNK_OVERLAP = int(CONFIG.get("chunking", {}).get("chunk_overlap", 80))

VECTOR_CANDIDATE_K = int(
    CONFIG.get("retrieval", {}).get("vector_candidate_k", 100)
)
VECTOR_TOP_K = int(CONFIG.get("retrieval", {}).get("vector_top_k", 15))
BM25_TOP_K = int(CONFIG.get("retrieval", {}).get("bm25_top_k", 15))
HYBRID_CANDIDATE_K = int(
    CONFIG.get("retrieval", {}).get("hybrid_candidate_k", 60)
)
RETRIEVAL_OVERFETCH_FACTOR = int(
    CONFIG.get("retrieval", {}).get("overfetch_factor", 2)
)
RRF_CONSTANT = int(CONFIG.get("retrieval", {}).get("rrf_constant", 60))
VECTOR_WEIGHT = float(CONFIG.get("retrieval", {}).get("vector_weight", 1.0))
BM25_WEIGHT = float(CONFIG.get("retrieval", {}).get("bm25_weight", 1.0))
CATEGORY_FALLBACK_WEIGHT = float(
    CONFIG.get("retrieval", {}).get("category_fallback_weight", 0.25)
)
SOFT_CATEGORY_BOOST = float(
    CONFIG.get("retrieval", {}).get("soft_category_boost", 0.005)
)
RERANK_TOP_K = int(CONFIG.get("retrieval", {}).get("final_top_k", 6))
RERANK_MODEL = CONFIG.get("retrieval", {}).get(
    "reranker_model", "Alibaba-NLP/gte-reranker-modernbert-base"
)
RERANK_BATCH_SIZE = int(
    CONFIG.get("retrieval", {}).get("reranker_batch_size", 4)
)
RERANK_MAX_LENGTH = int(
    CONFIG.get("retrieval", {}).get("reranker_max_length", 512)
)
RERANK_USE_FP16 = bool(
    CONFIG.get("retrieval", {}).get("reranker_use_fp16", False)
)

API_HOST = os.getenv(
    "API_HOST",
    str(CONFIG.get("api", {}).get("host", "127.0.0.1")),
)
API_PORT = int(
    os.getenv(
        "API_PORT",
        str(CONFIG.get("api", {}).get("port", 8000)),
    )
)
API_LOG_LEVEL = os.getenv(
    "API_LOG_LEVEL",
    str(CONFIG.get("api", {}).get("log_level", "info")),
).lower()
API_DEFAULT_PAGE_SIZE = int(
    CONFIG.get("api", {}).get("default_page_size", 20)
)
API_PRELOAD_RERANKER = bool(
    CONFIG.get("api", {}).get("preload_reranker", True)
)
API_PRELOAD_EMBEDDING = bool(
    CONFIG.get("api", {}).get("preload_embedding", True)
)
API_MAX_PAGE_SIZE = int(CONFIG.get("api", {}).get("max_page_size", 20))
API_MAX_RESULTS = int(CONFIG.get("api", {}).get("max_results", 60))
API_SESSION_TTL_SECONDS = int(
    CONFIG.get("api", {}).get("session_ttl_seconds", 600)
)
API_MAX_SESSIONS = int(CONFIG.get("api", {}).get("max_sessions", 500))
API_CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("API_CORS_ORIGINS", "").split(",")
    if origin.strip()
]

MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "rag_ht_test")
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_TABLE = os.getenv("MYSQL_TABLE", "ads_search_ready")
MYSQL_CONTENT_COLUMN = os.getenv("MYSQL_CONTENT_COLUMN", "embedding_content")
MYSQL_BM25_COLUMN = os.getenv("MYSQL_BM25_COLUMN", "bm25_content")
MYSQL_SEARCH_ID_COLUMN = os.getenv("MYSQL_SEARCH_ID_COLUMN", "id")
MYSQL_RESULT_TABLE = os.getenv("MYSQL_RESULT_TABLE", "ads")
MYSQL_RESULT_ID_COLUMN = os.getenv("MYSQL_RESULT_ID_COLUMN", "id")
