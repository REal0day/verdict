"""Stage execution for the Investigations pipeline.

Stages run as **plain chat calls that return JSON** — not tool/function calling.
Local models (via Ollama, LM Studio, etc.) are far more reliable at "respond
with JSON" than at OpenAI-style tool-calling, and this sidesteps the question
of whether a given model/endpoint supports tools at all. The source tree is
inlined into the prompt as a size-bounded digest, so no read tools are needed.

Every model response is logged and, on a parse failure, stored on the run so
you can see exactly what the model said instead of a dead-end error.

Runs are drained by a single in-process thread pool (`pipeline_max_concurrent`)
— the single-executor, one-long-queue model. Scale by moving this to a
Celery/RQ worker pool; the `run_stage` boundary already isolates it.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import crypto, cvss, models
from .ai import scope
from .ai.base import get_provider
from .ai.errors import AIProviderError, AIProviderUnavailable
from .config import settings

log = logging.getLogger("irs.pipeline")

_pool = ThreadPoolExecutor(
    max_workers=max(1, settings.pipeline_max_concurrent),
    thread_name_prefix="stage",
)

# A single local model instance can't handle concurrent requests (it 500s), so
# serialize every call to a self-hosted endpoint. Hosted providers are fine
# concurrently, so they don't take the lock.
_local_lock = threading.Lock()


class StageModelError(RuntimeError):
    """The model answered, but we couldn't get usable structured output from it.

    Carries the model's raw response so it can be logged and shown for review.
    """

    def __init__(self, reason: str, raw: str = ""):
        self.reason = reason
        self.raw = raw or ""
        super().__init__(reason)


# Rough chars-per-token for sizing prompts to a model's context window. Code is
# token-dense, so this is deliberately low (safe) rather than the ~4 of prose.
_CHARS_PER_TOKEN = 3.3
# Signatures of an upstream "prompt too long for the context" rejection.
_CTX_ERROR_MARKERS = ("context length", "context window", "n_ctx", "n_keep",
                      "maximum context", "too long", "exceeds")


class StageInputTooBig(RuntimeError):
    """The prompt (mostly the source digest) doesn't fit the model's context.

    `endpoint_ctx` is the context the model server *actually* rejected on, when
    we can read it from the upstream error — which may be smaller than the
    context configured in Verdict (i.e. the model isn't loaded at that size).
    """

    def __init__(self, prompt_tokens: int, configured_ctx: int, endpoint_ctx: int | None = None):
        self.prompt_tokens = prompt_tokens
        self.configured_ctx = configured_ctx
        self.endpoint_ctx = endpoint_ctx
        super().__init__("source too big for the model's context window")

    @property
    def message(self) -> str:
        need = self.prompt_tokens
        if self.endpoint_ctx and self.endpoint_ctx < self.configured_ctx:
            return (
                f"The model server rejected the prompt: it needs ~{need:,} tokens "
                f"but the model is actually loaded with only {self.endpoint_ctx:,} "
                f"tokens of context — even though Verdict is set to "
                f"{self.configured_ctx:,}. The model isn't loaded at that size. In "
                f"LM Studio: EJECT the model, set Context Length to at least "
                f"{max(self.configured_ctx, need + 2048):,}, and RELOAD it (the "
                f"setting only applies on reload). Then re-run."
            )
        return (
            f"The source is about {need:,} tokens but this model's context window "
            f"is {self.configured_ctx:,}. Fixes: load the model with a larger "
            f"context (e.g. LM Studio → Context Length) and set it under "
            f"Settings → AI; attach a narrower source path; or use a bigger-"
            f"context model. (A guided large-repo mode is planned.)"
        )


def _est_tokens(text: str) -> int:
    return int(len(text or "") / _CHARS_PER_TOKEN)


def _is_context_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _CTX_ERROR_MARKERS)


def _parse_endpoint_ctx(msg: str) -> int | None:
    """Pull the real n_ctx / context length out of an upstream error, if present."""
    m = re.search(r"n_ctx[:\s=]+(\d+)", msg, re.I) or \
        re.search(r"context length[^\d]{0,20}(\d{3,})", msg, re.I) or \
        re.search(r"maximum context length is (\d+)", msg, re.I)
    return int(m.group(1)) if m else None


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
    base_r = base.resolve()
    target = (base_r / (rel or "").lstrip("/")).resolve()
    if target != base_r and base_r not in target.parents:
        raise ValueError(f"path escapes the source tree: {rel!r}")
    return target


def _source_root(db, case: models.Case) -> Path | None:
    """Resolved source dir, or None if the case has no usable local source."""
    src = case.source
    if not src or src.kind != models.CaseSourceKind.local_path or not src.workspace_key:
        return None
    root = Path(src.workspace_key)
    return root if root.is_dir() else None


# ------------------------------------------------------------- source digest

# Directories that are never worth inlining.
_SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", "vendor", "__pycache__",
    ".venv", "venv", ".tox", "coverage", ".mypy_cache", ".pytest_cache",
}
# Extensions we treat as code (inlined first / preferred).
_CODE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rb", ".php",
    ".java", ".c", ".h", ".cc", ".cpp", ".cs", ".rs", ".sh", ".pl", ".lua",
    ".ejs", ".html", ".sql", ".yaml", ".yml", ".json", ".toml", ".cfg", ".ini",
}
_MAX_FILE_INLINE = 40 * 1024   # per file


def _iter_source_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            yield Path(dirpath) / fn


def _source_digest(root: Path, budget: int, focus: str = "") -> str:
    """Inline the source tree into a size-bounded string for the prompt.

    Code files first; a file matching `focus` (an affected component) is put at
    the very front. Notes truncation so the model knows the view is partial.
    """
    files = list(_iter_source_files(root))

    def rank(p: Path) -> tuple:
        rel = str(p.relative_to(root))
        is_focus = bool(focus) and focus in rel
        is_code = p.suffix.lower() in _CODE_EXT
        return (not is_focus, not is_code, len(rel))

    files.sort(key=rank)

    tree = "\n".join(sorted(str(p.relative_to(root)) for p in files)[:400])
    parts = [f"## Source tree ({len(files)} files under the source root)", tree, ""]
    used = len(tree)
    truncated_files = 0
    for p in files:
        if used >= budget:
            truncated_files += 1
            continue
        rel = str(p.relative_to(root))
        try:
            data = p.read_bytes()[:_MAX_FILE_INLINE]
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        chunk = f"\n### {rel}\n```\n{text}\n```\n"
        if used + len(chunk) > budget:
            truncated_files += 1
            continue
        parts.append(chunk)
        used += len(chunk)
    if truncated_files:
        parts.append(f"\n_(+{truncated_files} more files not shown — digest truncated at {budget} chars.)_")
    return "\n".join(parts)


# ---------------------------------------------------------------- json asking

def _extract_json(text: str):
    """Pull a JSON value out of a model response. Returns the object or None."""
    t = (text or "").strip()
    # Reasoning models sometimes inline their chain-of-thought in <think>…</think>.
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S | re.I).strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except (json.JSONDecodeError, ValueError):
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = t.find(open_c), t.rfind(close_c)
        if 0 <= i < j:
            try:
                return json.loads(t[i:j + 1])
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def _fit_output(provider, system: str, user: str, want_output: int) -> int:
    """Cap output tokens to what's left in the context, and refuse a prompt that
    can't fit at all (StageInputTooBig) — rather than let the endpoint 400."""
    ctx = int(getattr(provider, "context_window", 8192) or 8192)
    prompt_tokens = _est_tokens(system) + _est_tokens(user)
    avail = ctx - prompt_tokens - 512   # everything left over is fair game for output
    if avail < 256:
        raise StageInputTooBig(prompt_tokens, ctx)
    # A reasoning model burns much of the budget thinking before it answers, so
    # give it real room — cap at the request, but never starve it below ~4k.
    return max(min(want_output, avail), min(4096, avail))


def _chat(provider, system: str, user: str, want_output: int) -> str:
    out = _fit_output(provider, system, user, want_output)
    import contextlib
    serialize = getattr(provider, "name", "") == "local"
    gate = _local_lock if serialize else contextlib.nullcontext()
    try:
        with gate:
            raw = provider.chat(system, [{"role": "user", "content": user}], max_tokens=out) or ""
    except AIProviderUnavailable as e:
        if _is_context_error(e):
            raise StageInputTooBig(
                _est_tokens(system) + _est_tokens(user),
                int(getattr(provider, "context_window", 8192) or 8192),
                endpoint_ctx=_parse_endpoint_ctx(str(e)),
            )
        raise
    log.info(
        "stage model response via %s (%d chars): %s%s",
        getattr(provider, "display_name", getattr(provider, "name", "?")),
        len(raw), raw[:2000], "…(truncated in log)" if len(raw) > 2000 else "",
    )
    return raw


def _ask_json(provider, system: str, user: str, max_tokens: int = 8192):
    """Chat (context-aware), log the raw response, and extract JSON."""
    raw = _chat(provider, system, user, max_tokens)
    return _extract_json(raw), raw


_JSON_RULES = (
    "\n\nRespond with ONLY a single JSON value and nothing else — no prose "
    "before or after, no markdown fences. If you have nothing to report, return "
    "the empty shape (e.g. an empty array)."
)


# ---------------------------------------------------------------- shared ctx

def _provider(db, run):
    choice = scope.for_stage(db, run.id)
    return get_provider(choice.provider, choice.model)


def _report_text(case) -> str:
    return crypto.decrypt(case.report_enc).decode("utf-8", "replace") if case.report_enc else ""


def _finding_ctx(case, finding) -> str:
    return (
        f"# Reported finding\n\nTitle: {finding.title or '(none)'}\n"
        f"Severity: {finding.severity.value}\nCWE: {finding.cwe or '(none)'}\n"
        f"Description:\n{finding.description or '(none)'}\n\n"
        f"# Inbound bug report\n\n{_report_text(case) or '(none)'}\n"
    )


def _prior(db, run, stage) -> str:
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


def _digest_for(db, run, budget: int, focus: str = "") -> str:
    case = db.get(models.Case, run.case_id)
    root = _source_root(db, case)
    if root is None:
        return "(no source attached)"
    return _source_digest(root, budget, focus)


# --------------------------------------------------------------- discover stage

_DISCOVER_SYSTEM = (
    "You are a senior application-security engineer triaging a case. You are "
    "given an inbound bug report (which may describe issues, or may just be "
    "context) and a digest of the product's source code. Identify the real, "
    "concrete vulnerabilities — every issue the report describes AND any you "
    "find by reading the source. Ground each in the code; do not invent issues "
    "the code does not support.\n\n"
    'Return a JSON object of this shape:\n'
    '{"findings": [{"title": "...", "severity": "critical|high|medium|low|info", '
    '"cwe": "CWE-000", "affected_component": "path/file", '
    '"description": "what it is and why it is exploitable, citing file:line"}]}'
    + _JSON_RULES
)

_MAX_DISCOVERED = 50


def _run_discover_stage(db, run) -> str:
    case = db.get(models.Case, run.case_id)
    if case.scan_id is None:
        raise RuntimeError("case has no scan to attach findings to")
    provider = _provider(db, run)

    user = (
        f"# Inbound bug report / notes\n\n{_report_text(case) or '(none provided)'}\n\n"
        f"{_digest_for(db, run, budget=140_000)}\n\n"
        "Identify the vulnerabilities and return the JSON object."
    )
    parsed, raw = _ask_json(provider, _DISCOVER_SYSTEM, user, max_tokens=8192)
    if parsed is None:
        raise StageModelError("The model did not return valid JSON.", raw)

    items = parsed.get("findings") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        raise StageModelError("The model's JSON had no 'findings' list.", raw)

    created, rows = 0, []
    for it in items[:_MAX_DISCOVERED]:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or "").strip()
        if not title:
            continue
        try:
            sev = models.Severity((it.get("severity") or "unknown").strip().lower())
        except ValueError:
            sev = models.Severity.unknown
        db.add(models.Finding(
            scan_id=case.scan_id, user_id=case.user_id, title=title[:512], severity=sev,
            cwe=(it.get("cwe") or "").strip()[:64],
            affected_component=(it.get("affected_component") or "").strip()[:512],
            description=(it.get("description") or "").strip(),
        ))
        created += 1
        rows.append((title, sev.value, it.get("cwe") or "-", it.get("affected_component") or "-"))

    parts = [f"# Discovery — {created} finding(s)", ""]
    if created:
        parts += ["| # | Title | Severity | CWE | Component |", "|---|---|---|---|---|"]
        for i, (t, sv, cwe, comp) in enumerate(rows, 1):
            parts.append(f"| {i} | {t} | {sv} | {cwe} | {comp} |")
    else:
        parts.append("No vulnerabilities identified from the report and source.")
    return "\n".join(parts).strip()


