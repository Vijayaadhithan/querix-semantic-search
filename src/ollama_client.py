import requests

from settings import (
    EMBED_MODEL,
    OLLAMA_BASE_URL,
    OLLAMA_KEEP_ALIVE,
    QUERY_EXTRACT_MODEL,
)


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
        self.keep_alive = keep_alive
        self.last_chat_metrics: dict[str, float] = {}
        self.last_embedding_metrics: dict[str, float] = {}

    def embed_text(self, text: str) -> list[float]:
        return self.embed_texts([text], timeout=120)[0]

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
            ) from exc

        payload = response.json()
        self.last_embedding_metrics = ollama_timing_metrics(payload)
        embeddings = payload.get("embeddings")
        if not embeddings or len(embeddings) != len(texts):
            raise RuntimeError("Ollama returned an invalid embedding response.")
        return embeddings

    def structured_chat(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict,
        temperature: float = 0,
    ) -> str:
        try:
            response = requests.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "format": schema,
                    "stream": False,
                    "think": False,
                    "keep_alive": self.keep_alive,
                    "options": {"temperature": temperature},
                },
                timeout=300,
            )
            response.raise_for_status()
            payload = response.json()
            self.last_chat_metrics = ollama_timing_metrics(payload)
            return payload["message"]["content"]
        except (requests.RequestException, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"Cannot extract a structured query with '{model}' at "
                f"{self.base_url}."
            ) from exc

    def preload_chat_model(self, model: str = QUERY_EXTRACT_MODEL) -> dict:
        try:
            response = requests.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": model,
                    "stream": False,
                    "keep_alive": self.keep_alive,
                },
                timeout=300,
            )
            response.raise_for_status()
            return ollama_timing_metrics(response.json())
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Cannot preload Ollama query model '{model}' at "
                f"{self.base_url}."
            ) from exc

    def preload_embedding_model(self) -> dict:
        self.embed_texts(["startup warmup"], timeout=300)
        return dict(self.last_embedding_metrics)


DEFAULT_OLLAMA_PROVIDER = OllamaProvider()


def embed_text(text: str) -> list[float]:
    return DEFAULT_OLLAMA_PROVIDER.embed_text(text)


def embed_texts(texts: list[str], timeout: int = 300) -> list[list[float]]:
    return DEFAULT_OLLAMA_PROVIDER.embed_texts(texts, timeout=timeout)


def structured_chat(
    model: str,
    system_prompt: str,
    user_prompt: str,
    schema: dict,
    temperature: float = 0,
) -> str:
    return DEFAULT_OLLAMA_PROVIDER.structured_chat(
        model,
        system_prompt,
        user_prompt,
        schema,
        temperature,
    )


def preload_ollama_models() -> dict[str, dict]:
    # Load embeddings first and the query model last because query planning is
    # the first model-dependent stage for every new search.
    embedding = DEFAULT_OLLAMA_PROVIDER.preload_embedding_model()
    query = DEFAULT_OLLAMA_PROVIDER.preload_chat_model()
    return {
        "embedding_model": {
            "model": EMBED_MODEL,
            **embedding,
        },
        "query_model": {
            "model": QUERY_EXTRACT_MODEL,
            **query,
        },
    }


def last_ollama_metrics() -> dict[str, dict[str, float]]:
    return {
        "query_model": dict(DEFAULT_OLLAMA_PROVIDER.last_chat_metrics),
        "embedding_model": dict(DEFAULT_OLLAMA_PROVIDER.last_embedding_metrics),
    }
