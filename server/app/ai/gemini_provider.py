import httpx
from .base import AIProvider
from ..config import provider_keys


class GeminiProvider(AIProvider):
    name = "gemini"

    def chat(self, system: str, messages: list[dict]) -> str:
        if not provider_keys.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
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
        r = httpx.post(url, json=body, timeout=120)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