# ------------------------------------------------------------- source stage

_SOURCE_SYSTEM = (
    "You are a senior application-security engineer investigating one reported "
    "vulnerability against the product's source. Locate the vulnerable code, "
    "explain how it manifests, and identify the exact file/component and lines.\n\n"
    'Return JSON: {"affected_component": "path/file", '
    '"summary": "markdown: what the code does, why it is vulnerable, citing file:line", '
    '"key_locations": ["file:line", ...], "confidence": "low|medium|high"}'
    + _JSON_RULES
)


def _run_source_stage(db, run) -> str:
    case = db.get(models.Case, run.case_id)
    finding = db.get(models.Finding, run.finding_id) if run.finding_id else None
    if finding is None:
        raise RuntimeError("source investigation needs a finding")
    provider = _provider(db, run)

    user = (
        _finding_ctx(case, finding)
        + f"\n{_digest_for(db, run, budget=140_000, focus=finding.affected_component)}\n\n"
        "Investigate this finding and return the JSON object."
    )
    parsed, raw = _ask_json(provider, _SOURCE_SYSTEM, user)
    if not isinstance(parsed, dict):
        raise StageModelError("The model did not return a JSON object.", raw)

    ac = (parsed.get("affected_component") or "").strip()
    if ac and not finding.affected_component:
        finding.affected_component = ac[:512]

    parts = [f"# Source investigation — {finding.title or 'finding'}", ""]
    if ac:
        parts.append(f"**Affected component:** {ac}")
    if parsed.get("confidence"):
        parts.append(f"**Confidence:** {parsed['confidence']}")
    parts += ["", (parsed.get("summary") or "").strip()]
    locs = parsed.get("key_locations") or []
    if locs:
        parts += ["", "**Key locations:**", *[f"- `{l}`" for l in locs]]
    return "\n".join(parts).strip()


