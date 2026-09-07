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

from . import crypto, cvss, models
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


# ----------------------------------------------------------- shared helpers

def _provider(db, run):
    # Uses the module-level get_provider (imported at top) so tests can
    # monkeypatch pipeline.get_provider.
    choice = scope.for_stage(db, run.id)
    return get_provider(choice.provider, choice.model)


def _finding_ctx(case, finding) -> str:
    report = crypto.decrypt(case.report_enc).decode("utf-8", "replace") if case.report_enc else ""
    return (
        f"# Reported finding\n\nTitle: {finding.title or '(none)'}\n"
        f"Severity: {finding.severity.value}\nCWE: {finding.cwe or '(none)'}\n"
        f"Description:\n{finding.description or '(none)'}\n\n"
        f"# Inbound bug report\n\n{report or '(none)'}\n"
    )


def _prior(db, run, stage) -> str:
    """Latest done output of `stage` for this run's finding, decrypted."""
    q = (db.query(models.StageRun)
           .filter(models.StageRun.case_id == run.case_id,
                   models.StageRun.stage == stage,
                   models.StageRun.status == models.StageRunStatus.done)
           .order_by(models.StageRun.created_at.desc()))
    if run.finding_id:
        q = q.filter(models.StageRun.finding_id == run.finding_id)
    row = q.first()
    if row and row.output_enc:
        return crypto.decrypt(row.output_enc).decode("utf-8", "replace")
    return ""


def _run_read_tool_loop(db, run, provider, system, extra_tools, user, terminal_name):
    """Tool-use loop with the read-only source tools plus a terminal tool.

    Returns (terminal_args | None, last_text). The source root is optional:
    when there's no local source, only the terminal tool is offered.
    """
    case = db.get(models.Case, run.case_id)
    try:
        root = _source_root(db, case)
    except RuntimeError:
        root = None
    tools = ([] if root is None else list(_SOURCE_TOOLS[:3])) + extra_tools
    handlers = {"list_dir": _tool_list_dir, "read_file": _tool_read_file, "search_code": _tool_search_code}

    convo = provider.start_tools(system, tools, max_tokens=8192)
    turn = convo.send_user(user)
    final = None
    for _ in range(settings.pipeline_max_iterations):
        if not turn.wants_tools:
            break
        results = []
        for call in turn.tool_calls:
            if call.name == terminal_name:
                final = call.arguments
                results.append(ToolResult(call_id=call.id, content="recorded"))
            elif call.name in handlers and root is not None:
                try:
                    content = handlers[call.name](root, call.arguments)
                except ValueError as e:
                    content = f"error: {e}"
                results.append(ToolResult(call_id=call.id, content=content))
            else:
                results.append(ToolResult(call_id=call.id, content=f"unknown tool {call.name!r}", is_error=True))
        if final is not None:
            break
        turn = convo.send_tool_results(results)
    return final, (turn.text or "").strip()


# ------------------------------------------------------------- impact stage

_IMPACT_SYSTEM = (
    "You are a senior application-security engineer scoring the impact of a "
    "confirmed vulnerability. Use the read-only source tools if they help you "
    "judge attack vector, privileges, or scope. Then call submit_impact ONCE "
    "with: a CVSS 3.1 base vector, a CVSS 4.0 base vector, a CWE id, and — for "
    "each metric in each vector — one short sentence of justification. Keep "
    "each rationale to a single sentence. Do not inflate severity."
)

_IMPACT_TOOL = ToolSpec(
    name="submit_impact",
    description="Submit the CVSS assessment. Call exactly once.",
    parameters={
        "type": "object",
        "properties": {
            "cvss31_vector": {"type": "string", "description": "Full CVSS:3.1/... base vector."},
            "cvss40_vector": {"type": "string", "description": "Full CVSS:4.0/... base vector."},
            "cvss40_score": {"type": "number", "description": "Your CVSS 4.0 base score estimate (0-10)."},
            "cwe": {"type": "string", "description": "Primary CWE, e.g. 'CWE-434'."},
            "cvss31_rationale": {
                "type": "array", "description": "One entry per 3.1 metric.",
                "items": {"type": "object", "properties": {
                    "metric": {"type": "string"}, "value": {"type": "string"},
                    "reason": {"type": "string", "description": "one sentence"}}},
            },
            "cvss40_rationale": {
                "type": "array", "description": "One entry per 4.0 metric.",
                "items": {"type": "object", "properties": {
                    "metric": {"type": "string"}, "value": {"type": "string"},
                    "reason": {"type": "string", "description": "one sentence"}}},
            },
            "summary": {"type": "string", "description": "One-paragraph impact narrative."},
        },
        "required": ["cvss31_vector", "cwe"],
    },
)


