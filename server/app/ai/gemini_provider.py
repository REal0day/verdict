import httpx
from .base import AIProvider
from .errors import AIKeyMissing, raise_for_upstream_status, unreachable
from ..config import provider_keys


class GeminiProvider(AIProvider):
    name = "gemini"

    def chat(self, system: str, messages: list[dict]) -> str:
        if not provider_keys.gemini_api_key:
            raise AIKeyMissing("Gemini", "GEMINI_API_KEY")
        contents = []
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
        body = {"contents": contents}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{provider_keys.gemini_model}:generateContent?key={provider_keys.gemini_api_key}"
        )
        try:
            r = httpx.post(url, json=body, timeout=120)
        except httpx.HTTPError as e:
            raise unreachable("Gemini", e) from None
        # Gemini answers a bad key with 400 INVALID_ARGUMENT, not 401.
        if r.status_code == 400 and "API_KEY_INVALID" in r.text:
            from .errors import AIKeyInvalid
            raise AIKeyInvalid("Gemini")
        raise_for_upstream_status("Gemini", r.status_code, r.text)
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