# ------------------------------------------------------------- impact stage

_IMPACT_SYSTEM = (
    "You are a senior application-security engineer scoring a confirmed "
    "vulnerability. Provide a CVSS 3.1 base vector, a CVSS 4.0 base vector, a "
    "CWE, and one short sentence of justification for each metric in each "
    "vector. Do not inflate severity.\n\n"
    'Return JSON: {"cvss31_vector": "CVSS:3.1/AV:.../...", '
    '"cvss40_vector": "CVSS:4.0/AV:.../...", "cvss40_score": 0.0, "cwe": "CWE-000", '
    '"cvss31_rationale": [{"metric": "AV", "value": "N", "reason": "one sentence"}], '
    '"cvss40_rationale": [{"metric": "AV", "value": "N", "reason": "one sentence"}], '
    '"summary": "one-paragraph impact narrative"}'
    + _JSON_RULES
)


def _fmt_rationale(rows) -> str:
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        out.append(f"- **{r.get('metric','?')}:{r.get('value','?')}** — {(r.get('reason') or '').strip()}")
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
    else:
        user += f"\n{_digest_for(db, run, budget=100_000, focus=finding.affected_component)}\n"
    user += "\nScore this finding and return the JSON object."

    parsed, raw = _ask_json(provider, _IMPACT_SYSTEM, user, max_tokens=16384)
    if not isinstance(parsed, dict):
        raise StageModelError("The model did not return a JSON object.", raw)

    v31 = (parsed.get("cvss31_vector") or "").strip()
    score31 = sev31 = None
    v31_err = ""
    try:
        score31, sev31, _ = cvss.cvss31_base(v31)
        finding.cvss31_vector = v31[:128]
        finding.cvss31_score = score31
        finding.severity = models.Severity(sev31 if sev31 != "none" else "info")
    except cvss.CVSSError as e:
        v31_err = str(e)

    v40 = (parsed.get("cvss40_vector") or "").strip()
    v40_ok = False
    if v40:
        try:
            cvss.validate_cvss40(v40)
            v40_ok = True
            finding.cvss40_vector = v40[:160]
            finding.cvss40_score = parsed.get("cvss40_score")
        except cvss.CVSSError:
            v40_ok = False

    cwe = (parsed.get("cwe") or "").strip()
    if cwe and not finding.cwe:
        finding.cwe = cwe[:64]

    parts = [f"# Impact — {finding.title or 'finding'}", ""]
    if score31 is not None:
        parts.append(f"**CVSS 3.1:** {score31} ({sev31}) — computed · `{v31}`")
    elif v31:
        parts.append(f"**CVSS 3.1:** `{v31}` — could not compute ({v31_err})")
    if v40:
        parts.append(f"**CVSS 4.0:** {parsed.get('cvss40_score', '?')} "
                     f"({'model-estimated' if v40_ok else 'unvalidated'}) — `{v40}`")
    if cwe:
        parts.append(f"**CWE:** {cwe}")
    if parsed.get("summary"):
        parts += ["", parsed["summary"].strip()]
    r31 = _fmt_rationale(parsed.get("cvss31_rationale"))
    if r31:
        parts += ["", "**CVSS 3.1 rationale:**", r31]
    r40 = _fmt_rationale(parsed.get("cvss40_rationale"))
    if r40:
        parts += ["", "**CVSS 4.0 rationale:**", r40]
    return "\n".join(parts).strip()


