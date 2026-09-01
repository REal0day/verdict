"""Claude-driven plan generator for folder uploads.

Given a directory of files a user uploaded, ask Claude to figure out what
should be a report vs. a POC/attachment, whether to group them under a
scan, and which project they belong to. Claude can browse files via a
`read_file` tool so it isn't limited to filenames.

The conversation is bounded:
  - ``max_iterations`` ~ 30 tool calls before we cut Claude off
  - ``read_file`` capped at ``MAX_READ_BYTES`` per call so a single huge
    file can't blow the context window

Returns the validated plan dict (see ``PLAN_SCHEMA``) plus a short log of
which files Claude inspected, which we save for the UI.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
from typing import Any


from .base import get_provider
from .tools import ToolResult, ToolSpec

log = logging.getLogger("irs.import_planner")

MAX_READ_BYTES = 32_000
MAX_ITERATIONS = 40
# Per Claude prompt: stay generous — full small files preferred, head-only
# for medium, just metadata for huge.
TREE_INLINE_PREVIEW = 4_000
# Upper bound on how many files we enumerate into the planning prompt. A real
# source tree has thousands of files; listing them all overflows the context
# window. We list the highest-priority (report/doc-like) files up to this cap
# and summarize the rest. Files not listed are simply left unimported.
MAX_TREE_FILES = 1_500

# Dependency / VCS / build dirs that are noise for planning and balloon the
# file count. Pruned from the tree the planner sees (still on disk).
_NOISE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "bower_components", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".venv", "venv",
    "env", "dist", "build", "out", "target", "vendor", ".next", ".nuxt",
    ".gradle", ".idea", ".vscode", ".terraform", "site-packages", "coverage",
    ".cache", ".parcel-cache", ".turbo",
}

# Names/extensions that look like reports, notes, or POCs — what the planner
# actually classifies — so we surface them first when the tree is capped.
_DOC_EXTS = {
    ".md", ".markdown", ".txt", ".rst", ".json", ".csv", ".tsv", ".html",
    ".htm", ".log", ".out", ".pdf", ".xlsx", ".docx", ".yaml", ".yml",
}
_REPORTY = (
    "readme", "summary", "finding", "report", "triage", "note", "security",
    "audit", "poc", "exploit", "vuln", "advisory", "writeup",
)


def _doc_priority(relpath: str) -> int:
    """Lower sorts first. Report/doc-like files outrank bulk source code."""
    name = relpath.rsplit("/", 1)[-1].lower()
    ext = ("." + name.rsplit(".", 1)[-1]) if "." in name else ""
    score = 0
    if any(k in name for k in _REPORTY):
        score -= 2
    if ext in _DOC_EXTS:
        score -= 1
    return score


# Files Claude shouldn't waste turns trying to read (binary, oversize, etc.).
# We still surface them in the tree so the plan can mark them as POCs.
_BINARY_EXTS = {
    ".exe", ".dll", ".so", ".dylib", ".bin", ".pyc", ".class",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".tiff", ".bmp", ".pdf",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".mp3", ".mp4", ".wav", ".mov", ".avi",
    ".db", ".sqlite", ".sqlite3",
}


def _is_text_path(name: str) -> bool:
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    return ext not in _BINARY_EXTS


def _walk_tree(root: str) -> list[dict]:
    out: list[dict] = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        # prune noise dirs in place so os.walk doesn't descend into them
        dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS]
        for f in filenames:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            out.append({
                "relpath": rel,
                "size": size,
                "mime": mimetypes.guess_type(f)[0] or "",
            })
    out.sort(key=lambda x: x["relpath"])
    return out


# ----- tool definitions for Claude -----
_TOOL_DEFS = [
    {
        "name": "read_file",
        "description": (
            "Read the first ~32KB of a staged file by its relative path. "
            "Use this to understand a file's content before deciding what "
            "it is. Returns text content for text-y files; for binaries "
            "returns a hexdump-ish note."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "relpath": {
                    "type": "string",
                    "description": "Relative path inside the upload (use forward slashes).",
                },
            },
            "required": ["relpath"],
        },
    },
    {
        "name": "submit_plan",
        "description": (
            "Submit the final import plan. Call this exactly once when "
            "you've decided how to organize every file. After this is "
            "called, your turn ends."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "object",
                    "description": (
                        "Where the imported artefacts should live. Pick "
                        "kind='existing' to use an existing project, "
                        "kind='new' to create one, kind='none' to leave "
                        "everything unassigned."
                    ),
                    "properties": {
                        "kind": {"type": "string", "enum": ["existing", "new", "none"]},
                        "existing_id": {"type": "string"},
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["kind"],
                },
                "scans": {
                    "type": "array",
                    "description": (
                        "Zero or more VulnScans to create. Reports and runs "
                        "reference these by their local_id."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "local_id": {"type": "string"},
                            "product": {"type": "string"},
                            "scan_target": {"type": "string"},
                            "harness_used": {"type": "string"},
                            "scan_by": {"type": "string"},
                            "notes": {"type": "string"},
                            "rationale": {"type": "string"},
                        },
                        "required": ["local_id"],
                    },
                },
                "runs": {
                    "type": "array",
                    "description": (
                        "Optional RunLog rows to create under a scan "
                        "(typical for day-by-day fuzzing runs)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "scan_local_id": {"type": "string"},
                            "day": {"type": "string"},
                            "date": {"type": "string"},
                            "run": {"type": "string"},
                            "box": {"type": "string"},
                            "product": {"type": "string"},
                            "harness": {"type": "string"},
                            "prompt": {"type": "string"},
                            "results": {"type": "string"},
                            "poc": {"type": "string"},
                            "comment": {"type": "string"},
                            "complete": {"type": "boolean"},
                        },
                        "required": ["scan_local_id"],
                    },
                },
                "items": {
                    "type": "array",
                    "description": (
                        "Per-file dispositions. EVERY file in the tree "
                        "must appear here exactly once."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "relpath": {"type": "string"},
                            "kind": {
                                "type": "string",
                                "enum": ["report", "poc", "skip"],
                            },
                            "local_id": {
                                "type": "string",
                                "description": (
                                    "Local handle for this report so a POC "
                                    "can be attached to it. Required when "
                                    "kind=='report'."
                                ),
                            },
                            "title": {"type": "string"},
                            "scan_local_id": {
                                "type": "string",
                                "description": (
                                    "If this report belongs to a scan from "
                                    "the `scans` list, the scan's local_id."
                                ),
                            },
                            "attach_to_local_id": {
                                "type": "string",
                                "description": (
                                    "When kind=='poc', the local_id of the "
                                    "report this attaches to. Optional — "
                                    "leave blank to attach to the scan only."
                                ),
                            },
                            "rationale": {"type": "string"},
                        },
                        "required": ["relpath", "kind"],
                    },
                },
            },
            "required": ["project", "items"],
        },
    },
]


_SYSTEM_PROMPT = """\
You are an import-organizer for a vulnerability tracking system (Verdict).
A user uploaded a folder of files. Your job: figure out the best way to
import every file. Specifically:

