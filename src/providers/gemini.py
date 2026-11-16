import json
import logging
import math
import threading
import time

from core.request_limit import RequestWindowLimiter, TokenBudgetLimiter
from core.settings import (
    GEMINI_QUERY_RPM,
    GROQ_QUERY_RPM,
    GROQ_QUERY_TPM,
    QUERY_EXTRACT_MAX_OUTPUT_TOKENS,
    QUERY_EXTRACT_MODELS,
    QUERY_EXTRACT_TOTAL_TIMEOUT_SECONDS,
    REDIS_ENABLED,
    REDIS_KEY_PREFIX,
    REDIS_URL,
)
from providers.google_query import (
    GeminiProvider,
)
from providers.groq_query import (
    GroqProvider,
)
from providers.query_transport import (
    _QUERY_DEADLINE,
    FALLBACK_HTTP_STATUSES,
    QueryModelUnavailableError,
    pooled_http_adapter,
    strip_json_fence,
    thread_http_session,
)
from storage.redis import RedisJsonCache

__all__ = (
    "DEFAULT_GEMINI_PROVIDER",
    "DEFAULT_GROQ_PROVIDER",
    "FALLBACK_HTTP_STATUSES",
    "GeminiProvider",
    "GroqProvider",
    "LOGGER",
    "QUERY_MODEL_LIMITERS_LOCK",
    "QUERY_PROVIDER_RATE_LIMIT_CACHE",
    "QueryModelUnavailableError",
    "estimate_groq_request_tokens",
    "last_gemini_metrics",
    "last_query_model_metrics",
    "pooled_http_adapter",
    "query_model_limiter",
    "query_model_route",
    "query_model_token_limiter",
    "strip_json_fence",
    "structured_chat",
    "thread_http_session",
)

LOGGER = logging.getLogger("uvicorn.error")


def _provider_rate_limit_cache():
    if not REDIS_ENABLED:
        return None
    try:
        return RedisJsonCache(REDIS_URL, REDIS_KEY_PREFIX)
    except RuntimeError as exc:
        LOGGER.warning("%s Query-provider limits will use process memory.", exc)
        return None


QUERY_PROVIDER_RATE_LIMIT_CACHE = _provider_rate_limit_cache()
QUERY_MODEL_LIMITERS: dict[str, RequestWindowLimiter] = {}
QUERY_MODEL_TOKEN_LIMITERS: dict[str, TokenBudgetLimiter] = {}
QUERY_MODEL_LIMITERS_LOCK = threading.Lock()


def query_model_route(model: str) -> tuple[str, str]:
    """Return the provider and provider-local model ID for a configured model."""
    normalized = str(model).strip()
    if normalized.startswith("groq:"):
        provider, provider_model = "groq", normalized.split(":", 1)[1]
    elif normalized.startswith("google:"):
        provider, provider_model = "google", normalized.split(":", 1)[1]
    elif ":" in normalized:
        raise ValueError(
            f"Unsupported query model provider prefix in {normalized!r}. "
            "Use groq: or google:."
        )
    else:
        # Keep unprefixed Gemini/Gemma IDs backwards-compatible. They are all
        # Google models and must share Google's request budget.
        provider, provider_model = "google", normalized
    if not provider_model:
        raise ValueError(f"Query model {normalized!r} has no model ID.")
    return provider, provider_model


def query_model_limiter(model: str) -> RequestWindowLimiter | None:
    provider, _provider_model = query_model_route(model)
    if provider == "groq":
        requests_per_minute = GROQ_QUERY_RPM
    elif provider == "google":
        requests_per_minute = GEMINI_QUERY_RPM
    else:
        return None
    with QUERY_MODEL_LIMITERS_LOCK:
        # Quotas are configured per provider, so every model routed to the
        # same provider must consume the same local/Redis request budget.
        limiter = QUERY_MODEL_LIMITERS.get(provider)
        if limiter is None:
            limiter = RequestWindowLimiter(
                requests_per_minute,
                redis_cache=QUERY_PROVIDER_RATE_LIMIT_CACHE,
                scope=f"query:{provider}",
            )
            QUERY_MODEL_LIMITERS[provider] = limiter
        return limiter