# --------------------------------------------------------- remediation stage

_REMEDIATION_SYSTEM = (
    "You are a senior application-security engineer proposing a remediation for "
    "a confirmed vulnerability. Give a concrete, minimal fix: what to change and "
    "where (cite file:line), why it closes the issue, and any residual risk. "
    "Prefer a specific patch over generic advice.\n\n"
    'Return JSON: {"summary": "markdown: the concrete fix, citing file:line", '
    '"patch": "optional unified-diff or code snippet", "references": ["url", ...]}'
    + _JSON_RULES
)


def _run_remediation_stage(db, run) -> str:
    case = db.get(models.Case, run.case_id)
    finding = db.get(models.Finding, run.finding_id) if run.finding_id else None
    if finding is None:
        raise RuntimeError("remediation needs a finding")
    provider = _provider(db, run)

    prior = _prior(db, run, models.StageType.source)
    user = _finding_ctx(case, finding)
    if prior:
        user += f"\n# Prior source investigation\n\n{prior}\n"
    else:
        user += f"\n{_digest_for(db, run, budget=100_000, focus=finding.affected_component)}\n"
    user += "\nPropose the fix and return the JSON object."

    parsed, raw = _ask_json(provider, _REMEDIATION_SYSTEM, user)
    if not isinstance(parsed, dict):
        raise StageModelError("The model did not return a JSON object.", raw)

    summary = (parsed.get("summary") or "").strip()
    if not summary:
        raise StageModelError("The model returned no remediation summary.", raw)
    if not finding.remediation:
        finding.remediation = summary[:8000]

    parts = [f"# Remediation — {finding.title or 'finding'}", "", summary]
    if parsed.get("patch"):
        parts += ["", "```", str(parsed["patch"]).strip(), "```"]
    refs = parsed.get("references") or []
    if refs:
        parts += ["", "**References:**", *[f"- {r}" for r in refs]]
    return "\n".join(parts).strip()


