"""Model-agnostic provider plumbing: registry, local endpoints, tool use.

Covers the Phase 1/2 work — every feature routed through `get_provider()`,
per-provider configuration, and the local/self-hosted endpoint support.
"""
import base64, os, pathlib, secrets, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("IRS_ENCRYPTION_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
os.environ.setdefault("IRS_SECRET_KEY", "test")

import pytest

from app.ai import base as ai_base
from app.ai.base import (
    PROVIDERS, canonical, get_provider, is_configured, normalise_base_url,
    resolve, resolve_local_url,
)
from app.ai.errors import AICapabilityUnsupported, AIKeyMissing, AIProviderError
from app.ai.openai_compatible import OpenAICompatibleProvider
from app.ai.tools import AssistantTurn, ToolResult, ToolSpec
from app.config import provider_keys


# ---------------- registry ----------------

def test_aliases_resolve_to_canonical_names():
    assert canonical("claude") == "anthropic"
    assert canonical("xai") == "grok"
    assert canonical("ollama") == "local"


def test_unknown_provider_is_a_typed_error():
    with pytest.raises(AIProviderError, match="Unknown AI provider"):
        resolve("nope")


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_every_registered_provider_resolves(name):
    r = resolve(name)
    assert r.info.name == name


# ---------------- local endpoint URLs ----------------

@pytest.mark.parametrize("raw,expected", [
    ("http://localhost:11434", "http://localhost:11434/v1"),
    ("http://localhost:11434/v1", "http://localhost:11434/v1"),
    ("localhost:1234", "http://localhost:1234/v1"),
    ("http://gpu-box.lan:8000/v1", "http://gpu-box.lan:8000/v1"),
    ("", ""),
])
def test_base_url_normalisation_accepts_what_people_paste(raw, expected):
    assert normalise_base_url(raw) == expected


def test_loopback_is_rewritten_to_the_container_host(monkeypatch):
    """A model on the operator's machine is not on the container's loopback."""
    monkeypatch.setattr(ai_base, "in_container", lambda: True)
    assert resolve_local_url("http://localhost:11434/v1") == \
        "http://host.docker.internal:11434/v1"
    assert resolve_local_url("http://127.0.0.1:1234/v1") == \
        "http://host.docker.internal:1234/v1"


def test_remote_hosts_are_never_rewritten(monkeypatch):
    monkeypatch.setattr(ai_base, "in_container", lambda: True)
    url = "http://gpu-box.lan:11434/v1"
    assert resolve_local_url(url) == url


def test_no_rewrite_outside_a_container(monkeypatch):
    monkeypatch.setattr(ai_base, "in_container", lambda: False)
    assert resolve_local_url("http://localhost:11434/v1") == "http://localhost:11434/v1"


def test_rewrite_can_be_disabled(monkeypatch):
    monkeypatch.setattr(ai_base, "in_container", lambda: True)
    monkeypatch.setenv("IRS_LOCAL_AI_NO_REWRITE", "1")
    assert resolve_local_url("http://localhost:11434/v1") == "http://localhost:11434/v1"


# ---------------- configured-ness ----------------

def test_hosted_provider_needs_a_key(monkeypatch):
    monkeypatch.setattr(provider_keys, "openai_api_key", None)
    assert not is_configured("openai")
    monkeypatch.setattr(provider_keys, "openai_api_key", "sk-test")
    assert is_configured("openai")


def test_local_provider_needs_a_url_and_model_not_a_key(monkeypatch):
    monkeypatch.setattr(provider_keys, "local_ai_api_key", None)
    monkeypatch.setattr(provider_keys, "local_ai_base_url", "")
    monkeypatch.setattr(provider_keys, "local_ai_model", "")
    assert not is_configured("local")

    monkeypatch.setattr(provider_keys, "local_ai_base_url", "http://gpu-box.lan:11434/v1")
    assert not is_configured("local"), "a URL without a model id is not usable"

    monkeypatch.setattr(provider_keys, "local_ai_model", "llama3.1:8b")
    assert is_configured("local"), "a local endpoint must not require an API key"


def test_local_provider_builds_without_a_key(monkeypatch):
    monkeypatch.setattr(provider_keys, "local_ai_api_key", None)
    monkeypatch.setattr(provider_keys, "local_ai_base_url", "http://gpu-box.lan:11434")
    monkeypatch.setattr(provider_keys, "local_ai_model", "llama3.1:8b")
    p = get_provider("local")
    assert isinstance(p, OpenAICompatibleProvider)
    assert p.base_url == "http://gpu-box.lan:11434/v1"
    assert p.model == "llama3.1:8b"


def test_local_provider_without_a_model_says_so(monkeypatch):
    monkeypatch.setattr(provider_keys, "local_ai_base_url", "http://gpu-box.lan:11434/v1")
    monkeypatch.setattr(provider_keys, "local_ai_model", "")
    with pytest.raises(AIProviderError, match="model id"):
        get_provider("local")


def test_local_provider_without_a_url_says_so(monkeypatch):
    monkeypatch.setattr(provider_keys, "local_ai_base_url", "")
    monkeypatch.setattr(provider_keys, "local_ai_model", "llama3.1:8b")
    with pytest.raises(AIKeyMissing):
        get_provider("local")


# ---------------- tool use ----------------

def _fake_openai(monkeypatch, responses):
    """Drive OpenAIToolConversation off canned API payloads."""
    calls = iter(responses)
    monkeypatch.setattr(
        OpenAICompatibleProvider, "_post", lambda self, payload: next(calls)
    )


def test_openai_tool_calls_are_normalised(monkeypatch):
    _fake_openai(monkeypatch, [{
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {"name": "read_file", "arguments": '{"relpath": "a/b.md"}'},
                }],
            },
        }]
    }])
    p = OpenAICompatibleProvider(
        name="local", display_name="Local model", base_url="http://x/v1",
        api_key=None, model="m", requires_key=False,
    )
    convo = p.start_tools("sys", [ToolSpec("read_file", "d", {"type": "object"})])
    turn = convo.send_user("go")
    assert isinstance(turn, AssistantTurn) and turn.wants_tools
    assert turn.tool_calls[0].name == "read_file"
    assert turn.tool_calls[0].arguments == {"relpath": "a/b.md"}
    assert turn.stop_reason == "tool_use"


