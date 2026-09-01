"""Provider-neutral tool-use (function-calling) abstraction.

The folder-import planner is a real agentic loop: it hands the model a couple
of tools, runs them, feeds results back, and repeats until the model submits a
plan. Anthropic and the OpenAI-compatible APIs both support that, but with
different message shapes — Anthropic threads `tool_use`/`tool_result` blocks
through `content`, OpenAI uses `tool_calls` plus separate `role="tool"`
messages.

Rather than translate shapes at the call site, a provider hands back a
`ToolConversation` that owns its own native history. Callers only ever see the
normalised `AssistantTurn` / `ToolCall` / `ToolResult` types below.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """One tool offered to the model, in a provider-neutral shape."""

    name: str
    description: str
    # JSON Schema for the tool's arguments.
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """A model's request to run one tool."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """The outcome of running a ToolCall, to be fed back to the model."""

    call_id: str
    content: str
    is_error: bool = False


@dataclass
class AssistantTurn:
    """One assistant reply, normalised across providers.

    `stop_reason` is narrowed to what the loop actually needs: "tool_use" when
    the model wants tools run, "end_turn" when it is finished, "length" when it
    hit the output cap.
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ToolConversation(ABC):
    """A stateful tool-use session. Providers subclass this.

    The implementation keeps the provider-native message list internally, so
    the caller never has to know whether it is talking to Anthropic or an
    OpenAI-compatible endpoint.
    """

    @abstractmethod
    def send_user(self, content: str) -> AssistantTurn:
        """Append a user message and get the next assistant turn."""

    @abstractmethod
    def send_tool_results(self, results: list[ToolResult]) -> AssistantTurn:
        """Return tool outputs to the model and get the next assistant turn."""