# ----------------------------------------------------------------- poc stage

_POC_SYSTEM = (
    "You are a senior application-security engineer writing a proof-of-concept "
    "for a confirmed vulnerability. Produce a single self-contained script an "
    "analyst can run against a target of their choosing. The PoC must NOT "
    "hardcode a target — take the target host/URL as an argument or a clearly "
    "marked variable at the top, with a short usage comment. Do not run "
    "anything; just produce the file.\n\n"
    'Return JSON: {"filename": "poc_x.py", "language": "python|bash|javascript|'
    'go|ruby|php|http|text", "poc": "the complete script", '
    '"usage": "how to run it and what to point it at"}'
    + _JSON_RULES
)

_POC_LANG = {
    "python": (".py", "text/x-python"), "bash": (".sh", "text/x-shellscript"),
    "sh": (".sh", "text/x-shellscript"), "javascript": (".js", "text/javascript"),
    "typescript": (".ts", "text/plain"), "go": (".go", "text/plain"),
    "ruby": (".rb", "text/x-ruby"), "php": (".php", "text/x-php"),
    "http": (".http", "text/plain"), "text": (".txt", "text/plain"),
}


def _run_poc_stage(db, run) -> str:
    import hashlib

    case = db.get(models.Case, run.case_id)
    finding = db.get(models.Finding, run.finding_id) if run.finding_id else None
    if finding is None:
        raise RuntimeError("a PoC needs a finding")
    provider = _provider(db, run)

    prior = _prior(db, run, models.StageType.source)
    user = _finding_ctx(case, finding)
    if prior:
        user += f"\n# Prior source investigation\n\n{prior}\n"
    else:
        user += f"\n{_digest_for(db, run, budget=100_000, focus=finding.affected_component)}\n"
    user += "\nWrite the PoC (target NOT hardcoded) and return the JSON object."

    parsed, raw = _ask_json(provider, _POC_SYSTEM, user)
    if not isinstance(parsed, dict) or not (parsed.get("poc") or "").strip():
        raise StageModelError("The model did not return a PoC script.", raw)

    code = parsed["poc"]
    lang = (parsed.get("language") or "text").strip().lower()
    ext, ctype = _POC_LANG.get(lang, (".txt", "text/plain"))
    fname = (parsed.get("filename") or "").strip() or f"poc_{finding.id[:8]}{ext}"
    if "." not in fname:
        fname += ext
    usage = (parsed.get("usage") or "").strip()

    rawb = code.encode("utf-8")
    att = models.Attachment(
        user_id=case.user_id, agent_id=None, session_id=None,
        scan_id=case.scan_id, finding_id=finding.id,
        filename=fname, original_path=None, content_type=ctype,
        sha256=hashlib.sha256(rawb).hexdigest(), size_bytes=len(rawb),
        content_enc=crypto.encrypt(rawb),
    )
    db.add(att)
    db.flush()
    run.artifact_id = att.id

    parts = [f"# Proof of concept — {finding.title or 'finding'}", "",
             f"**File:** `{fname}` ({lang}) — download it and run against your own target."]
    if usage:
        parts += ["", f"**Usage:** {usage}"]
    parts += ["", "```" + (lang if lang != "text" else ""), code.strip(), "```"]
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
    report = _report_text(case)

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

    text = _chat(provider, _REPORT_SYSTEM, context, want_output=16384).strip()
    log.info("report stage produced %d chars", len(text))
    if not text:
        raise StageModelError("The model produced an empty report.", "")

    raw = text.encode("utf-8")
    fname = f"investigation-{case.id[:8]}.md"
    db.add(models.Report(
        user_id=case.user_id, agent_id=None,
        source_tool=models.SourceTool.generated,
        filename=fname, original_path=None,
        sha256=__import__("hashlib").sha256(raw).hexdigest(),
        size_bytes=len(raw), content_enc=crypto.encrypt(raw),
        project_id=case.project_id,
    ))
    return text


