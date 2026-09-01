from .base import AIProvider
from .errors import AIKeyInvalid, AIKeyMissing, AIProviderUnavailable
from .tools import AssistantTurn, ToolCall, ToolConversation, ToolResult, ToolSpec
from ..config import provider_keys

# 16k tokens of output covers extractor JSON with ~25 detailed findings
# without truncation. Opus supports much more, but this is the sweet spot
# for cost + latency.
DEFAULT_MAX_TOKENS = 16384


def _translate(e: Exception, model: str) -> Exception:
    """Map an Anthropic SDK exception onto our typed provider errors."""
    import anthropic

    if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return AIKeyInvalid("Anthropic")
    if isinstance(e, anthropic.RateLimitError):
        return AIProviderUnavailable(
            "Anthropic", "Anthropic is rate-limiting requests. Try again shortly."
        )
    if isinstance(e, anthropic.NotFoundError):
        # Almost always a model id the key can't see or that doesn't exist.
        return AIProviderUnavailable(
            "Anthropic",
            f"Anthropic rejected the model {model!r} — check the configured model name.",
        )
    if isinstance(e, anthropic.APIStatusError):
        return AIProviderUnavailable("Anthropic", f"Anthropic returned HTTP {e.status_code}.")
    if isinstance(e, anthropic.APIConnectionError):
        return AIProviderUnavailable("Anthropic", f"Could not reach Anthropic: {type(e).__name__}.")
    return e


class AnthropicProvider(AIProvider):
    name = "anthropic"
    display_name = "Anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        import anthropic

        key = api_key or provider_keys.anthropic_api_key
        if not key:
            raise AIKeyMissing("Anthropic", "ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(api_key=key)
        self.model = model or provider_keys.anthropic_model

    def _create(self, **kwargs):
        try:
            return self._client.messages.create(model=self.model, **kwargs)
        except Exception as e:
            raise _translate(e, self.model) from None

    def chat(self, system: str, messages: list[dict], max_tokens: int | None = None) -> str:
        resp = self._create(
            max_tokens=max_tokens or DEFAULT_MAX_TOKENS,
            system=system or "",
            messages=[{"role": m["role"], "content": m["content"]} for m in messages],
        )
        return "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )

    def start_tools(
        self, system: str, tools: list[ToolSpec], max_tokens: int | None = None
    ) -> "AnthropicToolConversation":
        return AnthropicToolConversation(self, system, tools, max_tokens)


class AnthropicToolConversation(ToolConversation):
    """Tool-use loop in Anthropic shape: tool_use / tool_result content blocks."""

    def __init__(
        self,
        provider: AnthropicProvider,
        system: str,
        tools: list[ToolSpec],
        max_tokens: int | None,
    ):
        self._p = provider
        self._system = system or ""
        self._max_tokens = max_tokens or DEFAULT_MAX_TOKENS
        self._tools = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]
        self._messages: list[dict] = []

    def send_user(self, content: str) -> AssistantTurn:
        self._messages.append({"role": "user", "content": content})
        return self._turn()

    def send_tool_results(self, results: list[ToolResult]) -> AssistantTurn:
        blocks = []
        for r in results:
            b: dict = {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content}
            if r.is_error:
                b["is_error"] = True
            blocks.append(b)
        self._messages.append({"role": "user", "content": blocks})
        return self._turn()

    def _turn(self) -> AssistantTurn:
        resp = self._p._create(
            max_tokens=self._max_tokens,
            system=self._system,
            tools=self._tools,
            messages=self._messages,
        )
        # Keep the assistant's own blocks in history so tool_use ids resolve.
        self._messages.append({"role": "assistant", "content": resp.content})

        text_parts, calls = [], []
        for block in resp.content:
            btype = getattr(block, "type", "")
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))

        stop = "tool_use" if calls else (
            "length" if resp.stop_reason == "max_tokens" else "end_turn"
        )
        return AssistantTurn(text="".join(text_parts), tool_calls=calls, stop_reason=stop)
