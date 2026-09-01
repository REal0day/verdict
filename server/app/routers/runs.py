"""Browse uploaded reports grouped by Claude Code session ("runs").

A run = all reports sharing the same `session_id`. The PostToolUse hook on the
agent fills `session_id` automatically; uploads from the watcher daemon (or
manually-dropped files) have no session_id and never appear here.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..config import settings
from ..database import get_db
from ..permissions import assert_can_view_report, scope_reports

log = logging.getLogger("irs.runs")
router = APIRouter(tags=["runs-ui"])
api = APIRouter(prefix="/runs", tags=["runs"])
templates = Jinja2Templates(directory="app/templates")


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


def _require_user(request: Request, db: Session) -> models.User:
    u = _user_from_cookie(request, db)
    if not u:
        raise HTTPException(401, "Not logged in")
    return u


def _ensure_run_row(db: Session, session_id: str, owner_user_id: str) -> models.Run:
    """Lazily create a Run row for a session_id if one doesn't exist yet."""
    run = db.get(models.Run, session_id)
    if run is None:
        run = models.Run(session_id=session_id, user_id=owner_user_id, title="")
        db.add(run)
        db.commit()
        db.refresh(run)
    return run


@router.get("/ui/runs", response_class=HTMLResponse)
def list_runs(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)

    # Aggregate one row per session_id, scoped to what this viewer can see.
    rows_q = (
        db.query(
            models.Report.session_id.label("session_id"),
            func.count(models.Report.id).label("file_count"),
            func.min(models.Report.created_at).label("first_seen"),
            func.max(models.Report.created_at).label("last_seen"),
        )
        .filter(models.Report.session_id.isnot(None))
        .group_by(models.Report.session_id)
        .order_by(func.max(models.Report.created_at).desc())
    )
    rows_q = scope_reports(rows_q, db, user)
    rows = rows_q.limit(200).all()

    runs = []
    for r in rows:
        rep = (
            db.query(models.Report)
            .filter(models.Report.session_id == r.session_id)
            .order_by(models.Report.created_at)
            .first()
        )
        scan = (
            db.query(models.VulnScan)
            .filter(models.VulnScan.source_session_id == r.session_id)
            .first()
        )
        run = db.get(models.Run, r.session_id)  # may be None for legacy data
        runs.append({
            "session_id": r.session_id,
            "title": run.title if run else "",
            "project": run.project if run else None,
            "file_count": r.file_count,
            "first_seen": r.first_seen,
            "last_seen": r.last_seen,
            "owner_email": rep.user.email if rep and rep.user else None,
            "hostname":   rep.agent.hostname if rep and rep.agent else None,
            "scan": scan,
        })

    return templates.TemplateResponse(
        request, "runs_list.html", {"user": user, "runs": runs}
    )


@router.get("/ui/runs/{session_id}", response_class=HTMLResponse)
def run_detail(session_id: str, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)

    q = (
        db.query(models.Report)
        .filter(models.Report.session_id == session_id)
        .order_by(models.Report.created_at)
    )
    q = scope_reports(q, db, user)
    reports = q.all()
    if not reports:
        raise HTTPException(404, "No reports in this run (or not visible to you)")

    run = _ensure_run_row(db, session_id, reports[0].user_id)
    scan = (
        db.query(models.VulnScan)
        .filter(models.VulnScan.source_session_id == session_id)
        .first()
    )

    # Projects the viewer can put this run into (their own + admin sees all).
    if user.role == models.Role.admin:
        my_projects = (
            db.query(models.Project).order_by(models.Project.name).all()
        )
    else:
        my_projects = sorted(user.projects, key=lambda p: p.name.lower())

    from .. import crypto
    enriched = [
        {
            "r": r,
            "summary": crypto.decrypt_str(r.summary_enc) if r.summary_enc else "",
        }
        for r in reports
    ]
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "user": user,
            "session_id": session_id,
            "run": run,
            "reports": enriched,
            "scan": scan,
            "my_projects": my_projects,
            "can_edit": (run.user_id == user.id or user.role == models.Role.admin),
        },
    )