def _autopilot_after_discover(db, case) -> None:
    """Queue impact scoring for each finding that doesn't have one yet.

    Each impact run is its own model call (its own context), drained by the
    executor over time — so a big discovery becomes a queue, not one huge prompt.
    """
    if not case or not case.autopilot or not case.scan_id:
        return
    scored = {
        r.finding_id for r in db.query(models.StageRun).filter(
            models.StageRun.case_id == case.id,
            models.StageRun.stage == models.StageType.impact,
        )
    }
    new_ids = []
    for f in (case.scan.finding_rows if case.scan else []):
        if f.id in scored:
            continue
        r = models.StageRun(case_id=case.id, finding_id=f.id, stage=models.StageType.impact)
        db.add(r)
        db.flush()
        new_ids.append(r.id)
    if new_ids:
        db.commit()
        log.info("autopilot: queued impact for %d finding(s) on case %s", len(new_ids), case.id)
        for rid in new_ids:
            submit(rid)


# --------------------------------------------------------------- dispatch

_RUNNERS = {
    models.StageType.discover: _run_discover_stage,
    models.StageType.source: _run_source_stage,
    models.StageType.impact: _run_impact_stage,
    models.StageType.remediation: _run_remediation_stage,
    models.StageType.poc: _run_poc_stage,
    models.StageType.report: _run_report_stage,
}


