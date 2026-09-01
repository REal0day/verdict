from .base import AIProvider
from .errors import AIKeyInvalid, AIKeyMissing, AIProviderUnavailable
from ..config import provider_keys


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(self):
        import anthropic
        if not provider_keys.anthropic_api_key:
            raise AIKeyMissing("Anthropic", "ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(api_key=provider_keys.anthropic_api_key)
        self._model = provider_keys.anthropic_model

    def chat(self, system: str, messages: list[dict]) -> str:
        import anthropic

        # 16k tokens of output covers extractor JSON with ~25 detailed
        # findings without truncation. Claude Opus 4.5 supports much more,
        # but this is the sweet spot for cost + latency.
        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=16384,
                system=system or "",
                messages=[{"role": m["role"], "content": m["content"]} for m in messages],
            )
        except anthropic.AuthenticationError:
            raise AIKeyInvalid("Anthropic") from None
        except anthropic.PermissionDeniedError:
            raise AIKeyInvalid("Anthropic") from None
        except anthropic.RateLimitError:
            raise AIProviderUnavailable(
                "Anthropic", "Anthropic is rate-limiting requests. Try again shortly."
            ) from None
        except anthropic.NotFoundError:
            # Almost always a model id the key can't see or that doesn't exist.
            raise AIProviderUnavailable(
                "Anthropic",
                f"Anthropic rejected the model {self._model!r} — check the configured model name.",
            ) from None
        except anthropic.APIStatusError as e:
            raise AIProviderUnavailable(
                "Anthropic", f"Anthropic returned HTTP {e.status_code}."
            ) from None
        except anthropic.APIConnectionError as e:
            raise AIProviderUnavailable(
                "Anthropic", f"Could not reach Anthropic: {type(e).__name__}."
            ) from None

        return "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
