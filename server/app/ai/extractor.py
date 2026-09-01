"""Extract structured VulnScan + RunLog + Finding fields from a markdown report.

Big reports — say, 100 findings — won't fit in a single Claude response even
at max_tokens=16384, so this module chunks the input by markdown section
boundaries and aggregates results.

Contract:
- `extract(text)` returns a dict { "scan": {...}, "runs": [...], "findings": [...] }
  or None if the document doesn't look like a vuln report at all.
- Always best-effort: bad model output is logged and swallowed; callers
  treat the result as a draft for human review.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from .base import get_provider

log = logging.getLogger(__name__)

_VALID_SEVERITIES = {"critical", "high", "medium", "low", "info", "unknown"}
_VALID_STATUSES = {
    "open", "true_positive", "false_positive", "sbp", "duplicate", "fixed"
}

# A chunk above this size (chars) gets split further. Picked so each chunk's
# JSON output stays comfortably under our 16k max_tokens cap (Claude generates
# roughly the same number of output tokens as input chars for these reports).
_CHUNK_TARGET = 8000
# Don't bother chunking documents below this size — single call is faster.
_SINGLE_SHOT_BELOW = 9000
# Hard cap on total markdown we'll process (sanity guard).
_MAX_INPUT = 200_000

# ----- prompts -----

_SCHEMA_FINDING = """\
{
  "title": string,                 // 1-line summary (e.g. "Heap buffer overflow in SSH parser")
  "severity": "critical"|"high"|"medium"|"low"|"info"|"unknown",
  "status": "open"|"true_positive"|"false_positive"|"sbp"|"duplicate"|"fixed",
  "cwe": string,                   // "CWE-122" form, "" if not stated
  "cve": string,                   // "CVE-2025-…" form, "" if not stated
  "affected_component": string,    // file/function/version/endpoint
  "description": string,           // multi-paragraph allowed; what is the bug
  "steps_to_reproduce": string,    // numbered steps if present
  "remediation": string,           // suggested fix
  "proof_of_concept": string,      // commands, payloads, crashing input refs
  "references": string,            // URLs / advisories, one per line
  "assigned_to": string,
  "triaged_by": string
}"""

_SYSTEM_FIRST = f"""\
You extract structured vulnerability-scan data from a markdown report.

Return a SINGLE JSON object — no prose, no markdown fences. Schema:

{{
  "is_vuln_report": boolean,           // true if the document describes one or more
                                       // security vulnerabilities — INCLUDING when the
                                       // header is "security assessment", "pentest report",
                                       // "code review", "audit", "vuln scan", etc.
  "scan": {{
    "product": string,
    "scan_target": string,
    "harness_used": string,
    "scan_by": string,
    "results_file": string,
    "spreadsheet_link": string,
    "triaged_by": string,
    "findings": integer,               // total raised
    "tp": integer,
    "fp": integer,
    "sbp": integer,
    "duplicates": integer,
    "untriaged": integer,
    "highest_severity": "critical"|"high"|"medium"|"low"|"info"|"unknown",
    "notes": string
  }},
  "runs": [
    {{
      "day": string, "date": "YYYY-MM-DD"|"", "run": string, "box": string,
      "product": string, "harness": string, "prompt": string, "results": string,
      "poc": string, "comment": string, "complete": boolean
    }}
  ],
  "findings": [{_SCHEMA_FINDING}]
}}

Rules:
- If NOT a vulnerability report, return {{"is_vuln_report": false}}.
- Unknown counts default to 0. Unknown strings default to "".
- Never invent data.
- highest_severity MUST be one of the enum values above (lowercase).
- Output ONLY the JSON object. No explanation, no code fences.
"""

# Subsequent chunks: skip the heavyweight scan/runs envelope so output stays
# short. Only findings.
_SYSTEM_CONT = f"""\
You continue extracting vulnerability findings from a portion of a larger
markdown report. The scan summary has already been extracted from earlier
chunks — focus ONLY on individual findings in THIS chunk.

Return a SINGLE JSON object — no prose, no markdown fences:

{{
  "findings": [{_SCHEMA_FINDING}]
}}

