"""Claude Code PostToolUse hook integration.

Two entry points:

- `submit_from_stdin(conf)`: reads the JSON payload Claude Code pipes to its
  PostToolUse hook, and if the tool was a Write/Edit on a `.md` file, hands
  the file to the existing Uploader. Exits 0 even on failure so we never
  break the user's Claude session.

- `install(remove=False)`: idempotently inserts (or removes) our hook entry
  in `~/.claude/settings.json`. Identifies our entry by the command string
  containing the marker `HOOK_CMD_MARKER`.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("irs-agent.hook")

# Substring used to recognise our own hook entry on (re)install or removal.
HOOK_CMD_MARKER = "irs-agent hook-submit"

# Tools whose output we want to capture.
_MATCHED_TOOLS = {"Write", "Edit", "MultiEdit"}


def _agent_bin() -> str:
    """Absolute path to the irs-agent script. Used when installing the hook."""
    # sys.argv[0] is "irs-agent" (or full path); fall back to executable + entry-point name.
    p = Path(sys.argv[0])
    if p.is_absolute() and p.exists():
        return str(p)
    # Common case: venv install — irs-agent is next to the current python interpreter.
    cand = Path(sys.executable).with_name("irs-agent")
    if cand.exists():
        return str(cand)
    return "irs-agent"


# ---------- runtime (called by Claude on every matching tool use) ----------

def submit_from_stdin(conf) -> int:
    """Read the hook payload, decide if it's a .md write/edit, and upload."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw else {}
    except Exception as e:
        print(f"irs-agent hook: bad payload: {e}", file=sys.stderr)
        return 0  # never break Claude

    tool_name = payload.get("tool_name")
    if tool_name not in _MATCHED_TOOLS:
        return 0

    tool_input = payload.get("tool_input") or {}
    path_str = tool_input.get("file_path") or payload.get("tool_response", {}).get("filePath")
    if not path_str:
        return 0

    p = Path(path_str)
    if not p.exists() or not p.is_file():
        return 0
    from .filters import should_skip
    if should_skip(p):
        return 0

    session_id = payload.get("session_id") or None
    try:
        from .uploader import Uploader
        up = Uploader(conf)
        if p.suffix.lower() == ".md":
            up.maybe_upload(p, "claude_code", session_id=session_id)
        elif _is_poc_path(p):
            up.maybe_upload_attachment(p, session_id=session_id)
        # else: silently ignore — we don't want to ship every file Claude touches.
    except Exception as e:
        # Log but don't propagate — Claude shouldn't see this as a failure.
        print(f"irs-agent hook: upload of {p} failed: {e}", file=sys.stderr)
    return 0


def _is_poc_path(p: Path) -> bool:
    """True if any path segment (case-insensitive) is exactly `poc` —
    matches `~/work/run-1/poc/F-1/crash.bin` and `~/reports/poc/x.zip`
    but not `~/poc-template.md` (extension already filtered separately)."""
    return any(seg.lower() == "poc" for seg in p.parts)


# ---------- installer (idempotent settings.json mutation) ----------

def _settings_path() -> Path:
    return Path(os.path.expanduser("~/.claude/settings.json"))


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"irs-agent: {path} is not valid JSON ({e}); refusing to touch it. "
            "Fix the file manually and re-run."
        )


def _save_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _our_hook_entry(agent_bin: str) -> dict:
    return {
        "matcher": "Write|Edit|MultiEdit",
        "hooks": [
            {
                "type": "command",
                "command": f"{agent_bin} hook-submit",
            }
        ],
    }


def _is_ours(entry: dict) -> bool:
    """Match by marker substring in any command inside this entry's hooks."""
    for h in entry.get("hooks", []) or []:
        cmd = h.get("command") or ""
        if HOOK_CMD_MARKER in cmd:
            return True
    return False


def install(remove: bool = False) -> int:
    path = _settings_path()
    data = _load_settings(path)

    hooks_block = data.setdefault("hooks", {})
    post_tool = hooks_block.setdefault("PostToolUse", [])
    if not isinstance(post_tool, list):
        print(
            f"irs-agent: unexpected shape in {path} (hooks.PostToolUse is not a list); "
            "refusing to touch it.",
            file=sys.stderr,
        )
        return 1

    # Strip any existing entries that we own (covers both --remove and re-install).
    before = len(post_tool)
    post_tool[:] = [e for e in post_tool if not _is_ours(e)]
    stripped = before - len(post_tool)

    if remove:
        if stripped == 0:
            print(f"irs-agent: no hook to remove in {path}")
        else:
            _save_settings(path, data)
            print(f"irs-agent: removed {stripped} hook entry(s) from {path}")
        return 0

    post_tool.append(_our_hook_entry(_agent_bin()))
    _save_settings(path, data)
    if stripped:
        print(f"irs-agent: refreshed Claude Code hook in {path}")
    else:
        print(f"irs-agent: installed Claude Code hook in {path}")
    print(
        "  Triggers on Write/Edit/MultiEdit of *.md and submits to Verdict.\n"
        "  To remove later: irs-agent install-claude-hook --remove"
    )
    return 0
