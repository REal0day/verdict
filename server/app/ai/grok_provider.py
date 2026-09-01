import httpx
from .base import AIProvider
from .errors import AIKeyMissing, raise_for_upstream_status, unreachable
from ..config import provider_keys


class GrokProvider(AIProvider):
    """xAI Grok uses an OpenAI-compatible chat completions endpoint."""
    name = "grok"

    def chat(self, system: str, messages: list[dict]) -> str:
        if not provider_keys.xai_api_key:
            raise AIKeyMissing("xAI", "XAI_API_KEY")
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        try:
            r = httpx.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {provider_keys.xai_api_key}"},
                json={"model": provider_keys.xai_model, "messages": msgs},
                timeout=120,
            )
        except httpx.HTTPError as e:
            raise unreachable("xAI", e) from None
        raise_for_upstream_status("xAI", r.status_code, r.text)
        return r.json()["choices"][0]["message"]["content"]
