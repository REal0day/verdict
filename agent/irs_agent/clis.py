"""Pluggable coding-agent CLIs for Workbench sessions.

The agent used to shell out to `claude` and parse Claude Code's stream-json.
That made the Workbench Claude-only, which is the deepest model coupling in
the product.

Two adapters ship:

  claude   — Claude Code. Full fidelity: streamed tool calls, thinking blocks,
             resumable sessions, cost/turn accounting.
  generic  — any other CLI, driven by an operator-supplied command template.
             Output is streamed as plain text. No resume and no structured
             tool events, because there is no agreed format to parse.

`generic` exists so an operator can wire up Codex, Gemini CLI, aider or an
in-house tool without waiting for us to add an adapter — and without us
guessing at flags we cannot verify. Adding a first-class adapter later is a
matter of subclassing CLIAdapter.
"""
from __future__ import annotations

import json
import shlex
import shutil
from dataclasses import dataclass, field


_TOOL_PREVIEW = 400


@dataclass
class ParsedLine:
    """What one line of CLI output means to the caller."""

    events: list[dict] = field(default_factory=list)  # slim events for the UI
    session_id: str | None = None                     # resume token, if any
    final_text: str | None = None                     # authoritative final answer
    final_err: str | None = None
    text_chunk: str | None = None                     # accumulate into output


@dataclass
class Launch:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)


class CLIAdapter:
    name = "generic"
    display_name = "Generic CLI"
    supports_resume = False
    supports_tool_events = False
    exe_candidates: list[str] = []

    def find_exe(self) -> str | None:
        for c in self.exe_candidates:
            if p := shutil.which(c):
                return p
        return None

    def build(self, *, exe, prompt, cwd, model="", resume=None,
              append_system_prompt="", bypass_permissions=False) -> Launch:
        raise NotImplementedError

    def parse_line(self, line: str) -> ParsedLine:
        raise NotImplementedError


class ClaudeCodeAdapter(CLIAdapter):
    name = "claude"
    display_name = "Claude Code"
    supports_resume = True
    supports_tool_events = True
    exe_candidates = ["claude"]

    def build(self, *, exe, prompt, cwd, model="", resume=None,
              append_system_prompt="", bypass_permissions=False) -> Launch:
        argv = [exe, "-p", "--verbose", "--output-format", "stream-json"]
        if model:
            argv += ["--model", model]
        if append_system_prompt:
            argv += ["--append-system-prompt", append_system_prompt]
        if bypass_permissions:
            # Active-testing sessions run headless — no one can answer
            # permission prompts, and the harness must pip-install + run
            # python/curl/nmap/etc.
            argv += ["--dangerously-skip-permissions"]
        if resume:
            argv += ["--resume", resume]
        argv.append(prompt)
        return Launch(argv=argv)

    def parse_line(self, line: str) -> ParsedLine:
        try:
            ev = json.loads(line)
        except Exception:
            # Not JSON — surface it rather than dropping it.
            return ParsedLine(events=[{"type": "text", "text": line[:500]}])

        out = ParsedLine()
        t = ev.get("type")

        if t == "system":
            if ev.get("session_id"):
                out.session_id = ev["session_id"]
            out.events.append({
                "type": "system", "subtype": ev.get("subtype"),
                "session_id": ev.get("session_id"),
                "model": ev.get("model"), "cwd": ev.get("cwd"),
            })
        elif t == "assistant":
            content = (ev.get("message") or {}).get("content") or []
            blocks: list[dict] = []
            texts: list[str] = []
            for c in content:
                ct = c.get("type")
                if ct == "text" and c.get("text"):
                    blocks.append({"type": "text", "text": c["text"]})
                    texts.append(c["text"])
                elif ct == "thinking":
                    txt = (c.get("thinking") or "").strip()
                    if txt:
                        blocks.append({"type": "thinking", "text": txt[:_TOOL_PREVIEW]})
                elif ct == "tool_use":
                    blocks.append({"type": "tool_use", "name": c.get("name", "?"),
                                   "hint": _hint(c.get("input") or {})})
            if texts:
                out.text_chunk = "".join(texts)
            if blocks:
                out.events.append({"type": "assistant", "content": blocks})
        elif t == "user":
            content = (ev.get("message") or {}).get("content") or []
            results = []
            for c in content:
                if c.get("type") == "tool_result":
                    raw = c.get("content")
                    if isinstance(raw, list):
                        txt = "".join(p.get("text", "") for p in raw if isinstance(p, dict))
                    else:
                        txt = str(raw or "")
                    results.append({"type": "tool_result",
                                    "is_error": bool(c.get("is_error")),
                                    "preview": txt[:_TOOL_PREVIEW]})
            if results:
                out.events.append({"type": "tool_result", "results": results})
        elif t == "result":
            if ev.get("subtype") == "success":
                out.final_text = str(ev.get("result") or "")
            else:
                out.final_err = str(
                    ev.get("result") or ev.get("error") or ev.get("subtype")
                    or "the CLI reported an error"
                )
            out.events.append({
                "type": "result", "subtype": ev.get("subtype"),
                "duration_ms": ev.get("duration_ms"),
                "total_cost_usd": ev.get("total_cost_usd"),
                "num_turns": ev.get("num_turns"),
            })
        return out


class GenericCLIAdapter(CLIAdapter):
    """Runs an operator-supplied command and streams its stdout as text.

    The template is split with shlex *before* placeholders are substituted, so
    a prompt containing spaces or quotes stays a single argv element and never
    reaches a shell.

        cli_command = "aider --model {model} --message {prompt}"

    A template with no {prompt} gets the prompt appended as the last argument.
    """

    name = "generic"
    display_name = "Generic CLI"
    supports_resume = False
    supports_tool_events = False

    def __init__(self, command: str = ""):
        self.command = command.strip()

    def find_exe(self) -> str | None:
        if not self.command:
            return None
        first = shlex.split(self.command)[0]
        return shutil.which(first) or (first if "/" in first else None)

    def build(self, *, exe, prompt, cwd, model="", resume=None,
              append_system_prompt="", bypass_permissions=False) -> Launch:
        tokens = shlex.split(self.command)
        tokens[0] = exe
        argv: list[str] = []
        saw_prompt = False
        for tok in tokens:
            if "{prompt}" in tok:
                saw_prompt = True
            argv.append(
                tok.replace("{prompt}", prompt)
                   .replace("{model}", model or "")
                   .replace("{cwd}", cwd or "")
            )
        if not saw_prompt:
            argv.append(prompt)
        return Launch(argv=argv)

    def parse_line(self, line: str) -> ParsedLine:
        return ParsedLine(
            events=[{"type": "text", "text": line[:500]}],
            text_chunk=line,
        )


def _hint(inp: dict) -> str:
    """One-line summary of a tool call's input, for the UI."""
    for k in ("command", "file_path", "path", "pattern", "url", "query"):
        v = inp.get(k)
        if isinstance(v, str) and v:
            return v[:160]
    try:
        return json.dumps(inp)[:160]
    except Exception:
        return ""


def get_adapter(name: str, command: str = "") -> CLIAdapter:
    """Resolve an adapter by config name. Unknown names fall back to generic."""
    n = (name or "claude").strip().lower()
    if n in ("claude", "claude-code", "claude_code"):
        return ClaudeCodeAdapter()
    return GenericCLIAdapter(command)


ADAPTERS = {"claude": ClaudeCodeAdapter, "generic": GenericCLIAdapter}
