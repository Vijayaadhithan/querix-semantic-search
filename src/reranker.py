import threading
import time
from contextlib import contextmanager
import logging
from collections import deque

import requests

from settings import (
    LANGSEARCH_API_KEY,
    LANGSEARCH_RERANK_MODEL,
    LANGSEARCH_RERANK_RPD,
    LANGSEARCH_RERANK_RPM,
    LANGSEARCH_RERANK_RPS,
    LANGSEARCH_RERANK_URL,
    RERANK_API_TIMEOUT_SECONDS,
    RERANK_FAILURE_COOLDOWN_SECONDS,
    RERANK_MAX_DOCUMENT_CHARS,
    RERANK_MODEL,
    RERANK_PROVIDER_ORDER,
    RERANK_RATE_LIMIT_COOLDOWN_SECONDS,
    VOYAGE_API_KEY,
    VOYAGE_RERANK_LITE_MODEL,
    VOYAGE_RERANK_MODEL,
    VOYAGE_RERANK_RPM_PER_MODEL,
    VOYAGE_RERANK_URL,
)

LOGGER = logging.getLogger("uvicorn.error")


class RequestWindowLimiter:
    """Thread-safe rolling request budget for a configurable time window."""

    def __init__(
        self,
        requests_per_window: int,
        *,
        window_seconds: float = 60,
        clock=time.monotonic,
    ):
        if requests_per_window <= 0 or window_seconds <= 0:
            raise ValueError("request budget and window must be greater than zero")
        self.requests_per_window = requests_per_window
        self.window_seconds = window_seconds
        self.clock = clock
        self._requests = deque()
        self._lock = threading.Lock()

    def allow(self) -> tuple[bool, float]:
        now = self.clock()
        cutoff = now - self.window_seconds
        with self._lock:
            while self._requests and self._requests[0] <= cutoff:
                self._requests.popleft()
            if len(self._requests) >= self.requests_per_window:
                return False, max(
                    self.window_seconds - (now - self._requests[0]),
                    0,
                )
            self._requests.append(now)
            return True, 0.0


class SharedReranker:
    """Loads one reranker instance for every tenant engine in the process."""

    def __init__(self, loader=None):
        self.loader = loader or load_reranker
        self.ranker = None
        self._lock = threading.Lock()
        self._inference_lock = threading.Lock()

    def ensure(self):
        if self.ranker is not None:
            return self.ranker, 0.0
        with self._lock:
            if self.ranker is not None:
                return self.ranker, 0.0
            started = time.perf_counter()
            self.ranker = self.loader()
            return self.ranker, time.perf_counter() - started

    @contextmanager
    def inference_guard(self):
        if getattr(self.ranker, "supports_parallel", False):
            yield
            return
        with self._inference_lock:
            yield


