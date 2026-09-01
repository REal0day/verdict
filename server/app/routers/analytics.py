"""Cross-product Claude analytics.

Users can ask questions like "give me a spreadsheet of all findings with
TP/FP and SBP/SS/VULN tags across every product" and get a downloadable
CSV / JSON / markdown response back. The endpoint resolves the scope to
the set of products the *caller* can see, dumps the relevant findings +
scans + product info as a compact JSON context, and hands it to Claude
with the same output-rule prompt the chat endpoint uses.

  POST /analytics/chat
"""
from __future__ import annotations

import hashlib
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import crypto, models, schemas
from ..ai.base import get_provider
from ..auth import get_current_user
from ..database import get_db
from ..permissions import scope_scans

log = logging.getLogger("irs.analytics")
router = APIRouter(prefix="/analytics", tags=["analytics"])


_SYSTEM = (
    "You are an analyst inside an internal vulnerability-tracking dashboard. "
    "You receive a JSON dump of products, scans, and findings the caller "
    "can see, plus a free-text question. Answer it.\n\n"
    "Output rules (your reply may be saved as a file):\n"
    "- If the user asks for a CSV / spreadsheet / table-to-export, respond "
    "with RAW CSV ONLY (comma-separated, newline rows, header row, RFC4180 "
    "quoting). DO NOT wrap it in a markdown table or code fence.\n"
    "- If the user asks for JSON, respond with raw JSON only.\n"
    "- Otherwise respond with a complete, well-structured Markdown document.\n"
    "- Never preface a CSV/JSON response with 'Here is the spreadsheet:' "
    "— output the file contents directly so they save as-is.\n"
    "- Don't invent facts that aren't in the JSON dump; if something is "
    "missing, say so."
)


class AnalyticsRequest(BaseModel):
    prompt: str = Field(min_length=1)
    # null/empty -> all visible products
    product_ids: list[str] | None = None
    save_as_report: bool = False
    save_filename: str | None = None


class AnalyticsResponse(BaseModel):
    reply: str
    generated_report_id: str | None = None
    # Tiny status block so the SPA can show "scoped to N products / M findings".
    scope: dict


class MasterRow(BaseModel):
    id: str
    project_id: str | None
    source_report_id: str | None
    created_at: str
    state: str
    product: str
    scan_target: str
    harness_used: str
    scan_by: str
    results_file: str
    spreadsheet_link: str
    triaged_by: str
    findings: int
    fp: int
    sbp: int
    tp: int
    ss: int
    duplicates: int
    untriaged: int
    highest_severity: str