def _fmt_rationale(rows) -> str:
    out = []
    for r in rows or []:
        m = r.get("metric", "?"); v = r.get("value", "?"); why = (r.get("reason") or "").strip()
        out.append(f"- **{m}:{v}** — {why}")
    return "\n".join(out)


def _run_impact_stage(db, run) -> str:
    case = db.get(models.Case, run.case_id)
    finding = db.get(models.Finding, run.finding_id) if run.finding_id else None
    if finding is None:
        raise RuntimeError("impact scoring needs a finding")
    provider = _provider(db, run)

    prior = _prior(db, run, models.StageType.source)
    user = _finding_ctx(case, finding)
    if prior:
        user += f"\n# Prior source investigation\n\n{prior}\n"
    user += "\nScore this finding and call submit_impact."

    args, text = _run_read_tool_loop(db, run, provider, _IMPACT_SYSTEM, [_IMPACT_TOOL], user, "submit_impact")
    if args is None:
        raise RuntimeError("the model finished without submitting an impact assessment")

    # CVSS 3.1: authoritative server-side score from the vector.
    v31 = (args.get("cvss31_vector") or "").strip()
    score31 = sev31 = None
    try:
        score31, sev31, _ = cvss.cvss31_base(v31)
        finding.cvss31_vector = v31[:128]
        finding.cvss31_score = score31
        finding.severity = models.Severity(sev31 if sev31 != "none" else "info")
    except cvss.CVSSError as e:
        v31_err = str(e)
    else:
        v31_err = ""

    # CVSS 4.0: validate shape, keep the model's estimate (labelled).
    v40 = (args.get("cvss40_vector") or "").strip()
    v40_ok = False
    if v40:
        try:
            cvss.validate_cvss40(v40)
            v40_ok = True
            finding.cvss40_vector = v40[:160]
            finding.cvss40_score = args.get("cvss40_score")
        except cvss.CVSSError:
            v40_ok = False

    cwe = (args.get("cwe") or "").strip()
    if cwe and not finding.cwe:
        finding.cwe = cwe[:64]

    parts = [f"# Impact — {finding.title or 'finding'}", ""]
    if score31 is not None:
        parts.append(f"**CVSS 3.1:** {score31} ({sev31}) — computed · `{v31}`")
    elif v31:
        parts.append(f"**CVSS 3.1:** `{v31}` — could not compute ({v31_err})")
    if v40:
        label = "model-estimated" if v40_ok else "unvalidated"
        parts.append(f"**CVSS 4.0:** {args.get('cvss40_score', '?')} ({label}) — `{v40}`")
    if cwe:
        parts.append(f"**CWE:** {cwe}")
    if args.get("summary"):
        parts += ["", args["summary"].strip()]
    r31 = _fmt_rationale(args.get("cvss31_rationale"))
    if r31:
        parts += ["", "**CVSS 3.1 rationale:**", r31]
    r40 = _fmt_rationale(args.get("cvss40_rationale"))
    if r40:
        parts += ["", "**CVSS 4.0 rationale:**", r40]
    return "\n".join(parts).strip()


# --------------------------------------------------------- remediation stage

_REMEDIATION_SYSTEM = (
    "You are a senior application-security engineer proposing a remediation for "
    "a confirmed vulnerability. Read the source with the read-only tools to "
    "ground your fix in the real code. Then call submit_remediation ONCE with a "
    "concrete, minimal fix: what to change and where (cite file:line), why it "
    "closes the issue, and any residual risk. Prefer a specific patch over "
    "generic advice."
)

