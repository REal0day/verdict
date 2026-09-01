import httpx
from .base import AIProvider
from .errors import AIKeyMissing, raise_for_upstream_status, unreachable
from ..config import provider_keys


class OpenAIProvider(AIProvider):
    name = "openai"

    def chat(self, system: str, messages: list[dict]) -> str:
        if not provider_keys.openai_api_key:
            raise AIKeyMissing("OpenAI", "OPENAI_API_KEY")
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        try:
            r = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {provider_keys.openai_api_key}"},
                json={"model": provider_keys.openai_model, "messages": msgs},
                timeout=120,
            )
        except httpx.HTTPError as e:
            raise unreachable("OpenAI", e) from None
        raise_for_upstream_status("OpenAI", r.status_code, r.text)
        return r.json()["choices"][0]["message"]["content"]
