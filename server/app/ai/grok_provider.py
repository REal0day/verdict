import httpx
from .base import AIProvider
from ..config import provider_keys


class GrokProvider(AIProvider):
    """xAI Grok uses an OpenAI-compatible chat completions endpoint."""
    name = "grok"

    def chat(self, system: str, messages: list[dict]) -> str:
        if not provider_keys.xai_api_key:
            raise RuntimeError("XAI_API_KEY not set")
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        r = httpx.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {provider_keys.xai_api_key}"},
            json={"model": provider_keys.xai_model, "messages": msgs},
            timeout=120,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