1. Decide what each file is — a vulnerability report (markdown/text), a
   POC/attachment (binary, script, crash input, screenshot, log), or
   something that should be skipped entirely.
2. Group related files. If you see day-by-day fuzzing notes, multiple
   reports about the same product, or a `poc/` directory next to a
   report, propose a VulnScan and attach the right files to it.
3. Pick (or invent) a project. Prefer an existing project when one
   matches the content; if nothing fits, propose a new project with a
   short, content-derived name.
4. Use `read_file` aggressively — at least open every markdown file
   you intend to mark as a report. Don't guess from filenames alone.
5. Be conservative: when unsure, mark the file as "skip" and explain
   in `rationale` so the user can decide.

When you're done, call `submit_plan` EXACTLY ONCE with the full plan.
After that, your turn ends. Do not narrate your reasoning in plain text
to the user; everything important should be in the rationale fields of
the plan.
"""


TOOLS = [
    ToolSpec(name=t["name"], description=t["description"], parameters=t["parameters"])
    for t in _TOOL_DEFS
]

def _build_initial_user_message(
    files: list[dict],
    existing_projects: list[dict],
    *,
    user_label: str,
) -> str:
    lines = []
    lines.append(f"Upload label: {user_label or '(unset)'}")
    lines.append(f"File count: {len(files)}")
    lines.append("")
    lines.append("Existing projects this user can attach to:")
    if existing_projects:
        for p in existing_projects:
            d = (p.get("description") or "").strip().replace("\n", " ")
            lines.append(f"  - id={p['id']}  name={p['name']!r}  desc={d[:120]!r}")
    else:
        lines.append("  (none — you'll need kind='new' or kind='none')")
    lines.append("")

    # Cap the enumerated tree so a large source upload doesn't overflow the
    # context window. Surface report/doc-like files first; summarize the rest.
    ordered = sorted(files, key=lambda f: (_doc_priority(f["relpath"]), f["relpath"]))
    shown = ordered[:MAX_TREE_FILES]
    omitted = ordered[MAX_TREE_FILES:]

    if omitted:
        lines.append(
            f"File tree ({len(files)} files total; showing the {len(shown)} most "
            "report/doc-like — the rest are bulk source code, summarized below):"
        )
    else:
        lines.append("Full file tree (relpath, size):")
    for f in shown:
        lines.append(f"  {f['relpath']}  ({f['size']} bytes)")

    if omitted:
        # by top-level dir, then by extension, so Claude understands the shape
        from collections import Counter
        by_dir: Counter = Counter()
        by_ext: Counter = Counter()
        for f in omitted:
            top = f["relpath"].split("/", 1)[0] if "/" in f["relpath"] else "(root)"
            by_dir[top] += 1
            name = f["relpath"].rsplit("/", 1)[-1]
            ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else "(none)"
            by_ext[ext] += 1
        lines.append("")
        lines.append(f"... plus {len(omitted)} more files not listed individually:")
        lines.append("  by top-level dir: " + ", ".join(
            f"{d} ({n})" for d, n in by_dir.most_common(20)))
        lines.append("  by extension: " + ", ".join(
            f"{e} ({n})" for e, n in by_ext.most_common(15)))

    lines.append("")
    lines.append(
        "Start by reading the markdown/text files that look like reports or "
        "findings, then submit_plan. NOTE: you only need to disposition the "
        "files listed individually above; any files referenced only in the "
        "summary are bulk source code and will be left unimported — do not try "
        "to read or list them. For a source-code upload it's fine to import "
        "just the report/notes files (or none) and attach them to the product."
    )
    return "\n".join(lines)


def _safe_join(staging_root: str, relpath: str) -> str | None:
    base = os.path.abspath(staging_root)
    target = os.path.abspath(os.path.join(base, relpath))
    if not target.startswith(base + os.sep) and target != base:
        return None
    return target


def _read_for_claude(staging_root: str, relpath: str) -> str:
    target = _safe_join(staging_root, relpath)
    if not target:
        return f"ERROR: refusing path escape: {relpath!r}"
    if not os.path.isfile(target):
        return f"ERROR: not a file: {relpath!r}"
    try:
        with open(target, "rb") as fh:
            raw = fh.read(MAX_READ_BYTES + 1)
    except OSError as e:
        return f"ERROR: read failed ({e})"

    truncated = len(raw) > MAX_READ_BYTES
    raw = raw[:MAX_READ_BYTES]

    if not _is_text_path(relpath):
        return (
            f"(binary file, {os.path.getsize(target)} bytes, "
            f"showing first {min(len(raw), 64)} bytes as hex)\n"
            + raw[:64].hex()
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")

    suffix = "\n\n[... truncated, file is larger than 32KB ...]" if truncated else ""
    return text + suffix


def plan_folder(
    staging_root: str,
    *,
    existing_projects: list[dict],
    user_label: str = "",
    provider_name: str | None = None,
    model: str | None = None,
) -> tuple[dict, str]:
    """Run the planner. Returns (plan, log_text).

    Runs against whichever provider is configured, via the tool-use
    abstraction in `ai/tools.py`. Raises if the model doesn't produce a valid
    plan within the iteration budget.
    """
    provider = get_provider(provider_name, model)

    files = _walk_tree(staging_root)
    if not files:
        raise RuntimeError("upload appears to be empty")

    log_lines: list[str] = []
    log_lines.append(f"tree: {len(files)} files")

    convo = provider.start_tools(_SYSTEM_PROMPT, TOOLS, max_tokens=8192)
    first_message = _build_initial_user_message(
        files, existing_projects, user_label=user_label,
    )

    final_plan: dict | None = None
    turn = convo.send_user(first_message)

    for it in range(MAX_ITERATIONS):
        log.info(
            "planner iter=%d stop=%s tool_calls=%d",
            it, turn.stop_reason, len(turn.tool_calls),
        )

        if not turn.wants_tools:
            log_lines.append(
                f"iter {it}: model stopped ({turn.stop_reason}) without calling "
                "submit_plan; aborting"
            )
            break

        results: list[ToolResult] = []
        for call in turn.tool_calls:
            if call.name == "read_file":
                rel = call.arguments.get("relpath", "")
                content = _read_for_claude(staging_root, rel)
                log_lines.append(f"iter {it}: read_file {rel!r} -> {len(content)} chars")
                results.append(ToolResult(call_id=call.id, content=content))
            elif call.name == "submit_plan":
                final_plan = call.arguments
                log_lines.append(f"iter {it}: submit_plan received")
                results.append(ToolResult(call_id=call.id, content="plan recorded"))
            else:
                results.append(ToolResult(
                    call_id=call.id,
                    content=f"unknown tool {call.name!r}",
                    is_error=True,
                ))

        if final_plan is not None:
            break

        turn = convo.send_tool_results(results)

    if final_plan is None:
        raise RuntimeError("planner finished without submitting a plan")

    _validate_plan(final_plan, files)
    return final_plan, "\n".join(log_lines)


def _validate_plan(plan: dict, files: list[dict]) -> None:
    """Lightweight shape check so the SPA + confirm endpoint can trust it.

    Be lenient about the top-level keys: a source-code upload often has no
    reports, so Claude may legitimately submit_plan with an empty/absent
    `items` and no `project`. We coerce those to safe defaults rather than
    failing — a pre-pinned product overrides the project anyway, and missing
    files default to "skip" below."""
    if not isinstance(plan.get("project"), dict):
        plan["project"] = {"kind": "none"}
    if not isinstance(plan.get("items"), list):
        plan["items"] = []
    p = plan["project"]
    if p.get("kind") not in {"existing", "new", "none"}:
        p["kind"] = "none"
    items = plan["items"]
    rel_seen: set[str] = set()
    valid_rel = {f["relpath"] for f in files}
    for it in items:
        rel = it.get("relpath")
        if rel not in valid_rel:
            raise ValueError(f"plan references unknown file {rel!r}")
        if rel in rel_seen:
            raise ValueError(f"plan has duplicate item for {rel!r}")
        rel_seen.add(rel)
        if it.get("kind") not in {"report", "poc", "skip"}:
            raise ValueError(
                f"plan item {rel!r} has invalid kind {it.get('kind')!r}"
            )
    # Tolerate Claude forgetting a file: anything missing gets implicit "skip".
    for f in files:
        if f["relpath"] not in rel_seen:
            items.append({
                "relpath": f["relpath"],
                "kind": "skip",
                "rationale": "(no decision from planner — defaulted to skip)",
            })
