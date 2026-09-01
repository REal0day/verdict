"""One implementation for every endpoint that speaks the OpenAI chat API.

OpenAI, xAI/Grok, Ollama, vLLM, LM Studio, LiteLLM and OpenRouter all accept
`POST {base_url}/chat/completions` with the same body. The only things that
differ are the base URL, whether a key is required, and the model id — so they
are configuration, not code.

That is what makes "bring your own model" cheap: a self-hosted endpoint is not
a special case, it is the same provider with a different base URL.
"""
from __future__ import annotations

import json
import logging

import httpx

from .base import AIProvider
from .errors import AIKeyMissing, raise_for_upstream_status, unreachable
from .tools import AssistantTurn, ToolCall, ToolConversation, ToolResult, ToolSpec

log = logging.getLogger("irs.ai")

DEFAULT_TIMEOUT = 300  # local models on CPU can be very slow


class OpenAICompatibleProvider(AIProvider):
    """Chat + tool-use against any OpenAI-shaped endpoint."""

    def __init__(
        self,
        *,
        name: str,
        display_name: str,
        base_url: str,
        api_key: str | None,
        model: str,
        env_var: str = "",
        requires_key: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.name = name
        self.display_name = display_name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        if requires_key and not api_key:
            raise AIKeyMissing(display_name, env_var or f"{name.upper()}_API_KEY")

    # ---- plumbing ----

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        # Local servers usually ignore auth, but sending a bearer is harmless
        # and LiteLLM/OpenRouter-style gateways do want one.
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _post(self, payload: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        try:
            r = httpx.post(url, headers=self._headers(), json=payload, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise unreachable(self.display_name, e) from None
        raise_for_upstream_status(self.display_name, r.status_code, r.text)
        try:
            return r.json()
        except json.JSONDecodeError:
            from .errors import AIProviderUnavailable
            raise AIProviderUnavailable(
                self.display_name, f"{self.display_name} returned a non-JSON response."
            ) from None

    # ---- chat ----

    def chat(self, system: str, messages: list[dict], max_tokens: int | None = None) -> str:
        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {"role": m["role"], "content": m["content"]} for m in messages
        ]
        payload: dict = {"model": self.model, "messages": msgs}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        data = self._post(payload)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            from .errors import AIProviderUnavailable
            raise AIProviderUnavailable(
                self.display_name,
                f"{self.display_name} returned an unexpected response shape.",
            ) from None

    # ---- tool use ----

    def start_tools(
        self, system: str, tools: list[ToolSpec], max_tokens: int | None = None
    ) -> "OpenAIToolConversation":
        return OpenAIToolConversation(self, system, tools, max_tokens)


class OpenAIToolConversation(ToolConversation):
    """Tool-use loop in OpenAI shape: `tool_calls` + `role="tool"` replies."""

    def __init__(
        self,
        provider: OpenAICompatibleProvider,
        system: str,
        tools: list[ToolSpec],
        max_tokens: int | None,
    ):
        self._p = provider
        self._max_tokens = max_tokens
        self._tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]
        self._messages: list[dict] = []
        if system:
            self._messages.append({"role": "system", "content": system})

    def send_user(self, content: str) -> AssistantTurn:
        self._messages.append({"role": "user", "content": content})
        return self._turn()

    def send_tool_results(self, results: list[ToolResult]) -> AssistantTurn:
        for r in results:
            self._messages.append({
                "role": "tool",
                "tool_call_id": r.call_id,
                "content": f"ERROR: {r.content}" if r.is_error else r.content,
            })
        return self._turn()

    def _turn(self) -> AssistantTurn:
        payload: dict = {
            "model": self._p.model,
            "messages": self._messages,
            "tools": self._tools,
        }
        if self._max_tokens:
            payload["max_tokens"] = self._max_tokens
        data = self._p._post(payload)

        try:
            choice = data["choices"][0]
        except (KeyError, IndexError, TypeError):
            from .errors import AIProviderUnavailable
            raise AIProviderUnavailable(
                self._p.display_name,
                f"{self._p.display_name} returned an unexpected response shape.",
            ) from None

        msg = choice.get("message") or {}
        # Echo the assistant message back verbatim so the next turn has context.
        self._messages.append(msg)

        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                # Smaller/local models sometimes emit not-quite-JSON arguments.
                log.warning("tool args were not valid JSON: %r", raw_args[:200])
                args = {}
            calls.append(ToolCall(id=tc.get("id") or "", name=fn.get("name") or "", arguments=args))

        finish = choice.get("finish_reason") or "stop"
        stop = "tool_use" if calls else ("length" if finish == "length" else "end_turn")
        return AssistantTurn(text=msg.get("content") or "", tool_calls=calls, stop_reason=stop)