def test_malformed_tool_arguments_do_not_crash_the_loop(monkeypatch):
    """Small local models sometimes emit arguments that aren't valid JSON."""
    _fake_openai(monkeypatch, [{
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "tool_calls": [{"id": "c1", "function": {"name": "read_file",
                                                          "arguments": "{not json"}}],
            },
        }]
    }])
    p = OpenAICompatibleProvider(
        name="local", display_name="Local model", base_url="http://x/v1",
        api_key=None, model="m", requires_key=False,
    )
    turn = p.start_tools("s", [ToolSpec("read_file", "d", {})]).send_user("go")
    assert turn.tool_calls[0].arguments == {}


def test_tool_results_round_trip(monkeypatch):
    _fake_openai(monkeypatch, [
        {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant",
            "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
        }}]},
        {"choices": [{"finish_reason": "stop",
                      "message": {"role": "assistant", "content": "done"}}]},
    ])
    p = OpenAICompatibleProvider(
        name="local", display_name="Local model", base_url="http://x/v1",
        api_key=None, model="m", requires_key=False,
    )
    convo = p.start_tools("s", [ToolSpec("read_file", "d", {})])
    convo.send_user("go")
    final = convo.send_tool_results([ToolResult(call_id="c1", content="file body")])
    assert not final.wants_tools
    assert final.text == "done"
    assert final.stop_reason == "end_turn"


def test_provider_without_tool_support_says_which_ones_have_it(monkeypatch):
    monkeypatch.setattr(provider_keys, "gemini_api_key", "k")
    with pytest.raises(AICapabilityUnsupported) as ei:
        get_provider("gemini").start_tools("s", [])
    assert "tool use" in ei.value.detail(is_admin=True)


def test_planner_uses_the_abstraction_not_the_anthropic_sdk():
    """Regression: the planner used to construct anthropic.Anthropic directly."""
    src = pathlib.Path(__file__).resolve().parents[1] / "app/ai/import_planner.py"
    text = src.read_text()
    assert "import anthropic" not in text
    assert "start_tools" in text


def test_ai_verdict_uses_the_abstraction_not_the_anthropic_sdk():
    src = pathlib.Path(__file__).resolve().parents[1] / "app/routers/scans.py"
    text = src.read_text()
    assert "anthropic.Anthropic(" not in text
