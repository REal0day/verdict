from .base import AIProvider
from ..config import provider_keys


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(self):
        import anthropic
        if not provider_keys.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self._client = anthropic.Anthropic(api_key=provider_keys.anthropic_api_key)
        self._model = provider_keys.anthropic_model

    def chat(self, system: str, messages: list[dict]) -> str:
        # 16k tokens of output covers extractor JSON with ~25 detailed
        # findings without truncation. Claude Opus 4.5 supports much more,
        # but this is the sweet spot for cost + latency.
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=16384,
            system=system or "",
            messages=[{"role": m["role"], "content": m["content"]} for m in messages],
        )
        return "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
