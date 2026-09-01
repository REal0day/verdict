"""Structured vulnerability scan data: VulnScan (Type A) + RunLog (Type B).

Two parallel surfaces, same RBAC:
  - /scans/...       Bearer-auth JSON API (clients, future agent ingest)
  - /ui/scans/...    cookie-auth HTML, used by the browser

The UI mirrors call into the same helpers so logic doesn't drift.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..config import settings
from ..database import get_db
from ..permissions import (
    assert_can_edit_scan,
    assert_can_view_scan,
    can_delete,
    scope_scans,
)

log = logging.getLogger("irs.scans")

api = APIRouter(prefix="/scans", tags=["scans"])
ui = APIRouter(prefix="/ui/scans", tags=["scans-ui"])
templates = Jinja2Templates(directory="app/templates")


# ---------------- helpers ----------------

def _user_from_cookie(request: Request, db: Session) -> Optional[models.User]:
    tok = request.cookies.get("irs_token")
    if not tok:
        return None
    from jose import jwt, JWTError
    try:
        payload = jwt.decode(tok, settings.secret_key, algorithms=["HS256"])
    except JWTError:
        return None
    return db.get(models.User, payload.get("sub"))


def _require_cookie_user(request: Request, db: Session) -> models.User:
    u = _user_from_cookie(request, db)
    if not u:
        raise HTTPException(401, "Not logged in")
    return u


def _scan_to_out(s: models.VulnScan) -> schemas.VulnScanOut:
    out = schemas.VulnScanOut.model_validate(s)
    # `user` is lazy-loaded via the relationship — populate it so the UI can
    # pre-fill the scan_by field with the owner's email.
    out.owner_email = s.user.email if s.user else None
    # Resolve the confirmer's email separately so the banner can name them
    # without a follow-up /users lookup.
    if s.confirmed_by:
        from sqlalchemy.orm import object_session
        sess = object_session(s)
        if sess is not None:
            u = sess.get(models.User, s.confirmed_by)
            if u:
                out.confirmed_by_email = u.email
    return out


def _run_to_out(r: models.RunLog) -> schemas.RunLogOut:
    return schemas.RunLogOut.model_validate(r)


def _finding_to_out(f: models.Finding) -> schemas.FindingOut:
    return schemas.FindingOut.model_validate(f)


def _apply_updates(obj, payload: dict) -> None:
    """Apply only fields that were explicitly set in the request."""
    for k, v in payload.items():
        if v is not None:
            setattr(obj, k, v)


# ---------------- API (JSON, Bearer) ----------------

@api.get("", response_model=list[schemas.VulnScanOut])
def list_scans(
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
    state: Optional[models.ScanState] = None,
    product: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    q = db.query(models.VulnScan).order_by(models.VulnScan.created_at.desc())
    q = scope_scans(q, db, viewer)
    if state:
        q = q.filter(models.VulnScan.state == state)
    if product:
        q = q.filter(models.VulnScan.product.ilike(f"%{product}%"))
    if project_id:
        run_sessions = (
            db.query(models.Run.session_id)
            .filter(models.Run.project_id == project_id)
            .subquery()
        )
        q = q.filter(
            (models.VulnScan.project_id == project_id)
            | (models.VulnScan.source_session_id.in_(run_sessions))
        )
    rows = q.offset(offset).limit(min(limit, 500)).all()
    return [_scan_to_out(s) for s in rows]


@api.post("", response_model=schemas.VulnScanOut, status_code=201)
def create_scan(
    body: schemas.VulnScanCreate,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    s = models.VulnScan(user_id=viewer.id, **body.model_dump())
    db.add(s)
    db.commit()
    db.refresh(s)
    return _scan_to_out(s)


@api.get("/{scan_id}", response_model=schemas.VulnScanDetail)
def get_scan(
    scan_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    s = db.get(models.VulnScan, scan_id)
    if not s:
        raise HTTPException(404, "Not found")
    assert_can_view_scan(db, viewer, s)
    return schemas.VulnScanDetail(
        **_scan_to_out(s).model_dump(),
        runs=[_run_to_out(r) for r in s.runs],
        findings_list=[_finding_to_out(f) for f in s.finding_rows],
    )


@api.patch("/{scan_id}", response_model=schemas.VulnScanOut)
def update_scan(
    scan_id: str,
    body: schemas.VulnScanUpdate,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    s = db.get(models.VulnScan, scan_id)
    if not s:
        raise HTTPException(404, "Not found")
    assert_can_edit_scan(db, viewer, s)
    payload = body.model_dump(exclude_unset=True)
    was_draft = s.state == models.ScanState.draft
    _apply_updates(s, payload)
    # Keep the underlying Run row's project in sync so visibility scope
    # (which still reads Run.project_id for session-scoped reports/scans)
    # stays correct now that the Runs UI is gone.
    if "project_id" in payload and s.source_session_id:
        run_row = db.get(models.Run, s.source_session_id)
        if run_row is not None:
            run_row.project_id = s.project_id
    # Editing a draft scan implies the user reviewed it — auto-confirm.
    # Direct state writes also stamp.
    if was_draft and s.state == models.ScanState.confirmed and not s.confirmed_at:
        s.confirmed_by = viewer.id
        s.confirmed_at = dt.datetime.now(dt.timezone.utc)
    elif was_draft and any(
        k in payload for k in
        ("product", "scan_target", "harness_used", "scan_by", "notes")
    ):
        # User corrected a field on a draft — interpret as "I've reviewed
        # this", flip to confirmed and stamp.
        s.state = models.ScanState.confirmed
        if not s.confirmed_at:
            s.confirmed_by = viewer.id
            s.confirmed_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(s)
    return _scan_to_out(s)


@api.post("/{scan_id}/agree", response_model=schemas.VulnScanOut)
def api_agree_with_scan(
    scan_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """One-click 'Agree with Claude's draft'. Anyone with edit access to
    the scan (admin / scan owner / project member) can confirm; we stamp
    the audit fields and flip state. Idempotent — re-clicking is a no-op
    that keeps the original confirmer's name."""
    s = db.get(models.VulnScan, scan_id)
    if not s:
        raise HTTPException(404, "Not found")
    assert_can_edit_scan(db, viewer, s)
    if s.state != models.ScanState.confirmed:
        s.state = models.ScanState.confirmed
    if not s.confirmed_at:
        s.confirmed_by = viewer.id
        s.confirmed_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(s)
    return _scan_to_out(s)