class HostedReranker:
    def __init__(
        self,
        *,
        name: str,
        url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = RERANK_API_TIMEOUT_SECONDS,
        max_document_chars: int = RERANK_MAX_DOCUMENT_CHARS,
        provider_name: str | None = None,
        requests_per_minute: int | None = None,
        requests_per_second: int | None = None,
        requests_per_day: int | None = None,
        clock=time.monotonic,
    ):
        self.name = name
        self.provider_name = provider_name or name
        self.url = url
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_document_chars = max_document_chars
        self.request_limiters = []
        for budget, window_seconds in (
            (requests_per_second, 1),
            (requests_per_minute, 60),
            (requests_per_day, 86400),
        ):
            if budget is not None:
                self.request_limiters.append(
                    RequestWindowLimiter(
                        budget,
                        window_seconds=window_seconds,
                        clock=clock,
                    )
                )
        self.clock = clock
        self._cooldown_lock = threading.Lock()
        self._unavailable_until = 0.0
        self._state = threading.local()

    @property
    def last_usage(self) -> dict[str, int]:
        return dict(getattr(self._state, "last_usage", {}))

    def _payload(self, query: str, documents: list[str]) -> dict:
        if self.provider_name == "voyage":
            return {
                "model": self.model,
                "query": query,
                "documents": documents,
                "return_documents": False,
                "truncation": True,
            }
        return {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_n": len(documents),
            "return_documents": False,
        }

    def _cooldown_retry_after(self) -> float:
        now = self.clock()
        with self._cooldown_lock:
            return max(self._unavailable_until - now, 0.0)

    def _set_cooldown(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._cooldown_lock:
            self._unavailable_until = max(
                self._unavailable_until,
                self.clock() + seconds,
            )

    @staticmethod
    def _retry_after_seconds(response) -> float | None:
        if response is None:
            return None
        try:
            retry_after = response.headers.get("Retry-After")
        except AttributeError:
            return None
        if retry_after is None:
            return None
        try:
            return max(float(retry_after), 0.0)
        except ValueError:
            return None

    def compute_score(self, pairs):
        self._state.last_usage = {}
        if not pairs:
            return []
        retry_after = self._cooldown_retry_after()
        if retry_after > 0:
            raise RuntimeError(
                f"{self.name} provider cooldown active; "
                f"retry_after={retry_after:.1f}s"
            )
        queries = {str(pair[0]) for pair in pairs}
        if len(queries) != 1:
            raise RuntimeError(
                f"{self.name} reranker requires one query per request."
            )
        query = str(pairs[0][0])
        documents = [
            str(pair[1])[: self.max_document_chars]
            for pair in pairs
        ]
        for request_limiter in self.request_limiters:
            allowed, retry_after = request_limiter.allow()
            if not allowed:
                raise RuntimeError(
                    f"{self.name} provider request budget exhausted; "
                    f"retry_after={retry_after:.1f}s"
                )
        try:
            response = requests.post(
                self.url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=self._payload(query, documents),
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            usage = payload.get("usage") or {}
            total_tokens = int(usage.get("total_tokens", 0) or 0)
            self._state.last_usage = {
                "input_tokens": total_tokens,
                "output_tokens": 0,
                "total_tokens": total_tokens,
            }
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            reason = f"http_{status}" if status else type(exc).__name__
            if status == 429:
                self._set_cooldown(
                    self._retry_after_seconds(response)
                    or RERANK_RATE_LIMIT_COOLDOWN_SECONDS
                )
            elif status is not None:
                self._set_cooldown(RERANK_FAILURE_COOLDOWN_SECONDS)
            elif isinstance(exc, (requests.Timeout, requests.ConnectionError)):
                self._set_cooldown(RERANK_FAILURE_COOLDOWN_SECONDS)
            raise RuntimeError(
                f"{self.name} reranker unavailable: {reason}"
            ) from exc
        except ValueError as exc:
            raise RuntimeError(
                f"{self.name} reranker returned invalid JSON"
            ) from exc
        results = payload.get("data") or payload.get("results")
        if not isinstance(results, list):
            raise RuntimeError(
                f"{self.name} reranker response has no result list"
            )
        scores: list[float | None] = [None] * len(documents)
        for result in results:
            try:
                index = int(result["index"])
                score = float(
                    result.get(
                        "relevance_score",
                        result.get("score"),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"{self.name} reranker returned an invalid result"
                ) from exc
            if 0 <= index < len(scores):
                scores[index] = score
        if any(score is None for score in scores):
            raise RuntimeError(
                f"{self.name} reranker did not score every document"
            )
        return [float(score) for score in scores]


class FallbackReranker:
    supports_parallel = True

    def __init__(self, providers):
        if not providers:
            raise ValueError("At least one reranker provider is required.")
        self.providers = providers
        self._state = threading.local()

    @property
    def last_provider(self) -> str:
        return getattr(self._state, "last_provider", "")

    @property
    def last_attempts(self) -> list[dict]:
        return list(getattr(self._state, "last_attempts", []))

    @property
    def model_label(self) -> str:
        labels = []
        for provider in self.providers:
            model = getattr(provider, "model", RERANK_MODEL)
            labels.append(f"{provider.name}:{model}")
        return " -> ".join(labels)

    def compute_score(self, pairs):
        self._state.last_attempts = []
        self._state.last_provider = ""
        failures = []
        for provider in self.providers:
            started = time.perf_counter()
            try:
                scores = provider.compute_score(pairs)
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                reason = str(exc).split(":", 1)[-1].strip()
                self._state.last_attempts.append(
                    {
                        "provider": provider.name,
                        "model": getattr(provider, "model", RERANK_MODEL),
                        "status": "fallback",
                        "reason": reason,
                        "duration_ms": elapsed_ms,
                        "usage": getattr(provider, "last_usage", {}),
                    }
                )
                failures.append(f"{provider.name}={reason}")
                LOGGER.warning(
                    "step=reranker_provider status=fallback provider=%s "
                    "reason=%s duration_ms=%.0f",
                    provider.name,
                    reason,
                    elapsed_ms,
                )
                continue
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._state.last_provider = provider.name
            self._state.last_attempts.append(
                {
                    "provider": provider.name,
                    "model": getattr(provider, "model", RERANK_MODEL),
                    "status": "success",
                    "duration_ms": elapsed_ms,
                    "usage": getattr(provider, "last_usage", {}),
                }
            )
            LOGGER.info(
                "step=reranker_provider status=success provider=%s "
                "model=%s duration_ms=%.0f",
                provider.name,
                getattr(provider, "model", RERANK_MODEL),
                elapsed_ms,
            )
            return scores
        raise RuntimeError(
            "All reranker providers failed: " + "; ".join(failures)
        )


def load_reranker():
    providers = []
    for name in RERANK_PROVIDER_ORDER:
        if name == "langsearch":
            if LANGSEARCH_API_KEY:
                providers.append(
                    HostedReranker(
                        name="langsearch",
                        provider_name="langsearch",
                        url=LANGSEARCH_RERANK_URL,
                        api_key=LANGSEARCH_API_KEY,
                        model=LANGSEARCH_RERANK_MODEL,
                        requests_per_second=LANGSEARCH_RERANK_RPS,
                        requests_per_minute=LANGSEARCH_RERANK_RPM,
                        requests_per_day=LANGSEARCH_RERANK_RPD,
                    )
                )
            else:
                LOGGER.info(
                    "LangSearch reranker skipped because "
                    "LANGSEARCH_API_KEY is unset."
                )
        elif name in {"voyage", "voyage-2.5", "voyage-2.5-lite"}:
            if VOYAGE_API_KEY:
                model = (
                    VOYAGE_RERANK_LITE_MODEL
                    if name == "voyage-2.5-lite"
                    else VOYAGE_RERANK_MODEL
                )
                providers.append(
                    HostedReranker(
                        name=name,
                        provider_name="voyage",
                        url=VOYAGE_RERANK_URL,
                        api_key=VOYAGE_API_KEY,
                        model=model,
                        requests_per_minute=(
                            VOYAGE_RERANK_RPM_PER_MODEL
                        ),
                    )
                )
            else:
                LOGGER.info(
                    "Voyage reranker skipped because VOYAGE_API_KEY is unset."
                )
    if not providers:
        raise RuntimeError(
            "No hosted reranker is configured. Set LANGSEARCH_API_KEY or "
            "VOYAGE_API_KEY for a provider in RERANK_PROVIDER_ORDER."
        )
    return FallbackReranker(providers)


def rerank(
    query,
    candidates,
    ranker,
    top_k=6,
    diversity_top_k=None,
):
    if not candidates:
        return []

    pairs = [[query, candidate["text"]] for candidate in candidates]
    scores = ranker.compute_score(pairs)
    if isinstance(scores, (int, float)):
        scores = [scores]

    ranked = sorted(
        zip(candidates, scores),
        key=lambda item: float(item[1]),
        reverse=True,
    )
    diversity_top_k = top_k if diversity_top_k is None else diversity_top_k
    primary = []
    deferred = []
    seen_titles = set()
    for position, (candidate, score) in enumerate(ranked):
        metadata = candidate.get("metadata") or {}
        title = metadata.get("content_title") or metadata.get("title")
        normalized_title = " ".join(str(title).casefold().split()) if title else None
        if (
            len(primary) < diversity_top_k
            and normalized_title
            and normalized_title in seen_titles
        ):
            deferred.append((position, candidate, score))
            continue
        if normalized_title:
            seen_titles.add(normalized_title)
        if len(primary) < diversity_top_k:
            primary.append((position, candidate, score))
        else:
            deferred.append((position, candidate, score))

    ordered = primary + sorted(deferred, key=lambda item: item[0])
    results = []
    for _, candidate, score in ordered[:top_k]:
        results.append(
            {
                "id": candidate["id"],
                "text": candidate["text"],
                "metadata": candidate["metadata"],
                "score": float(score),
            }
        )
    return results
