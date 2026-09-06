"""Stage execution for the Investigations pipeline.

The first executable stage is **source investigation**, and it is read-only:
a model is given `list_dir` / `read_file` / `search_code` tools confined to the
case's source tree and loops until it submits an analysis. Because it only
*reads* files (never runs code), it runs as a server-side provider tool-use
loop — no executor, no sandbox. PoC execution (running code) is a later,
carefully-sandboxed stage.

Runs are drained by a single in-process thread pool (`pipeline_max_concurrent`)
— the single-executor, one-long-queue model. Scale by moving this to a
Celery/RQ worker pool; the `run_stage` boundary already isolates it.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import crypto, models
from .ai import scope
from .ai.base import get_provider
from .ai.errors import AIProviderError
from .ai.tools import ToolResult, ToolSpec
from .config import settings

log = logging.getLogger("irs.pipeline")

_pool = ThreadPoolExecutor(
    max_workers=max(1, settings.pipeline_max_concurrent),
    thread_name_prefix="stage",
)


def submit(run_id: str) -> None:
    """Queue a stage run for the executor. Returns immediately."""
    _pool.submit(_run_guarded, run_id)


def _run_guarded(run_id: str) -> None:
    from .database import SessionLocal

    db = SessionLocal()
    try:
        run = db.get(models.StageRun, run_id)
        if run is None:
            return
        run_stage(db, run)
    except Exception:  # never let a worker thread die silently
        log.exception("stage run %s crashed", run_id)
    finally:
        db.close()


# ---------------------------------------------------------------- confinement

def _confine(base: Path, rel: str) -> Path:
    """Resolve `rel` under `base`, refusing any escape. Raises ValueError."""
    base_r = base.resolve()
    target = (base_r / (rel or "").lstrip("/")).resolve()
    if target != base_r and base_r not in target.parents:
        raise ValueError(f"path escapes the source tree: {rel!r}")
    return target


# ------------------------------------------------------------- source tools

def _source_root(db, case: models.Case) -> Path:
    src = case.source
    if not src or src.kind != models.CaseSourceKind.local_path:
        raise RuntimeError("source investigation needs a local-path source")
    if not src.workspace_key:
        raise RuntimeError("source is not resolved (attach a local path first)")
    root = Path(src.workspace_key)
    if not root.is_dir():
        raise RuntimeError(f"source path is not available: {root}")
    return root


_SOURCE_TOOLS = [
    ToolSpec(
        name="list_dir",
        description="List entries (files and subdirectories) of a directory in the source tree. Use '' for the root.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory path relative to the source root."}},
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="read_file",
        description="Read a UTF-8 text file from the source tree (first ~256KB). Returns the content with line numbers.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path relative to the source root."}},
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="search_code",
        description="Search the source tree for a fixed string or regex and return matching file:line locations.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Text or regular expression to search for."},
            },
            "required": ["pattern"],
        },
    ),
    ToolSpec(
        name="submit_analysis",
        description="Submit the finished analysis of how this finding manifests in the source. Call exactly once when done.",
        parameters={
            "type": "object",
            "properties": {
                "affected_component": {"type": "string", "description": "The file/module/component where the vuln lives (e.g. 'api/upload.py')."},
                "summary": {"type": "string", "description": "Markdown: what the code does, why it's vulnerable, and the exact locations. Cite file:line."},
                "key_locations": {"type": "array", "items": {"type": "string"}, "description": "file:line references central to the finding."},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": ["summary"],
        },
    ),
]


def _tool_list_dir(root: Path, args: dict) -> str:
    target = _confine(root, args.get("path", ""))
    if not target.is_dir():
        return f"not a directory: {args.get('path')!r}"
    out = []
    for e in sorted(os.scandir(target), key=lambda e: (not e.is_dir(), e.name.lower())):
        if e.name.startswith("."):
            continue
        out.append(f"{e.name}/" if e.is_dir(follow_symlinks=False) else e.name)
    return "\n".join(out) if out else "(empty)"


def _tool_read_file(root: Path, args: dict) -> str:
    target = _confine(root, args.get("path", ""))
    if not target.is_file():
        return f"not a file: {args.get('path')!r}"
    data = target.read_bytes()[: settings.pipeline_max_file_bytes]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"(binary file, {target.stat().st_size} bytes — not shown)"
    lines = text.splitlines()
    numbered = "\n".join(f"{i:>5}  {ln}" for i, ln in enumerate(lines, 1))
    if target.stat().st_size > len(data):
        numbered += "\n… (truncated)"
    return numbered


def _tool_search_code(root: Path, args: dict) -> str:
    pattern = (args.get("pattern") or "").strip()
    if not pattern:
        return "empty pattern"
    # ripgrep if present (fast, respects .gitignore); else a bounded Python walk.
    try:
        r = subprocess.run(
            ["rg", "--no-heading", "--line-number", "--color", "never", "-e", pattern, "."],
            cwd=str(root), capture_output=True, text=True, timeout=20,
        )
        if r.returncode in (0, 1):
            hits = r.stdout.splitlines()[:200]
            return "\n".join(hits) if hits else "no matches"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return _py_search(root, pattern)


def _py_search(root: Path, pattern: str) -> str:
    import re
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    hits, scanned = [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if scanned > 5000 or len(hits) >= 200:
                break
            fp = Path(dirpath) / fn
            scanned += 1
            try:
                for i, line in enumerate(fp.read_text("utf-8", "ignore").splitlines(), 1):
                    if rx.search(line):
                        rel = fp.relative_to(root)
                        hits.append(f"{rel}:{i}:{line.strip()[:200]}")
                        if len(hits) >= 200:
                            break
            except (OSError, UnicodeDecodeError):
                continue
    return "\n".join(hits) if hits else "no matches"


_SOURCE_SYSTEM = (
    "You are a senior application-security engineer investigating a reported "
    "vulnerability against the product's source code. You have read-only tools "
    "to list directories, read files, and search the tree. Work from the "
    "reported finding: locate the vulnerable code, understand how it manifests, "
    "and identify the exact file/component and line locations. Do not speculate "
    "beyond what the code shows. When you are confident, call submit_analysis "
    "exactly once. Be concise and cite file:line."
)


def _run_source_stage(db, run: models.StageRun) -> str:
    """Read-only source-investigation loop. Returns the analysis markdown."""
    case = db.get(models.Case, run.case_id)
    finding = db.get(models.Finding, run.finding_id) if run.finding_id else None
    if finding is None:
        raise RuntimeError("source investigation needs a finding")
    root = _source_root(db, case)

    choice = scope.for_stage(db, run.id)
    provider = get_provider(choice.provider, choice.model)

    report = crypto.decrypt(case.report_enc).decode("utf-8", "replace") if case.report_enc else ""
    user = (
        f"# Reported finding\n\nTitle: {finding.title or '(none)'}\n"
        f"Severity: {finding.severity.value}\n"
        f"CWE: {finding.cwe or '(none)'}\n"
        f"Reporter description:\n{finding.description or '(none)'}\n\n"
        f"# Inbound bug report\n\n{report or '(none)'}\n\n"
        "Investigate this against the source tree and submit your analysis."
    )

    convo = provider.start_tools(_SOURCE_SYSTEM, _SOURCE_TOOLS, max_tokens=8192)
    turn = convo.send_user(user)

    handlers = {
        "list_dir": _tool_list_dir,
        "read_file": _tool_read_file,
        "search_code": _tool_search_code,
    }
    analysis: dict | None = None

    for _ in range(settings.pipeline_max_iterations):
        if not turn.wants_tools:
            break
        results = []
        for call in turn.tool_calls:
            if call.name == "submit_analysis":
                analysis = call.arguments
                results.append(ToolResult(call_id=call.id, content="analysis recorded"))
            elif call.name in handlers:
                try:
                    content = handlers[call.name](root, call.arguments)
                except ValueError as e:  # confinement / bad path
                    content = f"error: {e}"
                results.append(ToolResult(call_id=call.id, content=content))
            else:
                results.append(ToolResult(call_id=call.id, content=f"unknown tool {call.name!r}", is_error=True))
        if analysis is not None:
            break
        turn = convo.send_tool_results(results)

    if analysis is None:
        # Model stopped without submitting; keep whatever prose it produced.
        text = (turn.text or "").strip()
        if not text:
            raise RuntimeError("the model finished without submitting an analysis")
        analysis = {"summary": text}

    # Persist: non-destructive Finding update + the full analysis as output.
    ac = (analysis.get("affected_component") or "").strip()
    if ac and not finding.affected_component:
        finding.affected_component = ac[:512]

    parts = [f"# Source investigation — {finding.title or 'finding'}", ""]
    if ac:
        parts.append(f"**Affected component:** {ac}")
    if analysis.get("confidence"):
        parts.append(f"**Confidence:** {analysis['confidence']}")
    parts += ["", analysis.get("summary", "").strip()]
    locs = analysis.get("key_locations") or []
    if locs:
        parts += ["", "**Key locations:**", *[f"- `{l}`" for l in locs]]
    return "\n".join(parts).strip()


# --------------------------------------------------------------- dispatch

_RUNNERS = {
    models.StageType.source: _run_source_stage,
}


def run_stage(db, run: models.StageRun) -> None:
    """Execute one stage run synchronously, updating its lifecycle + output."""
    runner = _RUNNERS.get(run.stage)
    run.status = models.StageRunStatus.running
    run.started_at = dt.datetime.now(dt.timezone.utc)
    run.error = ""
    db.commit()

    if runner is None:
        run.status = models.StageRunStatus.error
        run.error = f"the {run.stage.value} stage is not implemented yet"
        run.completed_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
        return

    try:
        output = runner(db, run)
        run.output_enc = crypto.encrypt(output.encode("utf-8"))
        run.status = models.StageRunStatus.done
    except AIProviderError as e:
        run.status = models.StageRunStatus.error
        run.error = e.detail(is_admin=False)[:2000]
    except Exception as e:
        log.exception("stage %s (%s) failed", run.id, run.stage.value)
        run.status = models.StageRunStatus.error
        run.error = f"{type(e).__name__}: {e}"[:2000]
    run.completed_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
