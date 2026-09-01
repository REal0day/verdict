import base64
import hashlib
import re

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy.orm import Session

from .. import models, schemas, crypto
from ..ai.extractor import extract
from ..ai.summarizer import summarize
from ..auth import get_current_agent, get_current_user
from ..database import get_db, SessionLocal
from ..permissions import scope_reports, assert_can_view_report, assert_can_delete

router = APIRouter(prefix="/reports", tags=["reports"])


# First H1 heading in the markdown body, optionally after a YAML frontmatter
# block. Used to default `Report.title` from the file's own header so the UI
# isn't full of generic filenames.
_FRONTMATTER_RE = re.compile(r"\A\s*---\s*\n.*?\n---\s*\n", re.DOTALL)
_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


def extract_title_from_markdown(text: str, max_len: int = 200) -> str:
    if not text:
        return ""
    body = _FRONTMATTER_RE.sub("", text, count=1)
    m = _H1_RE.search(body)
    if not m:
        return ""
    title = m.group(1).strip()
    # Strip surrounding markdown punctuation Claude sometimes adds.
    title = title.strip("*_`")
    return title[:max_len]


# ---------- ingest (called by agents) ----------
@router.post("/ingest", response_model=schemas.ReportOut, status_code=201)
def ingest(
    body: schemas.ReportIngest,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    agent: models.Agent = Depends(get_current_agent),
):
    import datetime as _dt
    raw = base64.b64decode(body.content_b64)
    sha = hashlib.sha256(raw).hexdigest()
    if sha != body.sha256:
        raise HTTPException(400, "sha256 mismatch")

    # Always touch the agent's last-seen so the onboarding UI ("Done" step)
    # can show a live heartbeat even when the upload is a duplicate.
    agent.last_seen = _dt.datetime.now(_dt.timezone.utc)

    existing = (
        db.query(models.Report)
        .filter(models.Report.user_id == agent.user_id, models.Report.sha256 == sha)
        .first()
    )
    if existing:
        db.commit()
        return _to_out(existing)

    text = raw.decode("utf-8", errors="replace")
    title = extract_title_from_markdown(text)
    rpt = models.Report(
        user_id=agent.user_id,
        agent_id=agent.id,
        source_tool=body.source_tool,
        filename=body.filename,
        title=title,
        original_path=body.original_path,
        sha256=sha,
        size_bytes=len(raw),
        content_enc=crypto.encrypt(raw),
        file_mtime=body.file_mtime,
        session_id=body.session_id,
    )
    db.add(rpt)
    db.flush()  # need rpt.id for the notification link

    # Tell the user their agent worked. One notification per fresh report;
    # POC attachments are sidecar and don't double-notify.
    db.add(models.Notification(
        user_id=agent.user_id,
        kind=models.NotificationKind.report_uploaded,
        title=f"Agent {agent.hostname} uploaded a report",
        body=title or body.filename,
        link=f"/reports/{rpt.id}",
        data={"report_id": rpt.id, "agent_id": agent.id},
    ))
    db.commit()
    db.refresh(rpt)

    bg.add_task(_summarize_and_store, rpt.id, text)
    bg.add_task(_extract_and_store_draft, rpt.id, text)
    return _to_out(rpt)