@router.get("/master", response_model=list[MasterRow])
def master_dashboard(
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """One row per scan across every product the viewer can see — the
    'master spreadsheet' upper management asked for. Data comes straight
    off VulnScan; product/scan_by fall back to the linked Project / owner
    email when the free-text field is blank."""
    q = db.query(models.VulnScan).order_by(models.VulnScan.created_at.desc())
    q = scope_scans(q, db, viewer)
    out: list[MasterRow] = []
    for s in q.all():
        product = (s.project.name if s.project else "") or s.product
        scan_by = s.scan_by or (s.user.email if s.user else "")
        results_file = s.results_file
        if not results_file and s.source_report_id:
            r = db.get(models.Report, s.source_report_id)
            if r:
                results_file = r.filename
        # SS isn't a stored aggregate on VulnScan (unlike fp/sbp/tp/etc.) —
        # it's a per-finding tag (SBP / SS / VULN, lowercased), so count it.
        ss = sum(1 for f in s.finding_rows if "ss" in (f.tags or []))
        out.append(MasterRow(
            id=s.id,
            project_id=s.project_id,
            source_report_id=s.source_report_id,
            created_at=s.created_at.isoformat(),
            state=s.state.value,
            product=product,
            scan_target=s.scan_target,
            harness_used=s.harness_used,
            scan_by=scan_by,
            results_file=results_file,
            spreadsheet_link=s.spreadsheet_link,
            triaged_by=s.triaged_by,
            findings=s.findings,
            fp=s.fp,
            sbp=s.sbp,
            tp=s.tp,
            ss=ss,
            duplicates=s.duplicates,
            untriaged=s.untriaged,
            highest_severity=s.highest_severity.value,
        ))
    return out


def _visible_projects(db: Session, viewer: models.User) -> list[models.Project]:
    if viewer.role == models.Role.admin:
        return db.query(models.Project).order_by(models.Project.name).all()
    return sorted(viewer.projects, key=lambda p: (p.name or "").lower())


def _scans_for_project(db: Session, project_id: str) -> list[models.VulnScan]:
    """Same dual-resolution as elsewhere: VulnScan.project_id OR Run.project_id."""
    direct = (
        db.query(models.VulnScan)
        .filter(models.VulnScan.project_id == project_id).all()
    )
    via_run = (
        db.query(models.VulnScan)
        .join(models.Run, models.VulnScan.source_session_id == models.Run.session_id)
        .filter(models.Run.project_id == project_id).all()
    )
    by_id: dict[str, models.VulnScan] = {s.id: s for s in direct}
    for s in via_run:
        by_id.setdefault(s.id, s)
    return list(by_id.values())


def _scan_rank_map(scans: list[models.VulnScan]) -> dict[str, int]:
    ordered = sorted(scans, key=lambda s: s.created_at)
    return {s.id: i + 1 for i, s in enumerate(ordered)}


def _dump_finding(f: models.Finding, scan_rank: int, scan_product: str) -> dict:
    return {
        "id": f.id,
        "title": f.title,
        "severity": f.severity.value,
        "dev_verdict": f.status.value,
        "ai_verdict": f.ai_verdict.value,
        "ai_rationale": (f.ai_rationale or "")[:400],
        "tags": list(f.tags or []),
        "cwe": f.cwe,
        "cve": f.cve,
        "affected_component": f.affected_component,
        "triaged_by": f.triaged_by,
        "triaged_at": f.triaged_at.isoformat() if f.triaged_at else None,
        "scan_rank": scan_rank,
        "scan_product": scan_product,
    }


def _dump_scope(db: Session, viewer: models.User, project_ids: list[str] | None) -> dict:
    """Build the JSON context Claude sees. Compact: drops descriptions /
    PoCs / steps_to_reproduce to keep the prompt small. Claude can still
    answer 'how many TP/FP/etc.' questions without those.
    """
    visible = _visible_projects(db, viewer)
    if project_ids:
        wanted = set(project_ids)
        visible = [p for p in visible if p.id in wanted]
    out_products = []
    out_scans = []
    out_findings = []
    for p in visible:
        out_products.append({
            "id": p.id,
            "name": p.name,
            "description": p.description or "",
            "members": len(p.members),
        })
        scans = _scans_for_project(db, p.id)
        ranks = _scan_rank_map(scans)
        for s in scans:
            out_scans.append({
                "id": s.id,
                "rank": ranks[s.id],
                "product_id": p.id,
                "product_name": p.name,
                "product": s.product or "",
                "scan_target": s.scan_target or "",
                "harness_used": s.harness_used or "",
                "scan_by": s.scan_by or "",
                "state": s.state.value,
                "findings_count": s.findings,
                "tp": s.tp, "fp": s.fp, "sbp": s.sbp,
                "untriaged": s.untriaged, "duplicates": s.duplicates,
                "highest_severity": s.highest_severity.value,
                "created_at": s.created_at.isoformat(),
                "confirmed_at": s.confirmed_at.isoformat() if s.confirmed_at else None,
            })
            for f in s.finding_rows:
                row = _dump_finding(f, ranks[s.id], p.name)
                row["scan_id"] = s.id
                out_findings.append(row)
    return {
        "products":  out_products,
        "scans":     out_scans,
        "findings":  out_findings,
    }


@router.post("/chat", response_model=AnalyticsResponse)
def analytics_chat(
    body: AnalyticsRequest,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    scope = _dump_scope(db, viewer, body.product_ids or None)
    if not scope["products"]:
        raise HTTPException(400, "No visible products in scope")

    # Soft cap: if findings > 5000, truncate. Most workspaces will be far
    # under this; an enormous tenant can still query subsets via product_ids.
    if len(scope["findings"]) > 5000:
        log.warning(
            "analytics: truncating findings %d -> 5000 (viewer=%s)",
            len(scope["findings"]), viewer.email,
        )
        scope["findings"] = scope["findings"][:5000]
        scope["_truncated"] = True

    user_msg = (
        "## Scope (JSON)\n\n```json\n"
        + json.dumps(scope, indent=2)
        + "\n```\n\n## Question\n\n"
        + body.prompt
    )

    provider = get_provider()
    reply = provider.chat(_SYSTEM, [{"role": "user", "content": user_msg}])

    generated_id: str | None = None
    if body.save_as_report:
        fname = (body.save_filename or "analytics.md").strip()
        if "." not in fname:
            fname = fname + ".md"
        raw = reply.encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        # Reports have a uq(user_id, sha256) constraint. If Claude produces
        # the same answer twice (common for deterministic-ish prompts) we
        # just reuse the existing report instead of 500ing.
        existing = (
            db.query(models.Report)
            .filter(models.Report.user_id == viewer.id, models.Report.sha256 == sha)
            .first()
        )
        if existing:
            generated_id = existing.id
        else:
            rpt = models.Report(
                user_id=viewer.id,
                agent_id=None,
                source_tool=models.SourceTool.generated,
                filename=fname,
                original_path=None,
                sha256=sha,
                size_bytes=len(raw),
                content_enc=crypto.encrypt(raw),
            )
            db.add(rpt)
            db.commit()
            db.refresh(rpt)
            generated_id = rpt.id

    return AnalyticsResponse(
        reply=reply,
        generated_report_id=generated_id,
        scope={
            "products":  len(scope["products"]),
            "scans":     len(scope["scans"]),
            "findings":  len(scope["findings"]),
            "truncated": bool(scope.get("_truncated")),
        },
    )
