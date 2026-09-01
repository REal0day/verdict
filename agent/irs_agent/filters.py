"""Path filters shared by the watcher, hook, and one-shot importer.

The collector watches `~/.claude/projects/` by default. Claude Code stores
its OWN bookkeeping there — session transcripts (`*.jsonl`, already filtered
by the `*.md` glob) and per-project memory files (`memory/*.md`, plus a
top-level `MEMORY.md`). Those aren't user-authored reports and shouldn't be
shipped to the server. This module is the single point of truth for "which
markdowns under .claude/ to ignore".
"""
from __future__ import annotations

from pathlib import Path

_SKIP_BASENAMES = {"MEMORY.md"}


def should_skip(path: Path) -> bool:
    """Return True if `path` is something the agent should NOT auto-upload."""
    parts = path.parts
    # Drop anything inside a `memory/` directory anywhere in the tree —
    # that's where Claude (and our own auto-memory) writes bookkeeping.
    if any(seg.lower() == "memory" for seg in parts):
        return True
    if path.name in _SKIP_BASENAMES:
        return True
    return False
