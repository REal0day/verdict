"""Investigations pipeline — Cases and StageRuns (foundation, no execution).

A Case is the multi-finding container for one investigation. It wraps a
VulnScan (so findings + their RBAC are reused) and adds pipeline state: the
inbound bug report, a source reference, a case-wide default model, and
per-Finding StageRuns.

This layer lets you create a case, attach source, queue stage runs, and see
which model each stage would use. Nothing runs yet — stage runs sit `pending`
until the executor (a later step) drains them.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crypto, localsource, models, schemas
from ..ai import scope
from ..auth import get_current_user
from ..database import get_db
from ..permissions import (
    assert_can_edit_case,
    assert_can_edit_scan,
    assert_can_view_case,
    can_delete,
    scope_cases,
)

log = logging.getLogger("irs.cases")
router = APIRouter(prefix="/cases", tags=["cases"])

# Stages that operate on a single finding; the report stage is case-level.
_FINDING_STAGES = {
    models.StageType.impact, models.StageType.poc,
    models.StageType.source, models.StageType.remediation,
}


# ---------------- enum coercion ----------------

def _enum(raw: str, enum_cls, field: str):
    try:
        return enum_cls(raw)
    except ValueError:
        allowed = ", ".join(e.value for e in enum_cls)
        raise HTTPException(400, f"invalid {field} {raw!r}; expected one of: {allowed}")


# ---------------- serialisation ----------------

def _resolved(db: Session, run: models.StageRun) -> schemas.ResolvedModel:
    c = scope.for_stage(db, run.id)
    return schemas.ResolvedModel(provider=c.provider, model=c.model, source=c.source)


def _stage_out(db: Session, run: models.StageRun) -> schemas.StageRunOut:
    out = schemas.StageRunOut.model_validate(run)
    out.finding_title = run.finding.title if run.finding else None
    out.has_output = run.output_enc is not None
    out.resolved = _resolved(db, run)
    return out


def _case_out(db: Session, case: models.Case) -> schemas.CaseOut:
    out = schemas.CaseOut.model_validate(case)
    out.project_name = case.project.name if case.project else None
    findings = case.scan.finding_rows if case.scan else []
    out.finding_count = len(findings)
    out.stage_run_count = len(case.stage_runs)
    out.pending_stage_count = sum(
        1 for r in case.stage_runs if r.status == models.StageRunStatus.pending
    )
    out.has_report = any(
        r.stage == models.StageType.report and r.output_enc is not None
        for r in case.stage_runs
    )
    return out


def _case_detail(db: Session, case: models.Case) -> schemas.CaseDetail:
    base = _case_out(db, case)
    detail = schemas.CaseDetail(**base.model_dump())
    detail.report_text = (
        crypto.decrypt(case.report_enc).decode("utf-8", "replace")
        if case.report_enc else None
    )
    detail.source = schemas.CaseSourceOut.model_validate(case.source) if case.source else None
    detail.findings = [
        schemas.FindingOut.model_validate(f)
        for f in (case.scan.finding_rows if case.scan else [])
    ]
    detail.stage_runs = [
        _stage_out(db, r)
        for r in sorted(case.stage_runs, key=lambda r: r.created_at)
    ]
    return detail


def _apply_source(db: Session, case: models.Case, body: schemas.CaseSourceIn):
    kind = _enum(body.kind, models.CaseSourceKind, "source kind")
    src = case.source or models.CaseSource(case_id=case.id)
    src.kind = kind
    src.local_path = (body.local_path or "").strip().lstrip("/")
    src.remote_url = (body.remote_url or "").strip()
    src.ref = (body.ref or "").strip()
    src.credential_id = body.credential_id
    src.detail = ""

    if kind == models.CaseSourceKind.local_path:
        # Option 1: a subdir under SOURCE_ROOT, resolved + confined now.
        try:
            resolved = localsource.resolve(src.local_path)
            src.status = models.SourceStatus.ready
            src.workspace_key = str(resolved)
        except localsource.SourceError as e:
            raise HTTPException(400, str(e))
    elif kind == models.CaseSourceKind.upload:
        # Option 3 (fallback): the source is the case's project's uploaded
        # files. Ready once the project actually has some.
        n = 0
        if case.project_id:
            n = db.query(models.ProjectFile).filter(
                models.ProjectFile.project_id == case.project_id).count()
        src.status = models.SourceStatus.ready if n else models.SourceStatus.pending
        src.detail = f"{n} uploaded file(s)" if n else "no files uploaded yet"
    elif kind in (models.CaseSourceKind.remote_git, models.CaseSourceKind.remote_archive):
        # Remote fetch is a future feature; accept the ref but leave it pending.
        src.status = models.SourceStatus.pending
        src.detail = "remote fetch is not implemented yet"
    else:
        src.status = models.SourceStatus.none
    case.source = src


# ---------------- local source browsing (SOURCE_ROOT picker) ----------------

@router.get("/source/browse")
def browse_source(
    path: str = "",
    _: models.User = Depends(get_current_user),
):
    """List subdirectories under SOURCE_ROOT so the wizard can pick a subdir.

    `available: false` means SOURCE_ROOT isn't configured — the UI should offer
    upload instead. Any signed-in user can browse (it's the operator's own
    mounted tree).
    """
    try:
        return localsource.browse(path)
    except localsource.SourceError as e:
        raise HTTPException(400, str(e))


# ---------------- case CRUD ----------------

@router.post("", response_model=schemas.CaseDetail, status_code=201)
def create_case(
    body: schemas.CaseCreate,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    # Resolve or create the backing scan.
    if body.scan_id:
        scan = db.get(models.VulnScan, body.scan_id)
        if not scan:
            raise HTTPException(404, "scan not found")
        assert_can_edit_scan(db, viewer, scan)
    else:
        proj = db.get(models.Project, body.project_id) if body.project_id else None
        scan = models.VulnScan(
            user_id=viewer.id,
            project_id=body.project_id,
            product=proj.name if proj else "",
            state=models.ScanState.draft,
        )
        db.add(scan)
        db.flush()

    case = models.Case(
        user_id=viewer.id,
        project_id=body.project_id,
        scan_id=scan.id,
        title=body.title.strip(),
        status=models.CaseStatus.intake,
        ai_provider=(body.ai_provider or None),
        ai_model=(body.ai_model or None),
        poc_auto_execute=body.poc_auto_execute,
        report_enc=crypto.encrypt(body.report_text.encode("utf-8")) if body.report_text else None,
    )
    db.add(case)
    db.flush()
    if body.source:
        _apply_source(db, case, body.source)
    db.commit()
    db.refresh(case)
    return _case_detail(db, case)


@router.get("", response_model=list[schemas.CaseOut])
def list_cases(
    project_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    q = scope_cases(db.query(models.Case), db, viewer)
    if project_id:
        q = q.filter(models.Case.project_id == project_id)
    if status:
        q = q.filter(models.Case.status == _enum(status, models.CaseStatus, "status"))
    q = q.order_by(models.Case.created_at.desc()).limit(min(limit, 500)).offset(offset)
    return [_case_out(db, c) for c in q.all()]


@router.get("/{case_id}", response_model=schemas.CaseDetail)
def get_case(
    case_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    case = db.get(models.Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    assert_can_view_case(db, viewer, case)
    return _case_detail(db, case)


@router.patch("/{case_id}", response_model=schemas.CaseDetail)
def update_case(
    case_id: str,
    body: schemas.CaseUpdate,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    case = db.get(models.Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    assert_can_edit_case(db, viewer, case)
    data = body.model_dump(exclude_unset=True)
    if "title" in data and data["title"] is not None:
        case.title = data["title"].strip()[:255]
    if "status" in data and data["status"] is not None:
        case.status = _enum(data["status"], models.CaseStatus, "status")
    if "project_id" in data:
        case.project_id = data["project_id"]
    if "ai_provider" in data:
        case.ai_provider = (data["ai_provider"] or "").strip() or None
    if "ai_model" in data:
        case.ai_model = (data["ai_model"] or "").strip() or None
    if "poc_auto_execute" in data and data["poc_auto_execute"] is not None:
        case.poc_auto_execute = bool(data["poc_auto_execute"])
    if "report_text" in data:
        case.report_enc = (
            crypto.encrypt(data["report_text"].encode("utf-8"))
            if data["report_text"] else None
        )
    db.commit()
    db.refresh(case)
    return _case_detail(db, case)


@router.delete("/{case_id}", status_code=204)
def delete_case(
    case_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    case = db.get(models.Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    if not can_delete(viewer, case.user_id):
        raise HTTPException(403, "Not allowed")
    db.delete(case)   # cascades to source + stage_runs; the scan is left intact
    db.commit()


@router.put("/{case_id}/source", response_model=schemas.CaseDetail)
def set_source(
    case_id: str,
    body: schemas.CaseSourceIn,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    case = db.get(models.Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    assert_can_edit_case(db, viewer, case)
    _apply_source(db, case, body)
    db.commit()
    db.refresh(case)
    return _case_detail(db, case)


# ---------------- stage runs ----------------

@router.post("/{case_id}/stages", response_model=schemas.StageRunOut, status_code=201)
def queue_stage(
    case_id: str,
    body: schemas.StageRunCreate,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    case = db.get(models.Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    assert_can_edit_case(db, viewer, case)

    stage = _enum(body.stage, models.StageType, "stage")
    finding_id = body.finding_id
    if stage in _FINDING_STAGES:
        if not finding_id:
            raise HTTPException(400, f"the {stage.value} stage needs a finding_id")
        f = db.get(models.Finding, finding_id)
        if not f or (case.scan_id and f.scan_id != case.scan_id):
            raise HTTPException(400, "finding_id is not part of this case")
    else:
        finding_id = None  # report stage is case-level

    run = models.StageRun(
        case_id=case.id,
        finding_id=finding_id,
        stage=stage,
        status=models.StageRunStatus.pending,
        ai_provider=(body.ai_provider or None),
        ai_model=(body.ai_model or None),
    )
    db.add(run)
    # First queued work moves the case out of intake.
    if case.status == models.CaseStatus.intake:
        case.status = models.CaseStatus.active
    db.commit()
    db.refresh(run)
    return _stage_out(db, run)


@router.get("/{case_id}/stages", response_model=list[schemas.StageRunOut])
def list_stages(
    case_id: str,
    finding_id: str | None = None,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    case = db.get(models.Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    assert_can_view_case(db, viewer, case)
    runs = sorted(case.stage_runs, key=lambda r: r.created_at)
    if finding_id:
        runs = [r for r in runs if r.finding_id == finding_id]
    return [_stage_out(db, r) for r in runs]


@router.get("/{case_id}/stages/{run_id}/output")
def stage_output(
    case_id: str,
    run_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    case = db.get(models.Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    assert_can_view_case(db, viewer, case)
    run = db.get(models.StageRun, run_id)
    if not run or run.case_id != case.id:
        raise HTTPException(404, "stage run not found")
    text = crypto.decrypt(run.output_enc).decode("utf-8", "replace") if run.output_enc else ""
    return {"id": run.id, "stage": run.stage.value, "status": run.status.value, "output": text}
