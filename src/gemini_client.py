import logging
import time
from urllib.parse import quote

import requests

from settings import (
    GEMINI_API_BASE_URL,
    GEMINI_API_KEY,
    QUERY_EXTRACT_MODELS,
)

FALLBACK_HTTP_STATUSES = {429, 500, 502, 503, 504}
LOGGER = logging.getLogger("uvicorn.error")


class GeminiModelUnavailableError(RuntimeError):
    def __init__(self, model: str, status_code: int):
        self.model = model
        self.status_code = status_code
        super().__init__(
            f"Google model '{model}' is temporarily unavailable "
            f"(HTTP {status_code})."
        )


class GeminiProvider:
    def __init__(
        self,
        api_key: str = GEMINI_API_KEY,
        base_url: str = GEMINI_API_BASE_URL,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.last_chat_metrics: dict[str, float | str | list[str]] = {}

    def structured_chat(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict,
        temperature: float = 0,
    ) -> str:
        if not self.api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not configured. Add it to the project .env file."
            )

        started = time.perf_counter()
        try:
            response = requests.post(
                (
                    f"{self.base_url}/models/"
                    f"{quote(model, safe='.-')}:generateContent"
                ),
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
                        "responseMimeType": "application/json",
                        "responseJsonSchema": schema,
                    },
                },
                timeout=60,
            )
            response.raise_for_status()
            payload = response.json()
            content = payload["candidates"][0]["content"]
            text = "".join(
                part.get("text", "")
                for part in content.get("parts", [])
            ).strip()
            if not text:
                raise ValueError("Gemini returned an empty response.")
            return strip_json_fence(text)
        except requests.HTTPError as exc:
            status_code = (
                exc.response.status_code
                if exc.response is not None
                else 0
            )
            if status_code in FALLBACK_HTTP_STATUSES:
                raise GeminiModelUnavailableError(
                    model,
                    status_code,
                ) from exc
            raise RuntimeError(
                f"Cannot extract a structured query with Google model "
                f"'{model}' (HTTP {status_code})."
            ) from exc
        except (
            requests.RequestException,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ) as exc:
            raise RuntimeError(
                f"Cannot extract a structured query with Google model "
                f"'{model}'."
            ) from exc
        finally:
            self.last_chat_metrics = {
                "total_ms": (time.perf_counter() - started) * 1000,
                "load_ms": 0.0,
                "model": model,
            }


DEFAULT_GEMINI_PROVIDER = GeminiProvider()


def strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    return stripped.strip()


def structured_chat(
    model: str,
    system_prompt: str,
    user_prompt: str,
    schema: dict,
    temperature: float = 0,
) -> str:
    models = (
        QUERY_EXTRACT_MODELS
        if model == QUERY_EXTRACT_MODELS[0]
        else (model,)
    )
    attempted_models = []
    started = time.perf_counter()
    last_error = None
    for position, candidate_model in enumerate(models, start=1):
        attempted_models.append(candidate_model)
        LOGGER.info(
            "step=query_model status=attempt model=%s position=%d/%d",
            candidate_model,
            position,
            len(models),
        )
        try:
            content = DEFAULT_GEMINI_PROVIDER.structured_chat(
                candidate_model,
                system_prompt,
                user_prompt,
                schema,
                temperature,
            )
            DEFAULT_GEMINI_PROVIDER.last_chat_metrics.update(
                {
                    "total_ms": (time.perf_counter() - started) * 1000,
                    "attempted_models": attempted_models,
                }
            )
            LOGGER.info(
                "step=query_model status=success model=%s duration_ms=%.0f",
                candidate_model,
                DEFAULT_GEMINI_PROVIDER.last_chat_metrics["total_ms"],
            )
            return content
        except GeminiModelUnavailableError as exc:
            last_error = exc
            LOGGER.warning(
                "step=query_model status=fallback model=%s http_status=%d "
                "next_model=%s",
                candidate_model,
                exc.status_code,
                (
                    models[position]
                    if position < len(models)
                    else "none"
                ),
            )
    LOGGER.error(
        "step=query_model status=failed attempted_models=%s",
        ",".join(attempted_models),
    )
    raise RuntimeError(
        "All configured Google query models are rate-limited or unavailable."
    ) from last_error


def last_gemini_metrics() -> dict[str, float | str | list[str]]:
    return dict(DEFAULT_GEMINI_PROVIDER.last_chat_metrics)