def run_stage(db, run: models.StageRun) -> None:
    """Execute one stage run synchronously, updating its lifecycle + output.

    On a model/parse failure the model's raw response is preserved as the run's
    output (viewable in the UI) and logged, so an error is never a dead end.
    """
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
    except StageInputTooBig as e:
        run.status = models.StageRunStatus.error
        run.error = e.message[:2000]
        log.warning("stage %s (%s): %s", run.id, run.stage.value, e.message)
    except StageModelError as e:
        run.status = models.StageRunStatus.error
        run.error = e.reason[:2000]
        if e.raw:
            # Keep the model's actual words so the operator can see what happened.
            body = "## The model's response (couldn't be used)\n\n" + e.raw
            run.output_enc = crypto.encrypt(body.encode("utf-8"))
        log.warning("stage %s (%s) model error: %s | raw=%r",
                    run.id, run.stage.value, e.reason, e.raw[:500])
    except AIProviderError as e:
        run.status = models.StageRunStatus.error
        run.error = e.detail(is_admin=False)[:2000]
    except Exception as e:
        log.exception("stage %s (%s) failed", run.id, run.stage.value)
        run.status = models.StageRunStatus.error
        run.error = f"{type(e).__name__}: {e}"[:2000]
    run.completed_at = dt.datetime.now(dt.timezone.utc)
    db.commit()

    # Autopilot: after a successful discovery, kick off impact on each finding.
    if run.stage == models.StageType.discover and run.status == models.StageRunStatus.done:
        try:
            _autopilot_after_discover(db, db.get(models.Case, run.case_id))
        except Exception:
            log.exception("autopilot after discover failed for case %s", run.case_id)
