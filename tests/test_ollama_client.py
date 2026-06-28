import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ollama_client import OllamaProvider, ollama_timing_metrics


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_ollama_timing_metrics_converts_nanoseconds_to_milliseconds():
    assert ollama_timing_metrics(
        {
            "total_duration": 2_500_000,
            "load_duration": 500_000,
            "prompt_eval_duration": 1_000_000,
            "eval_duration": 750_000,
        }
    ) == {
        "total_ms": 2.5,
        "load_ms": 0.5,
        "prompt_eval_ms": 1.0,
        "eval_ms": 0.75,
    }


def test_preload_embedding_model_uses_configured_keep_alive(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured.update({"url": url, "json": json, "timeout": timeout})
        return FakeResponse(
            {
                "embeddings": [[0.1, 0.2]],
                "total_duration": 4_000_000,
                "load_duration": 0,
            }
        )

    monkeypatch.setattr("ollama_client.requests.post", fake_post)
    provider = OllamaProvider(
        base_url="http://ollama.test",
        keep_alive="-1",
    )

    metrics = provider.preload_embedding_model()

    assert captured["url"] == "http://ollama.test/api/embed"
    assert captured["json"] == {
        "model": provider.embedding_model,
        "input": ["startup warmup"],
        "keep_alive": "-1",
    }
    assert metrics["total_ms"] == 4.0
