"""Identify what a single uploaded source archive is, as a component of a
larger product. Used by the source-code upload path on the product page.

Returns {"name", "description", "role"} — grounded in the archive's files and
placed in the context of the product and the components already catalogued.
"""
from __future__ import annotations

import json
import logging

from .base import get_provider

log = logging.getLogger("irs.component_analyzer")

_SYSTEM = (
    "You identify software components within a larger product, for a security "
    "tracking dashboard. You are given the product, the components already "
    "catalogued on it, and ONE newly-uploaded source archive (its file tree "
    "plus excerpts of key files). Decide what this component is.\n\n"
    "Respond with RAW JSON ONLY — no markdown, no code fence:\n"
    '{"name": "...", "description": "...", "role": "..."}\n\n'
    "- name: a short, specific component name (e.g. 'auth-service', 'web-ui', "
    "'api-gateway'). Ground it in the code — package name, top dir, or build "
    "manifest — not a guess. Make it DISTINCT from the existing components.\n"
    "- description: 1-2 sentences on what it is and its tech stack.\n"
    "- role: how it fits into the product overall, relative to the other "
    "components.\n"
    "Be concise and factual. Do not invent features the files don't evidence."
)


def analyze_component(
    *,
    product_name: str,
    product_desc: str,
    existing: list[dict],
    archive_name: str,
    tree_summary: str,
    key_files: list[tuple[str, str]],
) -> dict:
    parts = [f"Product: {product_name or '(unnamed)'}"]
    if product_desc:
        parts.append(f"Product description: {product_desc}")
    if existing:
        parts.append("Components already on this product:")
        for c in existing:
            parts.append(f"  - {c['name']}: {(c.get('description') or '').strip()}")
    else:
        parts.append("Components already on this product: (none yet)")
    parts += ["", f"New source archive: {archive_name}", "", "File tree (summary):", tree_summary]
    if key_files:
        parts.append("")
        parts.append("Key file excerpts:")
        for path, content in key_files:
            parts.append(f"--- {path} ---")
            parts.append(content)
    user = "\n".join(parts)

    reply = get_provider().chat(_SYSTEM, [{"role": "user", "content": user}])
    return _parse(reply, archive_name)


def _parse(reply: str, fallback_name: str) -> dict:
    out = {"name": _fallback_name(fallback_name), "description": "", "role": "", "ai_rationale": reply[:2000]}
    s = (reply or "").strip()
    # Pull the first {...} block out, tolerating stray prose / code fences.
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(s[start:end + 1])
            for k in ("name", "description", "role"):
                if isinstance(data.get(k), str) and data[k].strip():
                    out[k] = data[k].strip()
        except Exception as e:
            log.warning("component_analyzer: could not parse JSON (%s); using fallback", e)
    return out


def _fallback_name(archive_name: str) -> str:
    base = archive_name.rsplit("/", 1)[-1]
    for ext in (".zip", ".tar.gz", ".tgz", ".tar"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    return base or "component"
