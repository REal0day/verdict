"""Provider registry and the interface every provider implements.

Adding a provider means adding a row to PROVIDERS and, if it isn't already
OpenAI-shaped, a class. Everything above this layer — summariser, extractor,
chat, analytics, the import planner — talks only to `AIProvider`.
"""
from __future__ import annotations

import os
import pathlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .tools import ToolConversation, ToolSpec


class AIProvider(ABC):
    """Minimal chat interface every provider implements."""

    name: str
    display_name: str
    model: str

    @abstractmethod
    def chat(self, system: str, messages: list[dict], max_tokens: int | None = None) -> str:
        """messages: [{'role': 'user'|'assistant', 'content': str}, ...] -> reply text"""

    def start_tools(
        self, system: str, tools: list[ToolSpec], max_tokens: int | None = None
    ) -> ToolConversation:
        """Begin a tool-use session. Providers without tool-calling override nothing."""
        from .errors import AICapabilityUnsupported

        raise AICapabilityUnsupported(
            self.display_name, "tool use (function calling)", "Anthropic, OpenAI, xAI, or a local OpenAI-compatible model"
        )


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    display_name: str
    # attribute prefix on `provider_keys` (e.g. "anthropic" -> anthropic_api_key)
    attr: str
    env_key: str
    requires_key: bool
    supports_tools: bool
    # True when the endpoint is operator-supplied rather than a vendor default.
    self_hosted: bool = False


PROVIDERS: dict[str, ProviderInfo] = {
    "anthropic": ProviderInfo("anthropic", "Anthropic", "anthropic", "ANTHROPIC_API_KEY", True, True),
    "openai": ProviderInfo("openai", "OpenAI", "openai", "OPENAI_API_KEY", True, True),
    "gemini": ProviderInfo("gemini", "Gemini", "gemini", "GEMINI_API_KEY", True, False),
    "grok": ProviderInfo("grok", "xAI", "xai", "XAI_API_KEY", True, True),
    "local": ProviderInfo(
        "local", "Local model", "local_ai", "LOCAL_AI_BASE_URL",
        requires_key=False, supports_tools=True, self_hosted=True,
    ),
}

# Accepted spellings that map onto a canonical provider name.
ALIASES = {"claude": "anthropic", "xai": "grok", "ollama": "local", "self-hosted": "local"}


def canonical(name: str | None) -> str:
    from ..config import settings

    n = (name or settings.default_ai_provider or "anthropic").strip().lower()
    return ALIASES.get(n, n)


# ---------------------------------------------------------------- local URLs

def in_container() -> bool:
    """Best-effort: are we running inside a container?"""
    if pathlib.Path("/.dockerenv").exists():
        return True
    try:
        return "docker" in pathlib.Path("/proc/1/cgroup").read_text()
    except OSError:
        return False


# Set IRS_LOCAL_AI_NO_REWRITE=1 to keep loopback URLs exactly as written.
_HOST_ALIASES = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]")
CONTAINER_HOST = "host.docker.internal"


def resolve_local_url(url: str) -> str:
    """Point a loopback URL at the container's host when we're containerised.

    A model running on the operator's machine is on *their* loopback, not the
    container's. Without this, "http://localhost:11434/v1" silently fails from
    inside Docker — the single most common way this setup goes wrong.
    """
    if not url or os.environ.get("IRS_LOCAL_AI_NO_REWRITE") == "1":
        return url
    if not in_container():
        return url
    parts = urlsplit(url if "://" in url else f"http://{url}")
    host = parts.hostname or ""
    if host.lower() not in _HOST_ALIASES:
        return url
    netloc = CONTAINER_HOST + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def normalise_base_url(url: str) -> str:
    """Accept what people actually paste and make it a usable base URL.

    Ollama's own docs show `http://localhost:11434`; its OpenAI-compatible
    routes live under `/v1`. Accepting both spellings avoids a confusing
    404-shaped failure.
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if "://" not in u:
        u = "http://" + u
    if not u.endswith("/v1") and "/v1/" not in u and not u.endswith("/api"):
        u = u + "/v1"
    return u


# ---------------------------------------------------------------- resolution

@dataclass(frozen=True)
class ResolvedProvider:
    info: ProviderInfo
    api_key: str | None
    model: str
    base_url: str


def resolve(name: str | None = None) -> ResolvedProvider:
    """Read the live settings for one provider without constructing it."""
    from ..config import provider_keys
    from .errors import AIProviderError

    n = canonical(name)
    info = PROVIDERS.get(n)
    if not info:
        raise AIProviderError(
            n,
            f"Unknown AI provider {n!r} is configured.",
            f"Set IRS_DEFAULT_AI_PROVIDER to one of: {', '.join(sorted(PROVIDERS))}.",
        )
    key = getattr(provider_keys, f"{info.attr}_api_key", None)
    model = getattr(provider_keys, f"{info.attr}_model", "") or ""
    base = getattr(provider_keys, f"{info.attr}_base_url", "") or ""
    if info.self_hosted:
        base = resolve_local_url(normalise_base_url(base))
    return ResolvedProvider(info=info, api_key=key, model=model, base_url=base)


def is_configured(name: str | None = None) -> bool:
    """Can this provider actually be used right now?"""
    try:
        r = resolve(name)
    except Exception:
        return False
    if r.info.self_hosted:
        return bool(r.base_url and r.model)
    return bool(r.api_key)


def get_provider(name: str | None = None) -> AIProvider:
    from .anthropic_provider import AnthropicProvider
    from .gemini_provider import GeminiProvider
    from .openai_compatible import OpenAICompatibleProvider
    from .errors import AIKeyMissing, AIProviderError

    r = resolve(name)
    n = r.info.name

    if n == "anthropic":
        return AnthropicProvider(api_key=r.api_key, model=r.model)
    if n == "gemini":
        return GeminiProvider(api_key=r.api_key, model=r.model)
    if n == "local":
        if not r.base_url:
            raise AIKeyMissing("Local model", "LOCAL_AI_BASE_URL")
        if not r.model:
            raise AIProviderError(
                "Local model",
                "No model id is set for the local endpoint.",
                "Set it under Settings → AI (or LOCAL_AI_MODEL), e.g. 'llama3.1:8b'.",
            )
    return OpenAICompatibleProvider(
        name=n,
        display_name=r.info.display_name,
        base_url=r.base_url,
        api_key=r.api_key,
        model=r.model,
        env_var=r.info.env_key,
        requires_key=r.info.requires_key,
    )
