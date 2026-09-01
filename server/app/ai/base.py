from abc import ABC, abstractmethod


class AIProvider(ABC):
    """Minimal chat interface every provider implements."""

    name: str

    @abstractmethod
    def chat(self, system: str, messages: list[dict]) -> str:
        """messages: [{'role': 'user'|'assistant', 'content': str}, ...] -> reply text"""
        ...


def get_provider(name: str | None = None) -> "AIProvider":
    from ..config import settings
    from .anthropic_provider import AnthropicProvider
    from .openai_provider import OpenAIProvider
    from .gemini_provider import GeminiProvider
    from .grok_provider import GrokProvider

    name = (name or settings.default_ai_provider).lower()
    table = {
        "anthropic": AnthropicProvider,
        "claude": AnthropicProvider,
        "openai": OpenAIProvider,
        "gemini": GeminiProvider,
        "grok": GrokProvider,
        "xai": GrokProvider,
    }
    cls = table.get(name)
    if not cls:
        raise ValueError(f"Unknown AI provider: {name}")
    return cls()
