import requests

from settings import EMBED_MODEL, OLLAMA_BASE_URL


class OllamaProvider:
    def __init__(
        self,
        base_url: str = OLLAMA_BASE_URL,
        embedding_model: str = EMBED_MODEL,
    ):
        self.base_url = base_url.rstrip("/")
        self.embedding_model = embedding_model

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
                    "keep_alive": "30m",
                },
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Cannot get embeddings from Ollama at {self.base_url}. "
                f"Start Ollama and confirm '{self.embedding_model}' is installed."
            ) from exc

        embeddings = response.json().get("embeddings")
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
                    "keep_alive": "30m",
                    "options": {"temperature": temperature},
                },
                timeout=300,
            )
            response.raise_for_status()
            return response.json()["message"]["content"]
        except (requests.RequestException, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"Cannot extract a structured query with '{model}' at "
                f"{self.base_url}."
            ) from exc


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