Rules:
- Each finding mentioned in this chunk = one entry.
- Skip table-of-contents lines, summary tables that just list IDs, and any
  finding already described in full elsewhere — keep only ones with real
  text in this chunk.
- Never invent data. Unknown strings = "".
- Output ONLY the JSON object.
"""

_USER_TEMPLATE = "Extract from this markdown:\n\n```markdown\n{text}\n```"

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


# ----- public API -----

def extract(text: str, provider_name: str | None = None) -> Optional[dict]:
    """Return the parsed extraction dict, or None if nothing useful was extracted."""
    if not text or not text.strip():
        return None
    text = text[:_MAX_INPUT]

    try:
        provider = get_provider(provider_name)
    except Exception as e:
        log.warning("extractor: provider unavailable: %s", e)
        return None

    chunks = _split_into_chunks(text)
    if not chunks:
        return None

    log.info("extractor: %d chunk(s) (%d chars total)", len(chunks), len(text))

    # First chunk: full envelope (scan + runs + findings).
    first = _call(provider, _SYSTEM_FIRST, chunks[0])
    if not first:
        return None
    if not first.get("is_vuln_report") and not first.get("findings"):
        return None

    aggregated = _normalize_first(first)
    seen_titles = {_dedup_key(f) for f in aggregated["findings"]}

    # Subsequent chunks: just findings, merged + deduped.
    for i, chunk in enumerate(chunks[1:], start=2):
        cont = _call(provider, _SYSTEM_CONT, chunk)
        if not cont:
            log.info("extractor: chunk %d returned nothing; skipping", i)
            continue
        for raw_f in cont.get("findings") or []:
            if not isinstance(raw_f, dict):
                continue
            f = _normalize_finding(raw_f)
            if not f["title"]:
                continue
            k = _dedup_key(f)
            if k in seen_titles:
                continue
            seen_titles.add(k)
            aggregated["findings"].append(f)

    log.info("extractor: aggregated %d findings", len(aggregated["findings"]))
    return aggregated


# ----- chunking -----

# Match major (## ) and minor (### ) headings as candidate split points.
_H2_RE = re.compile(r"^(?=## [^#])", re.MULTILINE)
_H3_RE = re.compile(r"^(?=### [^#])", re.MULTILINE)


def _split_into_chunks(text: str) -> list[str]:
    """Split a markdown report into manageable chunks.

    Strategy:
    1. If the whole thing fits comfortably, one chunk.
    2. Otherwise try splitting at `## ` boundaries (severity tiers).
    3. If any of those chunks are still too big, sub-split at `### ` (per finding).
    4. Greedily merge consecutive small chunks back together up to _CHUNK_TARGET.
    """
    if len(text) <= _SINGLE_SHOT_BELOW:
        return [text]

    parts = _H2_RE.split(text)
    parts = [p for p in parts if p.strip()]
    if len(parts) <= 1:
        # No useful H2 structure; try H3.
        parts = _H3_RE.split(text)
        parts = [p for p in parts if p.strip()]
        if len(parts) <= 1:
            # Last resort: hard slice every _CHUNK_TARGET chars.
            return _hard_slice(text)

    # Sub-split any chunk that's still too big using H3.
    refined: list[str] = []
    for p in parts:
        if len(p) <= _CHUNK_TARGET:
            refined.append(p)
            continue
        sub = _H3_RE.split(p)
        sub = [s for s in sub if s.strip()]
        if len(sub) <= 1:
            refined.extend(_hard_slice(p))
        else:
            refined.extend(sub)

    return _greedy_merge(refined)


def _hard_slice(text: str) -> list[str]:
    return [text[i : i + _CHUNK_TARGET] for i in range(0, len(text), _CHUNK_TARGET)]


def _greedy_merge(chunks: list[str]) -> list[str]:
    """Combine consecutive chunks into groups up to _CHUNK_TARGET chars each."""
    out: list[str] = []
    buf = ""
    for c in chunks:
        if not buf:
            buf = c
            continue
        if len(buf) + len(c) + 1 <= _CHUNK_TARGET:
            buf = buf + "\n" + c
        else:
            out.append(buf)
            buf = c
    if buf:
        out.append(buf)
    return out


# ----- single model-call helper -----

# Extraction JSON for ~25 detailed findings fits comfortably in 16k. Pass it
# explicitly: only the Anthropic provider used to apply a default, so other
# providers silently truncated long extractions at their own server-side cap.
EXTRACT_MAX_TOKENS = 16384


def _call(provider, system: str, chunk: str) -> Optional[dict]:
    try:
        raw = provider.chat(
            system,
            [{"role": "user", "content": _USER_TEMPLATE.format(text=chunk)}],
            max_tokens=EXTRACT_MAX_TOKENS,
        )
    except Exception as e:
        log.warning("extractor: provider call failed: %s", e)
        return None
    parsed = _try_parse(raw)
    if not parsed:
        log.warning(
            "extractor: could not parse JSON from model output (%d chars). Tail: %r",
            len(raw or ""), (raw or "")[-160:],
        )
        return None
    return parsed


# ----- normalization & dedup -----

def _normalize_first(parsed: dict) -> dict:
    scan = parsed.get("scan") or {}
    return {
        "scan": {
            "product":          _s(scan.get("product")),
            "scan_target":      _s(scan.get("scan_target")),
            "harness_used":     _s(scan.get("harness_used")),
            "scan_by":          _s(scan.get("scan_by")),
            "results_file":     _s(scan.get("results_file")),
            "spreadsheet_link": _s(scan.get("spreadsheet_link")),
            "triaged_by":       _s(scan.get("triaged_by")),
            "findings":         _i(scan.get("findings")),
            "tp":               _i(scan.get("tp")),
            "fp":               _i(scan.get("fp")),
            "sbp":              _i(scan.get("sbp")),
            "duplicates":       _i(scan.get("duplicates")),
            "untriaged":        _i(scan.get("untriaged")),
            "highest_severity": _sev(scan.get("highest_severity")),
            "notes":            _s(scan.get("notes")),
        },
        "runs": [
            {
                "day":      _s(r.get("day")),
                "date":     _s(r.get("date")),
                "run":      _s(r.get("run")),
                "box":      _s(r.get("box")),
                "product":  _s(r.get("product")),
                "harness":  _s(r.get("harness")),
                "prompt":   _s(r.get("prompt")),
                "results":  _s(r.get("results")),
                "poc":      _s(r.get("poc")),
                "comment":  _s(r.get("comment")),
                "complete": bool(r.get("complete")),
            }
            for r in (parsed.get("runs") or [])
            if isinstance(r, dict)
        ],
        "findings": [
            _normalize_finding(f)
            for f in (parsed.get("findings") or [])
            if isinstance(f, dict) and _s(f.get("title"))
        ],
    }


def _normalize_finding(f: dict) -> dict:
    return {
        "title":              _s(f.get("title")),
        "severity":           _sev(f.get("severity")),
        "status":             _status(f.get("status")),
        "cwe":                _s(f.get("cwe")),
        "cve":                _s(f.get("cve")),
        "affected_component": _s(f.get("affected_component")),
        "description":        _s(f.get("description")),
        "steps_to_reproduce": _s(f.get("steps_to_reproduce")),
        "remediation":        _s(f.get("remediation")),
        "proof_of_concept":   _s(f.get("proof_of_concept")),
        "references":         _s(f.get("references")),
        "assigned_to":        _s(f.get("assigned_to")),
        "triaged_by":         _s(f.get("triaged_by")),
    }


def _dedup_key(f: dict) -> str:
    """Key for deduping the same finding appearing in overlapping chunks.
    Uses normalized title; severity included so similarly-titled distinct
    findings at different severities aren't collapsed."""
    return f"{_norm_title(f['title'])}|{f['severity']}"