_REMEDIATION_TOOL = ToolSpec(
    name="submit_remediation",
    description="Submit the remediation. Call exactly once.",
    parameters={
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Markdown: the concrete fix, citing file:line."},
            "patch": {"type": "string", "description": "Optional unified-diff or code snippet."},
            "references": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary"],
    },
)


def _run_remediation_stage(db, run) -> str:
    case = db.get(models.Case, run.case_id)
    finding = db.get(models.Finding, run.finding_id) if run.finding_id else None
    if finding is None:
        raise RuntimeError("remediation needs a finding")
    provider = _provider(db, run)

    user = _finding_ctx(case, finding)
    prior = _prior(db, run, models.StageType.source)
    if prior:
        user += f"\n# Prior source investigation\n\n{prior}\n"
    user += "\nPropose the fix and call submit_remediation."

    args, text = _run_read_tool_loop(db, run, provider, _REMEDIATION_SYSTEM,
                                     [_REMEDIATION_TOOL], user, "submit_remediation")
    if args is None:
        if not text:
            raise RuntimeError("the model finished without submitting a remediation")
        args = {"summary": text}

    summary = (args.get("summary") or "").strip()
    if summary and not finding.remediation:
        finding.remediation = summary[:8000]

    parts = [f"# Remediation — {finding.title or 'finding'}", "", summary]
    if args.get("patch"):
        parts += ["", "```", args["patch"].strip(), "```"]
    refs = args.get("references") or []
    if refs:
        parts += ["", "**References:**", *[f"- {r}" for r in refs]]
    return "\n".join(parts).strip()


# -------------------------------------------------------------- report stage

_REPORT_SYSTEM = (
    "You are a senior application-security engineer writing the final report for "
    "an investigation. You are given the case's findings and the outputs of the "
    "impact, source-investigation and remediation stages. Produce a single, "
    "well-structured Markdown report: an executive summary, then per finding a "
    "section covering impact (with CVSS), the technical root cause, and the "
    "remediation. Be precise and cite file:line where the stages did. Output the "
    "report only — no preamble."
)


def _run_report_stage(db, run) -> str:
    case = db.get(models.Case, run.case_id)
    provider = _provider(db, run)
    report = crypto.decrypt(case.report_enc).decode("utf-8", "replace") if case.report_enc else ""

    findings = case.scan.finding_rows if case.scan else []
    blocks = [f"# Case: {case.title or '(untitled)'}", "", f"Inbound report:\n{report or '(none)'}", ""]
    for f in findings:
        blocks.append(f"## Finding: {f.title or '(untitled)'}")
        blocks.append(f"Severity: {f.severity.value}  CWE: {f.cwe or '-'}  "
                      f"CVSS3.1: {f.cvss31_score or '-'} ({f.cvss31_vector or '-'})")
        for label, stage in (("Source investigation", models.StageType.source),
                             ("Impact", models.StageType.impact),
                             ("Remediation", models.StageType.remediation)):
            row = (db.query(models.StageRun)
                     .filter(models.StageRun.finding_id == f.id,
                             models.StageRun.stage == stage,
                             models.StageRun.status == models.StageRunStatus.done)
                     .order_by(models.StageRun.created_at.desc()).first())
            if row and row.output_enc:
                blocks.append(f"### {label}\n" + crypto.decrypt(row.output_enc).decode("utf-8", "replace"))
        blocks.append("")
    context = "\n".join(blocks)[:120_000]

    text = provider.chat(_REPORT_SYSTEM, [{"role": "user", "content": context}], max_tokens=16384)
    text = (text or "").strip()
    if not text:
        raise RuntimeError("the model produced an empty report")

    # Save a downloadable generated Report, like the chat/analytics path.
    raw = text.encode("utf-8")
    fname = f"investigation-{case.id[:8]}.md"
    rpt = models.Report(
        user_id=case.user_id, agent_id=None,
        source_tool=models.SourceTool.generated,
        filename=fname, original_path=None,
        sha256=__import__("hashlib").sha256(raw).hexdigest(),
        size_bytes=len(raw), content_enc=crypto.encrypt(raw),
        project_id=case.project_id,
    )
    db.add(rpt)
    return text