# ---------- browser upload (logged-in user) ----------
@router.post("/upload", response_model=schemas.ReportOut, status_code=201)
async def upload_report(
    bg: BackgroundTasks,
    file: UploadFile = File(...),
    project_id: str | None = Form(None),
    scan_id: str | None = Form(None),
    title: str | None = Form(None),
    create_scan: bool = Form(False),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Upload a single report/document from the browser. Stores any file type,
    optionally tags it to a product and/or attaches it to an existing scan (to
    back a scan that has summary numbers but no source document — the scan's
    counts are left untouched). AI draft-scan extraction runs only for text
    files, only when `create_scan` is set and no existing scan was chosen."""
    from ..permissions import assert_can_edit_scan

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file")
    sha = hashlib.sha256(raw).hexdigest()

    # Decode as text only if it really is text, so we don't try to extract a PDF.
    text: str | None = None
    try:
        decoded = raw.decode("utf-8")
        if "\x00" not in decoded:
            text = decoded
    except UnicodeDecodeError:
        text = None

    proj = None
    if project_id:
        proj = db.get(models.Project, project_id)
        if not proj:
            raise HTTPException(400, "Product not found")

    scan = None
    if scan_id:
        scan = db.get(models.VulnScan, scan_id)
        if not scan:
            raise HTTPException(400, "Scan not found")
        assert_can_edit_scan(db, viewer, scan)

    # Dedupe by (user, sha): reuse the existing row and just (re)apply links.
    rpt = (db.query(models.Report)
             .filter(models.Report.user_id == viewer.id,
                     models.Report.sha256 == sha).first())
    fresh = rpt is None
    if fresh:
        rpt = models.Report(
            user_id=viewer.id,
            source_tool=models.SourceTool.other,
            filename=file.filename or "upload",
            title=((title or "").strip()
                   or (extract_title_from_markdown(text) if text else "")
                   or "")[:255],
            sha256=sha,
            size_bytes=len(raw),
            content_enc=crypto.encrypt(raw),
        )
        db.add(rpt)
        db.flush()
    elif title:
        rpt.title = title.strip()[:255]

    if proj is not None:
        rpt.project_id = proj.id
    if scan is not None:
        rpt.scan_id = scan.id
        # Give the scan a backing document if it has none — never touch its counts.
        if not scan.source_report_id:
            scan.source_report_id = rpt.id
        # If no product was chosen, inherit the scan's.
        if proj is None and not rpt.project_id and scan.project_id:
            rpt.project_id = scan.project_id

    db.commit()
    db.refresh(rpt)

    # Optional AI draft scan — only a fresh text file with no existing scan link.
    if fresh and create_scan and scan is None and text:
        bg.add_task(_summarize_and_store, rpt.id, text)
        bg.add_task(_extract_and_store_draft, rpt.id, text)

    return _to_out(rpt, db)


def _summarize_and_store(report_id: str, text: str):
    s = summarize(text)
    if not s:
        return
    db = SessionLocal()
    try:
        rpt = db.get(models.Report, report_id)
        if rpt:
            rpt.summary_enc = crypto.encrypt_str(s)
            db.commit()
    finally:
        db.close()


def _store_runs_and_findings(db, scan, result, user_id: str):
    """Write the extractor's runs + findings onto `scan`. Shared by the
    auto-extract draft path and the on-demand /reports/{id}/extract endpoint."""
    for r in result.get("runs") or []:
        from datetime import date as _date
        date_val = None
        if r.get("date"):
            try:
                date_val = _date.fromisoformat(r["date"])
            except ValueError:
                pass
        db.add(models.RunLog(
            scan_id=scan.id,
            user_id=user_id,
            day=r["day"], date=date_val, run=r["run"], box=r["box"],
            product=r["product"], harness=r["harness"],
            prompt=r["prompt"], results=r["results"], poc=r["poc"],
            comment=r["comment"], complete=r["complete"],
        ))
    for f in result.get("findings") or []:
        # The extractor's `status` is the AI's read of whether the report calls
        # this a TP/FP; the *dev* verdict always starts "open" so a human still
        # confirms. Map TP/FP onto ai_verdict; sbp survives as a tag.
        ai_status = (f.get("status") or "open")
        ai_verdict = models.AIVerdict.open
        if ai_status == "true_positive":
            ai_verdict = models.AIVerdict.true_positive
        elif ai_status == "false_positive":
            ai_verdict = models.AIVerdict.false_positive
        tags: list[str] = []
        if ai_status == "sbp":
            tags.append("sbp")
        db.add(models.Finding(
            scan_id=scan.id,
            user_id=user_id,
            title=f["title"],
            severity=models.Severity(f["severity"]),
            status=models.FindingStatus.open,
            cwe=f["cwe"],
            cve=f["cve"],
            affected_component=f["affected_component"],
            description=f["description"],
            steps_to_reproduce=f["steps_to_reproduce"],
            remediation=f["remediation"],
            proof_of_concept=f["proof_of_concept"],
            references=f["references"],
            assigned_to=f["assigned_to"],
            triaged_by="",   # no human has triaged yet
            ai_verdict=ai_verdict,
            tags=tags,
        ))


@router.post("/{report_id}/extract")
def extract_report_findings(
    report_id: str,
    replace: bool = False,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Run the server AI over an already-stored report (e.g. an uploaded CSV
    that's showing raw) and write its findings onto the report's linked scan,
    creating a draft scan if none is linked. The scan's stored summary counts
    are NOT changed — this populates the findings detail. 409 if the scan
    already has findings unless replace=true (which overwrites them)."""
    from ..permissions import assert_can_edit_scan
    rpt = db.get(models.Report, report_id)
    if not rpt:
        raise HTTPException(404, "Not found")
    assert_can_view_report(db, viewer, rpt)

    text = crypto.decrypt(rpt.content_enc).decode("utf-8", errors="replace")
    result = extract(text)
    if not result or not result.get("findings"):
        raise HTTPException(422, "The AI couldn't extract any findings from this document.")

    scan = _linked_scan(rpt, db)
    created_scan = False
    if scan is None:
        s = result["scan"]
        scan = models.VulnScan(
            user_id=rpt.user_id, source_report_id=rpt.id,
            state=models.ScanState.draft, project_id=rpt.project_id,
            product=s["product"], scan_target=s["scan_target"],
            harness_used=s["harness_used"], scan_by=s["scan_by"],
            results_file=s["results_file"], spreadsheet_link=s["spreadsheet_link"],
            triaged_by=s["triaged_by"], findings=s["findings"],
            tp=s["tp"], fp=s["fp"], sbp=s["sbp"],
            duplicates=s["duplicates"], untriaged=s["untriaged"],
            highest_severity=models.Severity(s["highest_severity"]), notes=s["notes"],
        )
        db.add(scan); db.flush()
        created_scan = True
    assert_can_edit_scan(db, viewer, scan)

    existing = db.query(models.Finding).filter(models.Finding.scan_id == scan.id).count()
    if existing and not replace:
        raise HTTPException(
            409, f"That scan already has {existing} finding(s). "
                 "Re-run with replace=true to overwrite them.")
    if existing and replace:
        db.query(models.Finding).filter(models.Finding.scan_id == scan.id).delete()

    _store_runs_and_findings(db, scan, result, rpt.user_id)
    # Back the scan with this report (don't disturb its stored counts).
    if not scan.source_report_id:
        scan.source_report_id = rpt.id
    if not rpt.scan_id:
        rpt.scan_id = scan.id
    db.commit()

    n = db.query(models.Finding).filter(models.Finding.scan_id == scan.id).count()
    return {"scan_id": scan.id, "findings": n,
            "product": scan.product or "", "created_scan": created_scan}


def _extract_and_store_draft(report_id: str, text: str):
    """Best-effort: turn the markdown into a draft VulnScan + RunLogs."""
    import logging
    log = logging.getLogger("irs.reports.extract")
    try:
        result = extract(text)
    except Exception as e:
        log.warning("extract failed for %s: %s", report_id, e)
        return
    if not result:
        return

    db = SessionLocal()
    try:
        rpt = db.get(models.Report, report_id)
        if not rpt:
            return

        # Already derived a scan for this exact report? Skip (re-upload case).
        if db.query(models.VulnScan).filter(
            models.VulnScan.source_report_id == rpt.id
        ).first():
            log.info("draft scan already exists for report %s; skipping", rpt.id)
            return

        # If this report came from a Claude session we've already extracted from,
        # merge into that existing scan instead of creating a new one. The user
        # may have already edited it; only append new findings/runs.
        scan = None
        if rpt.session_id:
            scan = (
                db.query(models.VulnScan)
                .filter(models.VulnScan.source_session_id == rpt.session_id)
                .order_by(models.VulnScan.created_at)
                .first()
            )

        s = result["scan"]
        if scan is None:
            scan = models.VulnScan(
                user_id=rpt.user_id,
                source_report_id=rpt.id,
                source_session_id=rpt.session_id,
                state=models.ScanState.draft,
                product=s["product"],
                scan_target=s["scan_target"],
                harness_used=s["harness_used"],
                scan_by=s["scan_by"],
                results_file=s["results_file"],
                spreadsheet_link=s["spreadsheet_link"],
                triaged_by=s["triaged_by"],
                findings=s["findings"],
                tp=s["tp"],
                fp=s["fp"],
                sbp=s["sbp"],
                duplicates=s["duplicates"],
                untriaged=s["untriaged"],
                highest_severity=models.Severity(s["highest_severity"]),
                notes=s["notes"],
            )
            db.add(scan)
            db.flush()  # need scan.id for runs
            log.info("created draft scan %s for session %s", scan.id, rpt.session_id)
        else:
            log.info(
                "merging report %s into existing draft scan %s (session %s)",
                rpt.id, scan.id, rpt.session_id,
            )

        # Propagate the extracted product / scan_target onto the Run row so
        # the structured list view has something to show. Only if the user
        # hasn't already overridden it.
        if rpt.session_id:
            run_row = db.get(models.Run, rpt.session_id)
            if run_row is not None:
                if not run_row.product and scan.product:
                    run_row.product = scan.product
                if not run_row.subcomponent and scan.scan_target:
                    run_row.subcomponent = scan.scan_target

        _store_runs_and_findings(db, scan, result, rpt.user_id)

        db.commit()
        log.info(
            "extract for report %s: scan=%s (+%d runs, +%d findings)",
            rpt.id, scan.id,
            len(result.get("runs") or []),
            len(result.get("findings") or []),
        )

        # Per-finding enrichment pass: re-read the source markdown and
        # pull verbatim PoC blocks / references / CWE refs into each
        # newly-created finding's fields. Cheap to skip if there's
        # nothing to enrich.
        if scan.source_report_id:
            try:
                from .scans import _enrich_scan_findings
                # Re-fetch the scan with its fresh findings on the new session
                # state — we already committed, so finding_rows is current.
                _enrich_scan_findings(db, scan, only_thin=True)
            except Exception as e:
                log.warning("enrich after extract failed for scan %s: %s", scan.id, e)
    finally:
        db.close()


# ---------- read (called by humans) ----------
@router.get("", response_model=list[schemas.ReportOut])
def list_reports(
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
    user_id: str | None = None,
    source_tool: models.SourceTool | None = None,
    project_id: str | None = None,
    scan_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    q = db.query(models.Report).order_by(models.Report.created_at.desc())
    q = scope_reports(q, db, viewer)
    if user_id:
        q = q.filter(models.Report.user_id == user_id)
    if source_tool:
        q = q.filter(models.Report.source_tool == source_tool)
    if project_id:
        # Either directly attached to the project, OR inside a run that
        # belongs to the project (so the user sees everything "in" it).
        run_sessions = (
            db.query(models.Run.session_id)
            .filter(models.Run.project_id == project_id)
            .subquery()
        )
        q = q.filter(
            (models.Report.project_id == project_id)
            | (models.Report.session_id.in_(run_sessions))
        )
    if scan_id:
        # Report is "in" a scan if any of the three link paths match.
        scan = db.get(models.VulnScan, scan_id)
        if scan is None:
            return []
        clauses = [models.Report.scan_id == scan_id]  # manual link
        if scan.source_report_id:
            clauses.append(models.Report.id == scan.source_report_id)
        if scan.source_session_id:
            clauses.append(models.Report.session_id == scan.source_session_id)
        from sqlalchemy import or_
        q = q.filter(or_(*clauses))
    return [_to_out(r, db) for r in q.offset(offset).limit(min(limit, 500)).all()]


@router.get("/{report_id}", response_model=schemas.ReportDetail)
def get_report(
    report_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    rpt = db.get(models.Report, report_id)
    if not rpt:
        raise HTTPException(404, "Not found")
    assert_can_view_report(db, viewer, rpt)
    return schemas.ReportDetail(
        **_to_out(rpt, db).model_dump(),
        content=crypto.decrypt(rpt.content_enc).decode("utf-8", errors="replace"),
    )


@router.patch("/{report_id}", response_model=schemas.ReportOut)
def update_report(
    report_id: str,
    body: schemas.ReportUpdate,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Owner-or-admin update — title + project_id."""
    rpt = db.get(models.Report, report_id)
    if not rpt:
        raise HTTPException(404, "Not found")
    # owner / admin only
    if rpt.user_id != viewer.id and viewer.role != models.Role.admin:
        raise HTTPException(403, "Not allowed")
    payload = body.model_dump(exclude_unset=True)
    if "title" in payload:
        rpt.title = (payload["title"] or "").strip()
    if "scan_id" in payload:
        sid = payload["scan_id"]
        if sid in (None, ""):
            rpt.scan_id = None
        else:
            scan = db.get(models.VulnScan, sid)
            if not scan:
                raise HTTPException(400, "Scan not found")
            # Viewer must be able to edit the target scan (admin, owner, or
            # member of its project).
            from ..permissions import assert_can_edit_scan
            try:
                assert_can_edit_scan(db, viewer, scan)
            except HTTPException:
                raise HTTPException(403, "Not allowed to attach to that scan")
            rpt.scan_id = sid
    if "project_id" in payload:
        pid = payload["project_id"]
        if pid in (None, ""):
            rpt.project_id = None
        else:
            proj = db.get(models.Project, pid)
            if not proj:
                raise HTTPException(400, "Project not found")
            if viewer.role != models.Role.admin and proj.created_by != viewer.id \
                    and not any(m.id == viewer.id for m in proj.members):
                raise HTTPException(403, "You're not a member of that project")
            rpt.project_id = proj.id
    db.commit()
    db.refresh(rpt)
    return _to_out(rpt, db)


@router.delete("/{report_id}", status_code=204)
def delete_report(
    report_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    rpt = db.get(models.Report, report_id)
    if not rpt:
        return
    assert_can_delete(viewer, rpt.user_id, label="report")
    db.query(models.VulnScan).filter(
        models.VulnScan.source_report_id == rpt.id
    ).update({"source_report_id": None})
    db.delete(rpt)
    db.commit()


@router.get("/{report_id}/download")
def download_report(
    report_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    rpt = db.get(models.Report, report_id)
    if not rpt:
        raise HTTPException(404, "Not found")
    assert_can_view_report(db, viewer, rpt)
    # Re-use the same extension → mime mapping the UI download uses.
    from ..main import _mime_for
    return Response(
        content=crypto.decrypt(rpt.content_enc),
        media_type=_mime_for(rpt.filename),
        headers={"Content-Disposition": f'attachment; filename="{rpt.filename}"'},
    )


def _linked_scan(r: models.Report, db: Session) -> models.VulnScan | None:
    """Pick the scan this report is "in":
       1. manual Report.scan_id wins,
       2. scan whose source_report_id == report.id (auto-extracted from this file),
       3. scan whose source_session_id == report.session_id (multi-file run)."""
    if r.scan_id:
        s = db.get(models.VulnScan, r.scan_id)
        if s:
            return s
    s = (
        db.query(models.VulnScan)
        .filter(models.VulnScan.source_report_id == r.id)
        .first()
    )
    if s:
        return s
    if r.session_id:
        s = (
            db.query(models.VulnScan)
            .filter(models.VulnScan.source_session_id == r.session_id)
            .first()
        )
        if s:
            return s
    return None


def _to_out(r: models.Report, db: Session | None = None) -> schemas.ReportOut:
    # Lightweight derived-scan lookup so the SPA can show a "Review draft" CTA
    # without a second fetch.
    derived_scan_id = None
    derived_scan_product = None
    derived_scan_state = None
    effective_project_id = r.project_id

    if db is not None:
        scan = _linked_scan(r, db)
        if scan:
            derived_scan_id = scan.id
            derived_scan_product = scan.product or None
            derived_scan_state = scan.state.value if scan.state else None

        # If the report isn't directly attached to a project but its session is,
        # use the Run's project for the "effective" project (so UI grouping
        # follows the natural project chain: scan → run → project).
        if not effective_project_id and r.session_id:
            run = db.get(models.Run, r.session_id)
            if run and run.project_id:
                effective_project_id = run.project_id

    return schemas.ReportOut(
        id=r.id,
        user_id=r.user_id,
        filename=r.filename,
        title=r.title or "",
        original_path=r.original_path,
        source_tool=r.source_tool,
        sha256=r.sha256,
        size_bytes=r.size_bytes,
        summary=crypto.decrypt_str(r.summary_enc) if r.summary_enc else None,
        created_at=r.created_at,
        session_id=r.session_id,
        project_id=r.project_id,
        effective_project_id=effective_project_id,
        agent_hostname=r.agent.hostname if r.agent else None,
        owner_email=r.user.email if r.user else None,
        derived_scan_id=derived_scan_id,
        derived_scan_product=derived_scan_product,
        derived_scan_state=derived_scan_state,
    )