_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def _norm_title(t: str) -> str:
    return _PUNCT_RE.sub(" ", t.lower()).strip()


# ----- response parsing -----

def _try_parse(raw: str) -> Optional[dict]:
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        v = json.loads(raw)
    except json.JSONDecodeError:
        m = _JSON_OBJECT_RE.search(raw)
        if not m:
            return None
        try:
            v = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return v if isinstance(v, dict) else None


# ----- coercion helpers -----

def _s(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _i(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _sev(v) -> str:
    s = _s(v).lower()
    return s if s in _VALID_SEVERITIES else "unknown"


def _status(v) -> str:
    s = _s(v).lower().replace("-", "_").replace(" ", "_")
    return s if s in _VALID_STATUSES else "open"


# ---------------------------------------------------------------------------
# Per-scan finding enrichment
#
# The first-pass extractor compresses each finding into a tight summary so
# the per-chunk JSON stays small. That leaves fields like proof_of_concept,
# steps_to_reproduce, and references empty even when the source report
# has plenty of detail. This pass reads the full source markdown again and
# fills the gaps for every finding in one Claude call.
# ---------------------------------------------------------------------------

_ENRICH_SYSTEM = """\
You're enriching security findings from their source report. For each
finding you receive, find its section in the report and pull EVERY
relevant detail into the schema fields. Be thorough — copy code blocks,
payloads, and command lines verbatim into proof_of_concept; keep file
paths and line numbers in references; extract any CWE/CVE references
from the text. Never invent details that aren't in the source.

Return a single JSON object (no markdown fences) of the form:

{
  "findings": [
    {
      "id": "<the id you were given>",
      "description":         "Multi-paragraph prose from the report.",
      "steps_to_reproduce":  "Numbered steps if present, OR the reachable code path.",
      "proof_of_concept":    "Verbatim code/payload/command blocks from the report.",
      "references":          "File paths (with line numbers), URLs, advisory IDs — one per line.",
      "cwe":                 "CWE-NNN if mentioned, else \\"\\"",
      "cve":                 "CVE-YYYY-NNNN if mentioned, else \\"\\"",
      "affected_component":  "file path / endpoint / module"
    }
  ]
}

Rules:
- Return exactly one object per input finding, preserving the given id.
- Empty string for unknowns, never null.
- description should be the FULL explanation as prose, not a one-liner.
- proof_of_concept should include the verbatim ```code blocks``` if any.
"""


def enrich_findings(
    source_markdown: str,
    findings: list[dict],
    provider_name: str | None = None,
) -> list[dict]:
    """Re-extract per-finding detail from a shared source report.

    Input: list of {"id": str, "title": str, "current_description": str}.
    Output: list of {"id": str, "description": str, "steps_to_reproduce": str,
            "proof_of_concept": str, "references": str, "cwe": str,
            "cve": str, "affected_component": str}, aligned by id.
    Missing ids are skipped silently.
    """
    if not findings:
        return []
    if not source_markdown or len(source_markdown) > _MAX_INPUT:
        log.warning("enrich_findings: source too small/large (%d chars)", len(source_markdown))
        return []

    payload = {
        "findings": [
            {
                "id": f.get("id", ""),
                "title": f.get("title", ""),
                "current_description": (f.get("current_description") or "")[:400],
            }
            for f in findings
        ],
    }

    user_msg = (
        "## Source report\n\n```markdown\n"
        + source_markdown
        + "\n```\n\n"
        + "## Findings to enrich (JSON)\n\n```json\n"
        + json.dumps(payload, indent=2)
        + "\n```\n\nReturn the enriched JSON object."
    )

    provider = get_provider(provider_name)
    raw = provider.chat(
        _ENRICH_SYSTEM,
        [{"role": "user", "content": user_msg}],
        max_tokens=EXTRACT_MAX_TOKENS,
    )
    parsed = _try_parse(raw)
    if not parsed or not isinstance(parsed.get("findings"), list):
        log.warning("enrich_findings: bad model output (%d chars)", len(raw or ""))
        return []

    out: list[dict] = []
    for entry in parsed["findings"]:
        if not isinstance(entry, dict):
            continue
        out.append({
            "id":                 _s(entry.get("id")),
            "description":        _s(entry.get("description")),
            "steps_to_reproduce": _s(entry.get("steps_to_reproduce")),
            "proof_of_concept":   _s(entry.get("proof_of_concept")),
            "references":         _s(entry.get("references")),
            "cwe":                _s(entry.get("cwe")),
            "cve":                _s(entry.get("cve")),
            "affected_component": _s(entry.get("affected_component")),
        })
    return out
