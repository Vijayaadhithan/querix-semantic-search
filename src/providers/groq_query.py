"""Groq structured-query provider."""

import contextlib
import time

import requests

from core.settings import (
    GROQ_API_BASE_URL,
    GROQ_API_KEY,
    GROQ_TIMEOUT_SECONDS,
    QUERY_EXTRACT_MAX_OUTPUT_TOKENS,
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


class GroqProvider(QueryProviderTransport):
    def __init__(
        self,
        api_key: str = GROQ_API_KEY,
        base_url: str = GROQ_API_BASE_URL,
        timeout_seconds: float = GROQ_TIMEOUT_SECONDS,
    ):
        super().__init__(api_key, base_url, timeout_seconds)

    @staticmethod
    def _output_text(payload: dict) -> str:
        direct = payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        parts = []
        for item in payload.get("output") or []:
            if not isinstance(item, dict):
                continue
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                if content.get("type") == "output_text":
                    parts.append(str(content.get("text", "")))
        text = "".join(parts).strip()
        if not text:
            raise ValueError("Groq returned an empty response.")
        return text

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
            "provider": "groq",
        }
        request_body = {
            "model": model,
            "instructions": system_prompt,
            "input": user_prompt,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "query_plan",
                    "strict": True,
                    "schema": schema,
                }
            },
            "max_output_tokens": QUERY_EXTRACT_MAX_OUTPUT_TOKENS,
        }
        if model.startswith("openai/gpt-oss-"):
            request_body["reasoning"] = {"effort": "low"}
        elif temperature > 0:
            request_body["temperature"] = temperature
        try:
            if not self.api_key:
                raise QueryModelUnavailableError(
                    model,
                    provider="groq",
                    reason="missing_api_key",
                )
            timeout_seconds = _effective_timeout(self.timeout_seconds, model)
            response = self._post(
                f"{self.base_url}/responses",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Groq-Beta": "inference-metrics",
                },
                json=request_body,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            payload = _response_payload(
                response,
                model=model,
                provider="groq",
            )
            usage = payload.get("usage") or {}
            if not isinstance(usage, dict):
                usage = {}
            metrics.update(
                {
                    "input_tokens": _metric_int(usage.get("input_tokens")),
                    "output_tokens": _metric_int(usage.get("output_tokens")),
                    "total_tokens": _metric_int(usage.get("total_tokens")),
                }
            )
            metadata = payload.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            for key, value in metadata.items():
                if key.endswith("_time"):
                    with contextlib.suppress(TypeError, ValueError):
                        metrics[f"groq_{key}_ms"] = float(value) * 1000
            status = str(payload.get("status") or "").casefold()
            if status and status != "completed":
                raise QueryModelUnavailableError(
                    model,
                    provider="groq",
                    reason=f"status_{status}",
                )
            try:
                text = self._output_text(payload)
            except ValueError as exc:
                raise QueryModelUnavailableError(
                    model,
                    provider="groq",
                    reason="empty_response",
                ) from exc
            return _structured_json_text(
                text,
                model=model,
                provider="groq",
            )
        except requests.HTTPError as exc:
            metrics.update(_http_error_metrics(exc))
            error = _http_status_error(model, "groq", exc)
            raise error from exc
        except QueryModelUnavailableError:
            raise
        except requests.Timeout as exc:
            raise QueryModelUnavailableError(
                model,
                provider="groq",
                reason=_timeout_reason(),
            ) from exc
        except requests.ConnectionError as exc:
            raise QueryModelUnavailableError(
                model,
                provider="groq",
                reason="connection_error",
            ) from exc
        except (
            requests.RequestException,
            TypeError,
            ValueError,
        ) as exc:
            raise RuntimeError(
                f"Cannot extract a structured query with Groq model '{model}'."
            ) from exc
        finally:
            metrics["total_ms"] = (time.perf_counter() - started) * 1000
            self.last_chat_metrics = metrics
