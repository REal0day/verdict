"""Typed provider failures that an operator (not the caller) can fix.

Every provider raises one of these instead of a bare ``RuntimeError`` so the
API layer can turn "the Anthropic key is missing" into an actionable message
rather than an opaque 500. Registered as an exception handler in
``app.main``, so any route that reaches a provider gets the same treatment.
"""
from __future__ import annotations


class AIProviderError(RuntimeError):
    """Base class. ``status_code`` is what the API should return."""

    status_code = 503

    def __init__(self, provider: str, reason: str, remedy: str = ""):
        self.provider = provider
        self.reason = reason
        self.remedy = remedy
        super().__init__(f"{reason} ({provider})" if provider else reason)

    def detail(self, is_admin: bool = False) -> str:
        """Message for the API response. Admins get the fix; everyone else
        gets told who to ask, since only admins can set the key."""
        if not self.remedy:
            return self.reason
        who = self.remedy if is_admin else "Ask an administrator to configure it."
        return f"{self.reason} {who}"


class AIKeyMissing(AIProviderError):
    """No API key configured for the selected provider."""

    status_code = 503

    def __init__(self, provider: str, env_var: str):
        super().__init__(
            provider,
            f"No {provider} API key is configured, so AI features are unavailable.",
            f"Set it under Settings → AI in the portal, or set {env_var} in the server environment.",
        )


class AIKeyInvalid(AIProviderError):
    """The key exists but the provider rejected it (401/403)."""

    status_code = 502

    def __init__(self, provider: str):
        super().__init__(
            provider,
            f"The configured {provider} API key was rejected.",
            "Check the key under Settings → AI — it may be revoked, expired, or lack access to the model.",
        )


class AIProviderUnavailable(AIProviderError):
    """Upstream is rate-limiting, over quota, erroring, or unreachable."""

    status_code = 502

    def __init__(self, provider: str, reason: str):
        super().__init__(provider, reason, "")


def raise_for_upstream_status(provider: str, status_code: int, body: str = "") -> None:
    """Translate an upstream HTTP status into a typed error.

    Used by the httpx-based providers in place of ``resp.raise_for_status()``,
    which only ever yields an opaque ``HTTPStatusError``.
    """
    if status_code < 400:
        return
    if status_code in (401, 403):
        raise AIKeyInvalid(provider)
    if status_code == 429:
        raise AIProviderUnavailable(
            provider, f"{provider} is rate-limiting requests. Try again shortly."
        )
    snippet = f" — {body[:200]}" if body else ""
    raise AIProviderUnavailable(
        provider, f"{provider} returned HTTP {status_code}.{snippet}"
    )


def unreachable(provider: str, exc: Exception) -> AIProviderUnavailable:
    """Wrap a transport-level failure (DNS, TLS, timeout)."""
    return AIProviderUnavailable(
        provider, f"Could not reach {provider}: {type(exc).__name__}."
    )