@router.post("/ui/runs/{session_id}/edit")
def run_edit(
    session_id: str,
    request: Request,
    title: str = Form(""),
    project_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    # Make sure the run exists + viewer can see it
    first_report = (
        db.query(models.Report)
        .filter(models.Report.session_id == session_id)
        .order_by(models.Report.created_at).first()
    )
    if not first_report:
        raise HTTPException(404, "No reports in this run")
    # Viewer must be able to see *some* report in the run (= owner / team /
    # admin / project member). We just check the first one.
    from ..permissions import assert_can_view_report
    assert_can_view_report(db, user, first_report)

    run = _ensure_run_row(db, session_id, first_report.user_id)
    # Only the run owner or admin can mutate; non-owners shouldn't relabel
    # someone else's run even if they can view it.
    if run.user_id != user.id and user.role != models.Role.admin:
        raise HTTPException(403, "Only the run owner (or an admin) can edit")

    run.title = title.strip()
    if project_id == "":
        run.project_id = None
        msg = "Run saved (no project)."
    else:
        proj = db.get(models.Project, project_id)
        if not proj:
            raise HTTPException(400, "Project not found")
        # Viewer must be a member of (or admin/creator of) the target project
        # to drop the run into it.
        if user.role != models.Role.admin and proj.created_by != user.id \
                and not any(m.id == user.id for m in proj.members):
            raise HTTPException(403, "You're not a member of that project")
        run.project_id = proj.id
        msg = f"Run saved · attached to '{proj.name}'."
    db.commit()
    from urllib.parse import quote
    return RedirectResponse(f"/ui/runs/{session_id}?ok={quote(msg)}", status_code=303)


# ---------------- JSON API (Bearer) ----------------

@api.get("", response_model=list[dict])
def api_list_runs(
    project_id: str | None = None,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    rows_q = (
        db.query(
            models.Report.session_id.label("session_id"),
            func.count(models.Report.id).label("file_count"),
            func.min(models.Report.created_at).label("first_seen"),
            func.max(models.Report.created_at).label("last_seen"),
        )
        .filter(models.Report.session_id.isnot(None))
        .group_by(models.Report.session_id)
        .order_by(func.max(models.Report.created_at).desc())
    )
    rows_q = scope_reports(rows_q, db, viewer)
    if project_id:
        in_project = (
            db.query(models.Run.session_id)
            .filter(models.Run.project_id == project_id)
            .subquery()
        )
        rows_q = rows_q.filter(models.Report.session_id.in_(in_project))
    rows = rows_q.limit(200).all()

    # Pull all related rows in three batched queries instead of N-per-row.
    sids = [r.session_id for r in rows]
    if not sids:
        return []
    first_reports = {
        rp.session_id: rp
        for rp in db.query(models.Report)
                   .filter(models.Report.session_id.in_(sids))
                   .order_by(models.Report.session_id, models.Report.created_at).all()
        # `.all()` returns rows in order; dict-keying keeps the *last* read
        # (which would be the latest); we want the earliest, so reverse:
    } if False else {}
    # simpler: just query the earliest per session
    earliest = (
        db.query(models.Report.session_id, func.min(models.Report.created_at))
          .filter(models.Report.session_id.in_(sids))
          .group_by(models.Report.session_id).all()
    )
    earliest_ts = {sid: ts for sid, ts in earliest}

    reps_by_sid: dict[str, models.Report] = {}
    for rp in (
        db.query(models.Report).filter(models.Report.session_id.in_(sids)).all()
    ):
        cur = reps_by_sid.get(rp.session_id)
        if cur is None or rp.created_at <= cur.created_at:
            reps_by_sid[rp.session_id] = rp

    scans_by_sid = {
        s.source_session_id: s
        for s in db.query(models.VulnScan)
                   .filter(models.VulnScan.source_session_id.in_(sids)).all()
    }
    runs_by_sid = {
        r.session_id: r
        for r in db.query(models.Run).filter(models.Run.session_id.in_(sids)).all()
    }

    # Compute per-user-per-bucket run number, oldest first.
    # Bucket: project_id if the run is in a project, else "product:<product>",
    # else "no-project-no-product".
    def bucket_for(run: models.Run | None, scan_product: str | None) -> str:
        if run and run.project_id:
            return f"proj:{run.project_id}"
        product = (run.product if run and run.product else scan_product) or ""
        return f"prod:{product.lower()}" if product else "none"

    # gather (user_id, bucket, session_id, created_at) and sort oldest→newest
    # within each user/bucket to assign #1, #2, ...
    runlist: list[tuple[str, str, str, "dt.datetime"]] = []
    for r in rows:
        rep = reps_by_sid.get(r.session_id)
        if not rep:
            continue
        run = runs_by_sid.get(r.session_id)
        scan = scans_by_sid.get(r.session_id)
        ts = earliest_ts.get(r.session_id) or r.first_seen
        runlist.append((
            rep.user_id, bucket_for(run, scan.product if scan else None),
            r.session_id, ts,
        ))
    # sort by user, bucket, time
    runlist.sort(key=lambda t: (t[0], t[1], t[3]))
    user_run_number: dict[str, int] = {}
    bucket_counter: dict[tuple[str, str], int] = {}
    for uid, b, sid, _ in runlist:
        bucket_counter[(uid, b)] = bucket_counter.get((uid, b), 0) + 1
        user_run_number[sid] = bucket_counter[(uid, b)]

    out = []
    for r in rows:
        rep = reps_by_sid.get(r.session_id)
        scan = scans_by_sid.get(r.session_id)
        run = runs_by_sid.get(r.session_id)
        product = (
            run.project.name if run and run.project else None
        ) or (run.product if run else None) or (scan.product if scan else None) or ""
        out.append({
            "session_id":   r.session_id,
            "title":        run.title if run else "",
            "product":      product,
            "subcomponent": run.subcomponent if run else "",
            "project_id":   run.project_id if run else None,
            "project_name": run.project.name if run and run.project else None,
            "user_run_number": user_run_number.get(r.session_id),
            "file_count":   r.file_count,
            "first_seen":   r.first_seen.isoformat() if r.first_seen else None,
            "last_seen":    r.last_seen.isoformat() if r.last_seen else None,
            "owner_email":  rep.user.email if rep and rep.user else None,
            "owner_id":     rep.user_id if rep else None,
            "viewer_owns":  bool(rep and rep.user_id == viewer.id),
            "hostname":     rep.agent.hostname if rep and rep.agent else None,
            "scan_id":      scan.id if scan else None,
            "scan_state":   scan.state.value if scan else None,
            "scan_product": scan.product if scan else None,
        })
    return out


@api.get("/{session_id}", response_model=dict)
def api_run_detail(
    session_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    q = (
        db.query(models.Report)
        .filter(models.Report.session_id == session_id)
        .order_by(models.Report.created_at)
    )
    q = scope_reports(q, db, viewer)
    reports = q.all()
    if not reports:
        raise HTTPException(404, "No reports in this run")
    # Make sure the Run row exists for state we want to expose
    run = db.get(models.Run, session_id)
    if run is None:
        run = models.Run(session_id=session_id, user_id=reports[0].user_id, title="")
        db.add(run); db.commit(); db.refresh(run)
    scan = (
        db.query(models.VulnScan)
        .filter(models.VulnScan.source_session_id == session_id).first()
    )
    from .. import crypto
    return {
        "session_id":   run.session_id,
        "title":        run.title,
        "product":      run.product,
        "subcomponent": run.subcomponent,
        "project_id":   run.project_id,
        "project_name": run.project.name if run.project else None,
        "harness_id":   run.harness_id,
        "harness_name": run.harness.name if run.harness else None,
        "scan": (
            {
                "id": scan.id, "product": scan.product, "state": scan.state.value,
                "highest_severity": scan.highest_severity.value,
                "findings": scan.findings,
            } if scan else None
        ),
        "reports": [
            {
                "id": r.id, "filename": r.filename, "source_tool": r.source_tool.value,
                "created_at": r.created_at.isoformat(),
                "summary": (crypto.decrypt_str(r.summary_enc) if r.summary_enc else ""),
            } for r in reports
        ],
    }


@api.patch("/{session_id}", response_model=dict)
def api_run_update(
    session_id: str,
    body: schemas.RunUpdate,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    first_report = (
        db.query(models.Report)
        .filter(models.Report.session_id == session_id)
        .order_by(models.Report.created_at).first()
    )
    if not first_report:
        raise HTTPException(404, "No reports in this run")
    assert_can_view_report(db, viewer, first_report)
    run = _ensure_run_row(db, session_id, first_report.user_id)
    if run.user_id != viewer.id and viewer.role != models.Role.admin:
        raise HTTPException(403, "Only the run owner (or an admin) can edit")
    payload = body.model_dump(exclude_unset=True)
    if "title" in payload:
        run.title = (payload["title"] or "").strip()
    if "product" in payload:
        run.product = (payload["product"] or "").strip()
    if "subcomponent" in payload:
        run.subcomponent = (payload["subcomponent"] or "").strip()
    if "project_id" in payload:
        pid = payload["project_id"]
        if pid in (None, ""):
            run.project_id = None
        else:
            proj = db.get(models.Project, pid)
            if not proj:
                raise HTTPException(400, "Project not found")
            if viewer.role != models.Role.admin and proj.created_by != viewer.id \
                    and not any(m.id == viewer.id for m in proj.members):
                raise HTTPException(403, "You're not a member of that project")
            run.project_id = proj.id
    if "harness_id" in payload:
        hid = payload["harness_id"]
        if hid in (None, ""):
            run.harness_id = None
        else:
            h = db.get(models.Harness, hid)
            if not h:
                raise HTTPException(400, "Harness not found")
            # Same visibility rule as elsewhere: owner / project member / admin.
            if viewer.role != models.Role.admin and h.user_id != viewer.id:
                if not (h.project_id and any(p.id == h.project_id for p in viewer.projects)):
                    raise HTTPException(403, "You can't use that harness")
            run.harness_id = h.id
    db.commit()
    db.refresh(run)
    return {
        "session_id":   run.session_id,
        "title":        run.title,
        "product":      run.product,
        "subcomponent": run.subcomponent,
        "project_id":   run.project_id,
        "project_name": run.project.name if run.project else None,
        "harness_id":   run.harness_id,
        "harness_name": run.harness.name if run.harness else None,
    }
