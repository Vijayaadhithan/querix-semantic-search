from typing import Protocol


class EmbeddingProvider(Protocol):
    def embed_text(self, text: str) -> list[float]:
        ...

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        ...


class StructuredQueryProvider(Protocol):
    """Hosted or local provider that returns schema-constrained JSON."""

    def structured_chat(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict,
        temperature: float = 0,
    ) -> str:
        ...


class RerankingProvider(Protocol):
    def compute_score(
        self,
        pairs: list[list[str]],
        batch_size: int | None = None,
        max_length: int | None = None,
    ) -> list[float]:
        ...
