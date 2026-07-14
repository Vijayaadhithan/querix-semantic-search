import re

import requests

from settings import (
    EMBED_MODEL,
    OLLAMA_BASE_URL,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_QUERY_TIMEOUT_SECONDS,
)


def normalize_keep_alive(value: str | int) -> str | int:
    """Convert integer-looking environment values to JSON numbers.

    dotenv/environment values are always strings. Ollama accepts a numeric
    negative value such as -1, or a duration string such as "-1m", but the
    bare string "-1" is rejected by newer API versions.
    """
    if isinstance(value, int):
        return value
    normalized = str(value).strip()
    if re.fullmatch(r"[+-]?\d+", normalized):
        return int(normalized)
    return normalized


def ollama_error_detail(exc: requests.RequestException) -> str:
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    try:
        error = response.json().get("error")
    except (AttributeError, TypeError, ValueError):
        return ""
    if not isinstance(error, str) or not error.strip():
        return ""
    return f" Ollama error: {error.strip()[:300]}"


def ollama_timing_metrics(payload: dict) -> dict[str, float]:
    fields = (
        "total_duration",
        "load_duration",
        "prompt_eval_duration",
        "eval_duration",
    )
    return {
        field.removesuffix("_duration") + "_ms": (
            float(payload.get(field, 0)) / 1_000_000
        )
        for field in fields
    }


class OllamaProvider:
    def __init__(
        self,
        base_url: str = OLLAMA_BASE_URL,
        embedding_model: str = EMBED_MODEL,
        keep_alive: str | int = OLLAMA_KEEP_ALIVE,
    ):
        self.base_url = base_url.rstrip("/")
        self.embedding_model = embedding_model
        self.keep_alive = normalize_keep_alive(keep_alive)
        self.last_embedding_metrics: dict[str, float] = {}

    def embed_text(self, text: str) -> list[float]:
        return self.embed_texts(
            [text],
            timeout=OLLAMA_QUERY_TIMEOUT_SECONDS,
        )[0]

    def embed_texts(
        self,
        texts: list[str],
        timeout: int = 300,
    ) -> list[list[float]]:
        try:
            response = requests.post(
                f"{self.base_url}/api/embed",
                json={
                    "model": self.embedding_model,
                    "input": texts,
                    "keep_alive": self.keep_alive,
                },
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Cannot get embeddings from Ollama at {self.base_url}. "
                f"Start Ollama and confirm '{self.embedding_model}' is installed."
                f"{ollama_error_detail(exc)}"
            ) from exc

        payload = response.json()
        self.last_embedding_metrics = ollama_timing_metrics(payload)
        embeddings = payload.get("embeddings")
        if not embeddings or len(embeddings) != len(texts):
            raise RuntimeError("Ollama returned an invalid embedding response.")
        return embeddings

    def preload_embedding_model(self) -> dict:
        self.embed_texts(["startup warmup"], timeout=300)
        return dict(self.last_embedding_metrics)


DEFAULT_OLLAMA_PROVIDER = OllamaProvider()


def embed_text(text: str) -> list[float]:
    return DEFAULT_OLLAMA_PROVIDER.embed_text(text)


def embed_texts(texts: list[str], timeout: int = 300) -> list[list[float]]:
    return DEFAULT_OLLAMA_PROVIDER.embed_texts(texts, timeout=timeout)


def preload_ollama_embedding() -> dict[str, dict]:
    embedding = DEFAULT_OLLAMA_PROVIDER.preload_embedding_model()
    return {
        "embedding_model": {
            "model": EMBED_MODEL,
            **embedding,
        },
    }


def last_ollama_embedding_metrics() -> dict[str, float]:
    return dict(DEFAULT_OLLAMA_PROVIDER.last_embedding_metrics)