def query_model_token_limiter(model: str) -> TokenBudgetLimiter | None:
    provider, _provider_model = query_model_route(model)
    if provider != "groq":
        return None
    with QUERY_MODEL_LIMITERS_LOCK:
        limiter = QUERY_MODEL_TOKEN_LIMITERS.get(provider)
        if limiter is None:
            limiter = TokenBudgetLimiter(
                GROQ_QUERY_TPM,
                redis_cache=QUERY_PROVIDER_RATE_LIMIT_CACHE,
                scope=f"query:{provider}",
            )
            QUERY_MODEL_TOKEN_LIMITERS[provider] = limiter
        return limiter


def estimate_groq_request_tokens(
    system_prompt: str,
    user_prompt: str,
    schema: dict,
) -> int:
    """Conservatively reserve Groq input plus maximum generated tokens."""
    schema_text = json.dumps(
        schema,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    request_text = system_prompt + user_prompt + schema_text
    ascii_characters = sum(ord(character) < 128 for character in request_text)
    non_ascii_characters = len(request_text) - ascii_characters
    estimated_input_tokens = (
        math.ceil(ascii_characters / 4) + (non_ascii_characters * 3) + 64
    )
    return estimated_input_tokens + QUERY_EXTRACT_MAX_OUTPUT_TOKENS


# Backwards-compatible name for callers that imported the original exception.
GeminiModelUnavailableError = QueryModelUnavailableError


DEFAULT_GEMINI_PROVIDER = GeminiProvider()


DEFAULT_GROQ_PROVIDER = GroqProvider()


def structured_chat(
    model: str,
    system_prompt: str,
    user_prompt: str,
    schema: dict,
    temperature: float = 0,
) -> str:
    if not QUERY_EXTRACT_MODELS:
        raise RuntimeError("No query extraction models are configured.")
    models = QUERY_EXTRACT_MODELS if model == QUERY_EXTRACT_MODELS[0] else (model,)
    attempted_models: list[str] = []
    attempts: list[dict[str, object]] = []
    started = time.perf_counter()
    deadline = time.monotonic() + QUERY_EXTRACT_TOTAL_TIMEOUT_SECONDS
    last_error: QueryModelUnavailableError | None = None
    deadline_token = _QUERY_DEADLINE.set(deadline)

    def publish_metrics(
        current_attempt: dict[str, object] | None = None,
        *,
        failure_reason: str | None = None,
    ) -> None:
        aggregate = dict(current_attempt or {})
        aggregate.update(
            {
                "total_ms": (time.perf_counter() - started) * 1000,
                "attempted_models": list(attempted_models),
                "attempts": [dict(attempt) for attempt in attempts],
            }
        )
        if failure_reason:
            aggregate["failure_reason"] = failure_reason
        DEFAULT_GEMINI_PROVIDER.last_chat_metrics = aggregate

    try:
        for position, candidate_model in enumerate(models, start=1):
            attempted_models.append(candidate_model)
            provider_name, provider_model = query_model_route(candidate_model)
            provider = (
                DEFAULT_GROQ_PROVIDER
                if provider_name == "groq"
                else DEFAULT_GEMINI_PROVIDER
            )
            LOGGER.debug(
                "step=query_model status=attempt model=%s provider=%s position=%d/%d",
                candidate_model,
                provider_name,
                position,
                len(models),
            )
            if time.monotonic() >= deadline:
                last_error = QueryModelUnavailableError(
                    candidate_model,
                    provider=provider_name,
                    reason="total_deadline",
                )
                attempt_metrics = {
                    "load_ms": 0.0,
                    "total_ms": 0.0,
                    "model": candidate_model,
                    "provider": provider_name,
                    "status": "fallback",
                    "reason": last_error.reason,
                }
                attempts.append(attempt_metrics)
                publish_metrics(attempt_metrics, failure_reason=last_error.reason)
                break

            limiter = query_model_limiter(candidate_model)
            if limiter is not None:
                allowed, retry_after = limiter.allow()
                if not allowed:
                    last_error = QueryModelUnavailableError(
                        candidate_model,
                        provider=provider_name,
                        reason="local_rate_limit",
                    )
                    attempt_metrics = {
                        "load_ms": 0.0,
                        "total_ms": 0.0,
                        "model": candidate_model,
                        "provider": provider_name,
                        "status": "fallback",
                        "reason": last_error.reason,
                        "retry_after_seconds": retry_after,
                    }
                    attempts.append(attempt_metrics)
                    publish_metrics(
                        attempt_metrics,
                        failure_reason=last_error.reason,
                    )
                    LOGGER.info(
                        "step=query_model status=fallback model=%s reason=%s "
                        "retry_after=%.1fs next_model=%s",
                        candidate_model,
                        last_error.reason,
                        retry_after,
                        models[position] if position < len(models) else "none",
                    )
                    continue
            estimated_groq_tokens = None
            token_limiter = query_model_token_limiter(candidate_model)
            if token_limiter is not None:
                estimated_groq_tokens = estimate_groq_request_tokens(
                    system_prompt,
                    user_prompt,
                    schema,
                )
                allowed, retry_after = token_limiter.allow(estimated_groq_tokens)
                if not allowed:
                    last_error = QueryModelUnavailableError(
                        candidate_model,
                        provider=provider_name,
                        reason="local_token_limit",
                    )
                    attempt_metrics = {
                        "load_ms": 0.0,
                        "total_ms": 0.0,
                        "model": candidate_model,
                        "provider": provider_name,
                        "status": "fallback",
                        "reason": last_error.reason,
                        "estimated_tokens": estimated_groq_tokens,
                        "token_budget_per_minute": GROQ_QUERY_TPM,
                        "retry_after_seconds": retry_after,
                    }
                    attempts.append(attempt_metrics)
                    publish_metrics(
                        attempt_metrics,
                        failure_reason=last_error.reason,
                    )
                    LOGGER.info(
                        "step=query_model status=fallback model=%s reason=%s "
                        "estimated_tokens=%d retry_after=%.1fs next_model=%s",
                        candidate_model,
                        last_error.reason,
                        estimated_groq_tokens,
                        retry_after,
                        models[position] if position < len(models) else "none",
                    )
                    continue
            try:
                content = provider.structured_chat(
                    provider_model,
                    system_prompt,
                    user_prompt,
                    schema,
                    temperature,
                )
                attempt_metrics = {
                    **provider.last_chat_metrics,
                    "model": candidate_model,
                    "provider": provider_name,
                    "status": "success",
                    "attempt_number": position,
                }
                if estimated_groq_tokens is not None:
                    attempt_metrics.update(
                        {
                            "estimated_tokens": estimated_groq_tokens,
                            "token_budget_per_minute": GROQ_QUERY_TPM,
                        }
                    )
                attempts.append(attempt_metrics)
                publish_metrics(attempt_metrics)
                LOGGER.debug(
                    "step=query_model status=success model=%s duration_ms=%.0f",
                    candidate_model,
                    DEFAULT_GEMINI_PROVIDER.last_chat_metrics["total_ms"],
                )
                return content
            except QueryModelUnavailableError as exc:
                last_error = exc
                attempt_metrics = {
                    **provider.last_chat_metrics,
                    "model": candidate_model,
                    "provider": exc.provider or provider_name,
                    "status": "fallback",
                    "reason": exc.reason,
                    "attempt_number": position,
                }
                if estimated_groq_tokens is not None:
                    attempt_metrics.update(
                        {
                            "estimated_tokens": estimated_groq_tokens,
                            "token_budget_per_minute": GROQ_QUERY_TPM,
                        }
                    )
                attempts.append(attempt_metrics)
                publish_metrics(attempt_metrics, failure_reason=exc.reason)
                LOGGER.warning(
                    "step=query_model status=fallback model=%s reason=%s next_model=%s",
                    candidate_model,
                    exc.reason,
                    (models[position] if position < len(models) else "none"),
                )
            except RuntimeError:
                attempt_metrics = {
                    **provider.last_chat_metrics,
                    "model": candidate_model,
                    "provider": provider_name,
                    "status": "failed",
                    "reason": "provider_error",
                    "attempt_number": position,
                }
                attempts.append(attempt_metrics)
                publish_metrics(
                    attempt_metrics,
                    failure_reason="provider_error",
                )
                LOGGER.exception(
                    "step=query_model status=failed model=%s reason=provider_error",
                    candidate_model,
                )
                raise
        LOGGER.error(
            "step=query_model status=failed attempted_models=%s reason=%s",
            ",".join(attempted_models),
            last_error.reason if last_error is not None else "unknown",
        )
        raise RuntimeError(
            "All configured query models are unavailable "
            f"(last_reason={last_error.reason if last_error else 'unknown'})."
        ) from last_error
    finally:
        _QUERY_DEADLINE.reset(deadline_token)


def last_query_model_metrics() -> dict[str, object]:
    return dict(DEFAULT_GEMINI_PROVIDER.last_chat_metrics)


def last_gemini_metrics() -> dict[str, object]:
    """Backward-compatible alias for the provider-neutral metrics accessor."""
    return last_query_model_metrics()