@api.delete("/{scan_id}", status_code=204)
def delete_scan(
    scan_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    s = db.get(models.VulnScan, scan_id)
    if not s:
        return
    if not can_delete(viewer, s.user_id):
        # Project members keep scan-delete rights even without manager role.
        assert_can_edit_scan(db, viewer, s)
    db.delete(s)
    db.commit()


# nested runs
@api.post("/{scan_id}/runs", response_model=schemas.RunLogOut, status_code=201)
def add_run(
    scan_id: str,
    body: schemas.RunLogCreate,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    s = db.get(models.VulnScan, scan_id)
    if not s:
        raise HTTPException(404, "Not found")
    assert_can_edit_scan(db, viewer, s)
    r = models.RunLog(scan_id=s.id, user_id=viewer.id, **body.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    return _run_to_out(r)


@api.patch("/{scan_id}/runs/{run_id}", response_model=schemas.RunLogOut)
def update_run(
    scan_id: str,
    run_id: str,
    body: schemas.RunLogUpdate,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    r = db.get(models.RunLog, run_id)
    if not r or r.scan_id != scan_id:
        raise HTTPException(404, "Not found")
    assert_can_edit_scan(db, viewer, r.scan)
    _apply_updates(r, body.model_dump(exclude_unset=True))
    db.commit()
    db.refresh(r)
    return _run_to_out(r)


@api.delete("/{scan_id}/runs/{run_id}", status_code=204)
def delete_run(
    scan_id: str,
    run_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    r = db.get(models.RunLog, run_id)
    if not r or r.scan_id != scan_id:
        return
    assert_can_edit_scan(db, viewer, r.scan)
    db.delete(r)
    db.commit()


# ---------------- UI (HTML, cookie) ----------------

@ui.get("", response_class=HTMLResponse)
def ui_list(
    request: Request,
    db: Session = Depends(get_db),
    state: Optional[models.ScanState] = None,
    product: Optional[str] = None,
):
    viewer = _require_cookie_user(request, db)
    q = db.query(models.VulnScan).order_by(models.VulnScan.created_at.desc())
    q = scope_scans(q, db, viewer)
    if state:
        q = q.filter(models.VulnScan.state == state)
    if product:
        q = q.filter(models.VulnScan.product.ilike(f"%{product}%"))
    scans = q.limit(200).all()
    return templates.TemplateResponse(
        request,
        "scans_list.html",
        {
            "user": viewer,
            "scans": scans,
            "filter_state": state.value if state else "",
            "filter_product": product or "",
            "severities": list(models.Severity),
            "states": list(models.ScanState),
        },
    )


@ui.get("/new", response_class=HTMLResponse)
def ui_new_form(request: Request, db: Session = Depends(get_db)):
    viewer = _require_cookie_user(request, db)
    return templates.TemplateResponse(
        request,
        "scan_new.html",
        {
            "user": viewer,
            "severities": list(models.Severity),
        },
    )


@ui.post("/new")
async def ui_new_submit(request: Request, db: Session = Depends(get_db)):
    viewer = _require_cookie_user(request, db)
    form = await request.form()
    s = models.VulnScan(
        user_id=viewer.id,
        state=models.ScanState.confirmed,
        product=form.get("product", "").strip(),
        scan_target=form.get("scan_target", "").strip(),
        harness_used=form.get("harness_used", "").strip(),
        scan_by=form.get("scan_by", "").strip() or viewer.email,
        results_file=form.get("results_file", "").strip(),
        spreadsheet_link=form.get("spreadsheet_link", "").strip(),
        triaged_by=form.get("triaged_by", "").strip(),
        findings=int(form.get("findings") or 0),
        fp=int(form.get("fp") or 0),
        sbp=int(form.get("sbp") or 0),
        tp=int(form.get("tp") or 0),
        duplicates=int(form.get("duplicates") or 0),
        untriaged=int(form.get("untriaged") or 0),
        highest_severity=models.Severity(form.get("highest_severity", "unknown")),
        notes=form.get("notes", ""),
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return RedirectResponse(f"/ui/scans/{s.id}", status_code=303)


@ui.get("/{scan_id}", response_class=HTMLResponse)
def ui_detail(scan_id: str, request: Request, db: Session = Depends(get_db)):
    viewer = _require_cookie_user(request, db)
    s = db.get(models.VulnScan, scan_id)
    if not s:
        raise HTTPException(404, "Not found")
    assert_can_view_scan(db, viewer, s)
    can_edit = True
    try:
        assert_can_edit_scan(db, viewer, s)
    except HTTPException:
        can_edit = False

    source_report = (
        db.get(models.Report, s.source_report_id) if s.source_report_id else None
    )

    # Attachments: anything stored under this scan's session_id, plus any
    # directly linked to scan_id.
    attach_q = db.query(models.Attachment).filter(
        (models.Attachment.scan_id == s.id)
        | ((models.Attachment.session_id != None)  # noqa: E711
           & (models.Attachment.session_id == s.source_session_id))
    ).order_by(models.Attachment.created_at.desc())
    attachments = attach_q.all()

    if viewer.role == models.Role.admin:
        my_projects = (
            db.query(models.Project).order_by(models.Project.name).all()
        )
    else:
        my_projects = sorted(viewer.projects, key=lambda p: p.name.lower())

    return templates.TemplateResponse(
        request,
        "scan_detail.html",
        {
            "user": viewer,
            "scan": s,
            "runs": s.runs,
            "findings": s.finding_rows,
            "can_edit": can_edit,
            "source_report": source_report,
            "attachments": attachments,
            "my_projects": my_projects,
            "severities": list(models.Severity),
            "states": list(models.ScanState),
            "finding_statuses": list(models.FindingStatus),
        },
    )


@ui.post("/{scan_id}/edit")
async def ui_edit(scan_id: str, request: Request, db: Session = Depends(get_db)):
    viewer = _require_cookie_user(request, db)
    s = db.get(models.VulnScan, scan_id)
    if not s:
        raise HTTPException(404, "Not found")
    assert_can_edit_scan(db, viewer, s)
    form = await request.form()

    str_fields = (
        "product", "scan_target", "harness_used", "scan_by", "results_file",
        "spreadsheet_link", "triaged_by", "notes",
    )
    for f in str_fields:
        if f in form:
            setattr(s, f, form.get(f, "").strip() if f != "notes" else form.get(f, ""))

    int_fields = ("findings", "fp", "sbp", "tp", "duplicates", "untriaged")
    for f in int_fields:
        if f in form:
            try:
                setattr(s, f, int(form.get(f) or 0))
            except ValueError:
                pass

    if "highest_severity" in form:
        s.highest_severity = models.Severity(form.get("highest_severity"))
    if "state" in form:
        s.state = models.ScanState(form.get("state"))

    db.commit()
    return RedirectResponse(f"/ui/scans/{s.id}", status_code=303)


@ui.post("/{scan_id}/project")
def ui_set_project(
    scan_id: str,
    request: Request,
    project_id: str = Form(""),
    db: Session = Depends(get_db),
):
    viewer = _require_cookie_user(request, db)
    s = db.get(models.VulnScan, scan_id)
    if not s:
        raise HTTPException(404, "Not found")
    assert_can_edit_scan(db, viewer, s)
    if project_id == "":
        s.project_id = None
        msg = "Detached from project."
    else:
        proj = db.get(models.Project, project_id)
        if not proj:
            raise HTTPException(400, "Project not found")
        if viewer.role != models.Role.admin and proj.created_by != viewer.id \
                and not any(m.id == viewer.id for m in proj.members):
            raise HTTPException(403, "You're not a member of that project")
        s.project_id = proj.id
        msg = f"Attached to project '{proj.name}'."
    # Sync the linked Run's project so session-scoped visibility stays right.
    if s.source_session_id:
        run_row = db.get(models.Run, s.source_session_id)
        if run_row is not None:
            run_row.project_id = s.project_id
    db.commit()
    from urllib.parse import quote
    return RedirectResponse(f"/ui/scans/{s.id}?ok={quote(msg)}", status_code=303)


@ui.post("/{scan_id}/confirm")
def ui_confirm(scan_id: str, request: Request, db: Session = Depends(get_db)):
    viewer = _require_cookie_user(request, db)
    s = db.get(models.VulnScan, scan_id)
    if not s:
        raise HTTPException(404, "Not found")
    assert_can_edit_scan(db, viewer, s)
    s.state = models.ScanState.confirmed
    db.commit()
    return RedirectResponse(f"/ui/scans/{s.id}", status_code=303)


@ui.post("/{scan_id}/delete")
def ui_delete(scan_id: str, request: Request, db: Session = Depends(get_db)):
    viewer = _require_cookie_user(request, db)
    s = db.get(models.VulnScan, scan_id)
    if not s:
        return RedirectResponse("/ui/scans", status_code=303)
    assert_can_edit_scan(db, viewer, s)
    db.delete(s)
    db.commit()
    return RedirectResponse("/ui/scans", status_code=303)


@ui.post("/{scan_id}/runs")
async def ui_add_run(scan_id: str, request: Request, db: Session = Depends(get_db)):
    viewer = _require_cookie_user(request, db)
    s = db.get(models.VulnScan, scan_id)
    if not s:
        raise HTTPException(404, "Not found")
    assert_can_edit_scan(db, viewer, s)
    form = await request.form()
    date_str = form.get("date", "").strip()
    date_val = None
    if date_str:
        try:
            date_val = dt.date.fromisoformat(date_str)
        except ValueError:
            pass
    r = models.RunLog(
        scan_id=s.id,
        user_id=viewer.id,
        day=form.get("day", "").strip(),
        date=date_val,
        run=form.get("run", "").strip(),
        box=form.get("box", "").strip(),
        product=form.get("product", "").strip() or s.product,
        harness=form.get("harness", "").strip() or s.harness_used,
        prompt=form.get("prompt", ""),
        results=form.get("results", ""),
        poc=form.get("poc", ""),
        comment=form.get("comment", ""),
        complete=bool(form.get("complete")),
    )
    db.add(r)
    db.commit()
    return RedirectResponse(f"/ui/scans/{s.id}", status_code=303)


@ui.post("/{scan_id}/runs/{run_id}/delete")
def ui_delete_run(
    scan_id: str, run_id: str, request: Request, db: Session = Depends(get_db)
):
    viewer = _require_cookie_user(request, db)
    r = db.get(models.RunLog, run_id)
    if not r or r.scan_id != scan_id:
        return RedirectResponse(f"/ui/scans/{scan_id}", status_code=303)
    assert_can_edit_scan(db, viewer, r.scan)
    db.delete(r)
    db.commit()
    return RedirectResponse(f"/ui/scans/{scan_id}", status_code=303)


# ---------------- findings (API, Bearer) ----------------

@api.post("/{scan_id}/findings", response_model=schemas.FindingOut, status_code=201)
def add_finding(
    scan_id: str,
    body: schemas.FindingCreate,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    s = db.get(models.VulnScan, scan_id)
    if not s:
        raise HTTPException(404, "Not found")
    assert_can_edit_scan(db, viewer, s)
    f = models.Finding(scan_id=s.id, user_id=viewer.id, **body.model_dump())
    db.add(f)
    db.commit()
    db.refresh(f)
    return _finding_to_out(f)


@api.patch("/{scan_id}/findings/{finding_id}", response_model=schemas.FindingOut)
def update_finding(
    scan_id: str,
    finding_id: str,
    body: schemas.FindingUpdate,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    import datetime as _dt
    f = db.get(models.Finding, finding_id)
    if not f or f.scan_id != scan_id:
        raise HTTPException(404, "Not found")
    assert_can_edit_scan(db, viewer, f.scan)
    payload = body.model_dump(exclude_unset=True)

    # Auto-stamp triaged_by + triaged_at when the dev verdict or tags
    # change, unless the caller already supplied an explicit triaged_by.
    triage_signal = (
        ("status" in payload and payload["status"] is not None)
        or ("tags" in payload and payload["tags"] is not None)
    )
    if triage_signal and "triaged_by" not in payload:
        f.triaged_by = viewer.email
        f.triaged_at = _dt.datetime.now(_dt.timezone.utc)
    elif "triaged_by" in payload and payload["triaged_by"] is not None:
        # Explicit triaged_by set — refresh triaged_at too.
        f.triaged_at = _dt.datetime.now(_dt.timezone.utc)

    # `tags` needs special handling: None means leave-unchanged but an
    # empty list is a real "clear all tags" signal.
    tags_val = payload.pop("tags", "__SENTINEL__")
    _apply_updates(f, payload)
    if tags_val != "__SENTINEL__" and tags_val is not None:
        # Normalize: lowercase, dedupe, only keep recognized values.
        allowed = {"sbp", "ss", "vuln"}
        cleaned = sorted({str(t).strip().lower() for t in tags_val if str(t).strip()})
        f.tags = [t for t in cleaned if t in allowed]

    db.commit()
    db.refresh(f)
    return _finding_to_out(f)


@api.delete("/{scan_id}/findings/{finding_id}", status_code=204)
def delete_finding(
    scan_id: str,
    finding_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    f = db.get(models.Finding, finding_id)
    if not f or f.scan_id != scan_id:
        return
    assert_can_edit_scan(db, viewer, f.scan)
    db.delete(f)
    db.commit()


_AI_VERDICT_SYSTEM = """\
You're triaging a single security finding produced by an automated
vulnerability scanner. Decide whether the finding looks like a real
exploitable issue (true_positive), a misfire (false_positive), or you
genuinely can't tell from the evidence (open). Be conservative — if
the description is too thin to judge, return open.

Respond with a single JSON object, no markdown fences, with keys:
  verdict:  "true_positive" | "false_positive" | "open"
  rationale: 1-3 sentences explaining your call.
"""


def _enrich_scan_findings(db: Session, scan: models.VulnScan, *, only_thin: bool) -> int:
    """Re-extract per-finding detail from the scan's source report. Updates
    findings in place; returns the number of findings touched. `only_thin`
    skips any finding that already has both a description and a PoC, so
    re-running this is cheap and idempotent.
    """
    from .. import crypto
    from ..ai.extractor import enrich_findings

    if not scan.source_report_id:
        return 0
    rpt = db.get(models.Report, scan.source_report_id)
    if not rpt:
        return 0

    targets = list(scan.finding_rows)
    if only_thin:
        targets = [
            f for f in targets
            if not (f.proof_of_concept and f.steps_to_reproduce and f.references)
        ]
    if not targets:
        return 0

    try:
        source_md = crypto.decrypt(rpt.content_enc).decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("enrich: couldn't decrypt source report %s: %s", rpt.id, e)
        return 0

    enriched = enrich_findings(
        source_md,
        [
            {"id": f.id, "title": f.title, "current_description": f.description}
            for f in targets
        ],
    )
    if not enriched:
        return 0

    by_id = {e["id"]: e for e in enriched if e.get("id")}
    touched = 0
    for f in targets:
        e = by_id.get(f.id)
        if not e:
            continue
        # Only fill empty fields so we don't clobber user edits.
        if not f.description and e["description"]:
            f.description = e["description"]
        if not f.steps_to_reproduce and e["steps_to_reproduce"]:
            f.steps_to_reproduce = e["steps_to_reproduce"]
        if not f.proof_of_concept and e["proof_of_concept"]:
            f.proof_of_concept = e["proof_of_concept"]
        if not f.references and e["references"]:
            f.references = e["references"]
        if not f.cwe and e["cwe"]:
            f.cwe = e["cwe"]
        if not f.cve and e["cve"]:
            f.cve = e["cve"]
        if not f.affected_component and e["affected_component"]:
            f.affected_component = e["affected_component"]
        touched += 1
    db.commit()
    log.info("enrich: scan %s -> %d findings filled", scan.id, touched)
    return touched


@api.post("/_admin/enrich_thin_findings")
def api_admin_enrich_thin_findings(
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Backfill: iterate every scan with a source_report_id and re-extract
    any findings that are still thin (missing PoC / steps / references).
    Admin only — it's a batch of Claude calls.
    """
    if viewer.role != models.Role.admin:
        raise HTTPException(403, "Admin only")
    rows = (
        db.query(models.VulnScan)
        .filter(models.VulnScan.source_report_id.isnot(None))
        .all()
    )
    touched = 0
    scans_visited = 0
    for s in rows:
        scans_visited += 1
        try:
            touched += _enrich_scan_findings(db, s, only_thin=True)
        except Exception as e:
            log.warning("enrich backfill failed for scan %s: %s", s.id, e)
    return {"scans_visited": scans_visited, "findings_touched": touched}


@api.post("/{scan_id}/enrich_findings")
def api_enrich_scan_findings(
    scan_id: str,
    only_thin: bool = True,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Re-extract per-finding detail from this scan's source report.

    Any project member who can edit the scan can trigger it. Defaults to
    only_thin=True so re-running is cheap and idempotent. Pass
    ?only_thin=false to refill every finding regardless.
    """
    s = db.get(models.VulnScan, scan_id)
    if not s:
        raise HTTPException(404, "Not found")
    assert_can_edit_scan(db, viewer, s)
    if not s.source_report_id:
        raise HTTPException(400, "Scan has no source report to enrich from")
    touched = _enrich_scan_findings(db, s, only_thin=only_thin)
    return {"scan_id": s.id, "touched": touched}


@api.post("/{scan_id}/findings/{finding_id}/ai_verdict",
          response_model=schemas.FindingOut)
def run_ai_verdict(
    scan_id: str,
    finding_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Ask Claude to TP/FP a finding. Anyone allowed to edit the scan
    can trigger it. Writes ai_verdict + ai_rationale; never touches the
    dev verdict (`status`)."""
    import json
    from ..config import provider_keys

    f = db.get(models.Finding, finding_id)
    if not f or f.scan_id != scan_id:
        raise HTTPException(404, "Not found")
    assert_can_edit_scan(db, viewer, f.scan)

    if not provider_keys.anthropic_api_key:
        raise HTTPException(503, "Anthropic API key not configured")

    summary_lines = [
        f"Title: {f.title or '(none)'}",
        f"Severity: {f.severity.value}",
        f"CWE: {f.cwe or '(none)'}",
        f"CVE: {f.cve or '(none)'}",
        f"Affected component: {f.affected_component or '(none)'}",
        "",
        "## Description",
        f.description or "(empty)",
        "",
        "## Steps to reproduce",
        f.steps_to_reproduce or "(empty)",
        "",
        "## Proof of concept",
        f.proof_of_concept or "(empty)",
        "",
        "## Remediation",
        f.remediation or "(empty)",
        "",
        "## References",
        f.references or "(none)",
    ]
    prompt = "\n".join(summary_lines)

    import anthropic
    client = anthropic.Anthropic(api_key=provider_keys.anthropic_api_key)
    try:
        resp = client.messages.create(
            model=provider_keys.anthropic_model,
            max_tokens=512,
            system=_AI_VERDICT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        # Strip code fences if Claude ignored the no-markdown instruction.
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip("`\n ")
        parsed = json.loads(raw)
    except Exception as e:
        log.exception("ai_verdict failed for finding %s", finding_id)
        raise HTTPException(502, f"AI verdict failed: {e}")

    verdict = (parsed.get("verdict") or "open").strip().lower()
    if verdict not in {"open", "true_positive", "false_positive"}:
        verdict = "open"
    rationale = (parsed.get("rationale") or "")[:4000]
    f.ai_verdict = models.AIVerdict(verdict)
    f.ai_rationale = rationale
    db.add(models.AIVerdictRun(
        finding_id=f.id,
        ran_by=viewer.id,
        verdict=models.AIVerdict(verdict),
        rationale=rationale,
        model=provider_keys.anthropic_model,
    ))
    db.commit()
    db.refresh(f)
    return _finding_to_out(f)


@api.get("/{scan_id}/findings/{finding_id}/ai_verdict",
         response_model=list[schemas.AIVerdictRunOut])
def list_ai_verdicts(
    scan_id: str,
    finding_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """History of AI verdict runs for one finding, newest first."""
    f = db.get(models.Finding, finding_id)
    if not f or f.scan_id != scan_id:
        raise HTTPException(404, "Not found")
    # Read access matches the scan's view-permission, not edit.
    assert_can_view_scan(db, viewer, f.scan)
    rows = (
        db.query(models.AIVerdictRun)
        .filter(models.AIVerdictRun.finding_id == f.id)
        .order_by(models.AIVerdictRun.created_at.desc())
        .all()
    )
    out: list[schemas.AIVerdictRunOut] = []
    for r in rows:
        o = schemas.AIVerdictRunOut.model_validate(r)
        if r.ran_by:
            u = db.get(models.User, r.ran_by)
            if u:
                o.ran_by_email = u.email
        out.append(o)
    return out


# ---------------- findings (UI, cookie) ----------------

def _finding_fields_from_form(form) -> dict:
    """Pull the editable finding fields from a submitted form."""
    return {
        "title":              form.get("title", "").strip(),
        "severity":           models.Severity(form.get("severity", "unknown")),
        "status":             models.FindingStatus(form.get("status", "open")),
        "cwe":                form.get("cwe", "").strip(),
        "cve":                form.get("cve", "").strip(),
        "affected_component": form.get("affected_component", "").strip(),
        "description":        form.get("description", ""),
        "steps_to_reproduce": form.get("steps_to_reproduce", ""),
        "remediation":        form.get("remediation", ""),
        "proof_of_concept":   form.get("proof_of_concept", ""),
        "references":         form.get("references", ""),
        "assigned_to":        form.get("assigned_to", "").strip(),
        "triaged_by":         form.get("triaged_by", "").strip(),
    }


@ui.post("/{scan_id}/findings")
async def ui_add_finding(scan_id: str, request: Request, db: Session = Depends(get_db)):
    viewer = _require_cookie_user(request, db)
    s = db.get(models.VulnScan, scan_id)
    if not s:
        raise HTTPException(404, "Not found")
    assert_can_edit_scan(db, viewer, s)
    fields = _finding_fields_from_form(await request.form())
    f = models.Finding(scan_id=s.id, user_id=viewer.id, **fields)
    if f.status != models.FindingStatus.open:
        f.triaged_at = dt.datetime.now(dt.timezone.utc)
        if not f.triaged_by:
            f.triaged_by = viewer.email
    db.add(f)
    db.commit()
    return RedirectResponse(f"/ui/scans/{s.id}", status_code=303)


@ui.get("/{scan_id}/findings/{finding_id}", response_class=HTMLResponse)
def ui_finding_detail(
    scan_id: str, finding_id: str, request: Request, db: Session = Depends(get_db)
):
    viewer = _require_cookie_user(request, db)
    f = db.get(models.Finding, finding_id)
    if not f or f.scan_id != scan_id:
        raise HTTPException(404, "Not found")
    assert_can_view_scan(db, viewer, f.scan)
    can_edit = True
    try:
        assert_can_edit_scan(db, viewer, f.scan)
    except HTTPException:
        can_edit = False
    return templates.TemplateResponse(
        request,
        "finding_detail.html",
        {
            "user": viewer,
            "scan": f.scan,
            "f": f,
            "can_edit": can_edit,
            "severities": list(models.Severity),
            "statuses": list(models.FindingStatus),
        },
    )


@ui.post("/{scan_id}/findings/{finding_id}/edit")
async def ui_finding_edit(
    scan_id: str, finding_id: str, request: Request, db: Session = Depends(get_db)
):
    viewer = _require_cookie_user(request, db)
    f = db.get(models.Finding, finding_id)
    if not f or f.scan_id != scan_id:
        raise HTTPException(404, "Not found")
    assert_can_edit_scan(db, viewer, f.scan)
    fields = _finding_fields_from_form(await request.form())
    old_status = f.status
    for k, v in fields.items():
        setattr(f, k, v)
    # Stamp triaged_at on any status transition out of 'open'.
    if f.status != models.FindingStatus.open and (
        old_status == models.FindingStatus.open or not f.triaged_at
    ):
        f.triaged_at = dt.datetime.now(dt.timezone.utc)
        if not f.triaged_by:
            f.triaged_by = viewer.email
    db.commit()
    return RedirectResponse(f"/ui/scans/{scan_id}/findings/{finding_id}", status_code=303)


@ui.post("/{scan_id}/findings/{finding_id}/delete")
def ui_finding_delete(
    scan_id: str, finding_id: str, request: Request, db: Session = Depends(get_db)
):
    viewer = _require_cookie_user(request, db)
    f = db.get(models.Finding, finding_id)
    if not f or f.scan_id != scan_id:
        return RedirectResponse(f"/ui/scans/{scan_id}", status_code=303)
    assert_can_edit_scan(db, viewer, f.scan)
    db.delete(f)
    db.commit()
    return RedirectResponse(f"/ui/scans/{scan_id}", status_code=303)
