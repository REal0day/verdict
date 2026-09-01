import httpx

from .base import AIProvider
from .errors import AIKeyInvalid, AIKeyMissing, raise_for_upstream_status, unreachable
from ..config import provider_keys


class GeminiProvider(AIProvider):
    """Google Gemini. Its own request shape, so not OpenAI-compatible.

    Tool use is not wired up here yet — `start_tools` falls through to the
    base class, which raises AICapabilityUnsupported naming providers that do.
    """

    name = "gemini"
    display_name = "Gemini"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or provider_keys.gemini_api_key
        if not self.api_key:
            raise AIKeyMissing("Gemini", "GEMINI_API_KEY")
        self.model = model or provider_keys.gemini_model

    def chat(self, system: str, messages: list[dict], max_tokens: int | None = None) -> str:
        contents = []
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
        body: dict = {"contents": contents}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if max_tokens:
            body["generationConfig"] = {"maxOutputTokens": max_tokens}
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )
        try:
            r = httpx.post(url, json=body, timeout=120)
        except httpx.HTTPError as e:
            raise unreachable("Gemini", e) from None
        # Gemini answers a bad key with 400 INVALID_ARGUMENT, not 401.
        if r.status_code == 400 and "API_KEY_INVALID" in r.text:
            raise AIKeyInvalid("Gemini")
        raise_for_upstream_status("Gemini", r.status_code, r.text)
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
