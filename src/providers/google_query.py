"""Google structured-query provider."""

import time
from urllib.parse import quote

import requests

from core.settings import (
    GEMINI_API_BASE_URL,
    GEMINI_API_KEY,
    GEMINI_THINKING_LEVEL,
    QUERY_EXTRACT_MAX_OUTPUT_TOKENS,
    QUERY_EXTRACT_TIMEOUT_SECONDS,
)
from providers.query_transport import (
    QueryModelUnavailableError,
    QueryProviderTransport,
    _effective_timeout,
    _http_error_metrics,
    _http_status_error,
    _metric_int,
    _response_payload,
    _structured_json_text,
    _timeout_reason,
)


class GeminiProvider(QueryProviderTransport):
    def __init__(
        self,
        api_key: str = GEMINI_API_KEY,
        base_url: str = GEMINI_API_BASE_URL,
        timeout_seconds: float = QUERY_EXTRACT_TIMEOUT_SECONDS,
    ):
        super().__init__(api_key, base_url, timeout_seconds)

    def structured_chat(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict,
        temperature: float = 0,
    ) -> str:
        started = time.perf_counter()
        metrics: dict[str, float | int | str | list] = {
            "load_ms": 0.0,
            "model": model,
            "provider": "google",
        }
        try:
            if not self.api_key:
                raise QueryModelUnavailableError(
                    model,
                    provider="google",
                    reason="missing_api_key",
                )
            timeout_seconds = _effective_timeout(self.timeout_seconds, model)
            response = self._post(
                (f"{self.base_url}/models/{quote(model, safe='.-')}:generateContent"),
                headers={
                    "Content-Type": "application/json",
                    "X-goog-api-key": self.api_key,
                },
                json={
                    "systemInstruction": {
                        "parts": [{"text": system_prompt}],
                    },
                    "contents": [
                        {
                            "role": "user",
                            "parts": [{"text": user_prompt}],
                        }
                    ],
                    "generationConfig": {
                        "temperature": temperature,
                        "maxOutputTokens": QUERY_EXTRACT_MAX_OUTPUT_TOKENS,
                        "responseFormat": {
                            "text": {
                                "mimeType": "APPLICATION_JSON",
                                "schema": schema,
                            }
                        },
                        "thinkingConfig": {
                            "thinkingLevel": GEMINI_THINKING_LEVEL,
                        },
                    },
                },
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            payload = _response_payload(
                response,
                model=model,
                provider="google",
            )
            usage = payload.get("usageMetadata") or {}
            if not isinstance(usage, dict):
                usage = {}
            metrics.update(
                {
                    "input_tokens": _metric_int(usage.get("promptTokenCount")),
                    "output_tokens": _metric_int(usage.get("candidatesTokenCount")),
                    "thought_tokens": _metric_int(usage.get("thoughtsTokenCount")),
                    "total_tokens": _metric_int(usage.get("totalTokenCount")),
                }
            )
            candidates = payload.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                prompt_feedback = payload.get("promptFeedback") or {}
                block_reason = (
                    prompt_feedback.get("blockReason")
                    if isinstance(prompt_feedback, dict)
                    else None
                )
                reason = (
                    f"blocked_{str(block_reason).casefold()}"
                    if block_reason
                    else "empty_candidates"
                )
                raise QueryModelUnavailableError(
                    model,
                    provider="google",
                    reason=reason,
                )
            candidate = candidates[0]
            if not isinstance(candidate, dict):
                raise QueryModelUnavailableError(
                    model,
                    provider="google",
                    reason="invalid_candidate",
                )
            finish_reason = str(candidate.get("finishReason") or "").upper()
            if finish_reason and finish_reason not in {"STOP", "UNSPECIFIED"}:
                metrics["finish_reason"] = finish_reason
                raise QueryModelUnavailableError(
                    model,
                    provider="google",
                    reason=f"finish_{finish_reason.casefold()}",
                )
            content = candidate.get("content")
            if not isinstance(content, dict):
                raise QueryModelUnavailableError(
                    model,
                    provider="google",
                    reason="invalid_candidate_content",
                )
            parts = content.get("parts") or []
            text = "".join(
                str(part.get("text", ""))
                for part in parts
                if isinstance(part, dict)
                and not part.get("thought")
                and part.get("text") is not None
            ).strip()
            if not text:
                raise QueryModelUnavailableError(
                    model,
                    provider="google",
                    reason="empty_response",
                )
            return _structured_json_text(
                text,
                model=model,
                provider="google",
            )
        except requests.HTTPError as exc:
            metrics.update(_http_error_metrics(exc))
            error = _http_status_error(model, "google", exc)
            raise error from exc
        except QueryModelUnavailableError:
            raise
        except requests.Timeout as exc:
            raise QueryModelUnavailableError(
                model,
                provider="google",
                reason=_timeout_reason(),
            ) from exc
        except requests.ConnectionError as exc:
            raise QueryModelUnavailableError(
                model,
                provider="google",
                reason="connection_error",
            ) from exc
        except (
            requests.RequestException,
            TypeError,
            ValueError,
        ) as exc:
            raise RuntimeError(
                f"Cannot extract a structured query with Google model '{model}'."
            ) from exc
        finally:
            metrics["total_ms"] = (time.perf_counter() - started) * 1000
            self.last_chat_metrics = metrics
