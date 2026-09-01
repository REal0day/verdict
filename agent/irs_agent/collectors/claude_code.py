from .base import Collector, register, _home

# Claude Code writes session/project artifacts under ~/.claude/projects/**.
# We watch user-designated output dirs by default + that tree for any .md files.
register(Collector(
    name="claude_code",
    default_paths=[_home(".claude", "projects"), _home("claude-reports")],
    glob="*.md",
))
