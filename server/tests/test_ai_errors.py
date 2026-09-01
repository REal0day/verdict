"""AI provider failures must be typed and actionable, not opaque 500s.

Regression cover for the "no indication when the API key is missing or
invalid" issue: chat used to fail with a bare RuntimeError that FastAPI
turned into a 500, so the UI could only say "request failed".
"""
import base64, secrets, os, sys, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("IRS_ENCRYPTION_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
os.environ.setdefault("IRS_SECRET_KEY", "test")

import pytest

from app.ai.errors import (
    AIKeyInvalid,
    AIKeyMissing,
    AIProviderError,
    AIProviderUnavailable,
    raise_for_upstream_status,
    unreachable,
)


def test_missing_key_is_503_and_names_the_env_var():
    e = AIKeyMissing("Anthropic", "ANTHROPIC_API_KEY")
    assert e.status_code == 503
    admin = e.detail(is_admin=True)
    assert "ANTHROPIC_API_KEY" in admin and "Settings" in admin


def test_non_admin_is_told_who_to_ask_not_how_to_fix():
    e = AIKeyMissing("Anthropic", "ANTHROPIC_API_KEY")
    plain = e.detail(is_admin=False)
    assert "ANTHROPIC_API_KEY" not in plain
    assert "administrator" in plain.lower()


def test_rejected_key_is_502_and_distinct_from_missing():
    e = AIKeyInvalid("Anthropic")
    assert e.status_code == 502
    assert isinstance(e, AIProviderError)
    assert not isinstance(e, AIKeyMissing)


@pytest.mark.parametrize("code", [401, 403])
def test_upstream_auth_status_maps_to_key_invalid(code):
    with pytest.raises(AIKeyInvalid):
        raise_for_upstream_status("OpenAI", code)


def test_rate_limit_maps_to_unavailable():
    with pytest.raises(AIProviderUnavailable, match="rate-limiting"):
        raise_for_upstream_status("OpenAI", 429)


def test_server_error_carries_a_body_snippet():
    with pytest.raises(AIProviderUnavailable, match="upstream boom"):
        raise_for_upstream_status("OpenAI", 500, "upstream boom")


def test_success_status_does_not_raise():
    assert raise_for_upstream_status("OpenAI", 200) is None


def test_transport_failure_is_unavailable_not_auth():
    e = unreachable("Gemini", TimeoutError("timed out"))
    assert isinstance(e, AIProviderUnavailable)
    assert not isinstance(e, AIKeyInvalid)


def test_unknown_provider_name_is_typed():
    from app.ai.base import get_provider
    with pytest.raises(AIProviderError, match="Unknown AI provider"):
        get_provider("not-a-real-provider")


@pytest.mark.parametrize(
    "provider,attr,env_var",
    [
        ("openai", "openai_api_key", "OPENAI_API_KEY"),
        ("gemini", "gemini_api_key", "GEMINI_API_KEY"),
        ("grok", "xai_api_key", "XAI_API_KEY"),
    ],
)
def test_each_provider_raises_key_missing_without_a_key(monkeypatch, provider, attr, env_var):
    from app.ai.base import get_provider
    from app.config import provider_keys

    monkeypatch.setattr(provider_keys, attr, None)
    with pytest.raises(AIKeyMissing) as ei:
        get_provider(provider).chat("sys", [{"role": "user", "content": "hi"}])
    assert env_var in ei.value.detail(is_admin=True)


def test_anthropic_raises_key_missing_at_construction(monkeypatch):
    from app.ai.base import get_provider
    from app.config import provider_keys

    monkeypatch.setattr(provider_keys, "anthropic_api_key", None)
    with pytest.raises(AIKeyMissing):
        get_provider("anthropic")