# ----------------------------------------------------------------- poc stage

_POC_SYSTEM = (
    "You are a senior application-security engineer writing a proof-of-concept "
    "for a confirmed vulnerability. Read the source with the read-only tools to "
    "ground the PoC in the real code paths. Then call submit_poc ONCE with a "
    "single self-contained script an analyst can run against a target of their "
    "choosing. The PoC must NOT hardcode a target — take the target host/URL as "
    "an argument or a clearly-marked variable at the top. Add a short usage "
    "comment. Do not run anything; just produce the file."
)

# Map a declared language to a filename extension + content type.
_POC_LANG = {
    "python": (".py", "text/x-python"), "bash": (".sh", "text/x-shellscript"),
    "sh": (".sh", "text/x-shellscript"), "javascript": (".js", "text/javascript"),
    "typescript": (".ts", "text/plain"), "go": (".go", "text/plain"),
    "ruby": (".rb", "text/x-ruby"), "php": (".php", "text/x-php"),
    "http": (".http", "text/plain"), "text": (".txt", "text/plain"),
}

_POC_TOOL = ToolSpec(
    name="submit_poc",
    description="Submit the finished proof-of-concept file. Call exactly once.",
    parameters={
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "Suggested filename, e.g. 'poc_upload_rce.py'."},
            "language": {"type": "string", "description": "python | bash | javascript | go | ruby | php | http | text"},
            "poc": {"type": "string", "description": "The complete PoC script. Target is a variable/argument, never hardcoded."},
            "usage": {"type": "string", "description": "One or two lines on how to run it and what to point it at."},
        },
        "required": ["poc"],
    },
)


def _run_poc_stage(db, run) -> str:
    import hashlib
    case = db.get(models.Case, run.case_id)
    finding = db.get(models.Finding, run.finding_id) if run.finding_id else None
    if finding is None:
        raise RuntimeError("a PoC needs a finding")
    provider = _provider(db, run)

    user = _finding_ctx(case, finding)
    prior = _prior(db, run, models.StageType.source)
    if prior:
        user += f"\n# Prior source investigation\n\n{prior}\n"
    user += "\nWrite the PoC and call submit_poc. Do not hardcode a target."

    args, text = _run_read_tool_loop(db, run, provider, _POC_SYSTEM, [_POC_TOOL], user, "submit_poc")
    if args is None or not (args.get("poc") or "").strip():
        raise RuntimeError("the model finished without submitting a PoC")

    code = args["poc"]
    lang = (args.get("language") or "text").strip().lower()
    ext, ctype = _POC_LANG.get(lang, (".txt", "text/plain"))
    fname = (args.get("filename") or "").strip() or f"poc_{finding.id[:8]}{ext}"
    if "." not in fname:
        fname += ext
    usage = (args.get("usage") or "").strip()

    raw = code.encode("utf-8")
    att = models.Attachment(
        user_id=case.user_id, agent_id=None, session_id=None,
        scan_id=case.scan_id, finding_id=finding.id,
        filename=fname, original_path=None, content_type=ctype,
        sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw),
        content_enc=crypto.encrypt(raw),
    )
    db.add(att)
    db.flush()
    run.artifact_id = att.id

    parts = [f"# Proof of concept — {finding.title or 'finding'}", ""]
    parts.append(f"**File:** `{fname}` ({lang}) — download it and run against your own target.")
    if usage:
        parts += ["", f"**Usage:** {usage}"]
    parts += ["", "```" + (lang if lang != "text" else ""), code.strip(), "```"]
    return "\n".join(parts).strip()


# --------------------------------------------------------------- dispatch

_RUNNERS = {
    models.StageType.source: _run_source_stage,
    models.StageType.impact: _run_impact_stage,
    models.StageType.remediation: _run_remediation_stage,
    models.StageType.report: _run_report_stage,
    models.StageType.poc: _run_poc_stage,
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
