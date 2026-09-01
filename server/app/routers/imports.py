"""Folder import: upload a directory, let Claude plan it, then confirm.

Flow:
  1. POST /imports             multipart with files keyed by relpath
                               -> creates FolderImport (status=staged)
  2. POST /imports/{id}/plan   -> spawns Claude tool-use loop on the
                                  staging dir; on success stores the plan
                                  JSON and flips status to "planned"
  3. POST /imports/{id}/confirm body={plan: {...}}
                               -> applies the (possibly user-edited) plan:
                                  creates Project, VulnScan, RunLog,
                                  Report, Attachment rows; cleans staging.
  4. DELETE /imports/{id}      -> cancels and wipes staging

Permission model: any logged-in user can create an import. Each import
is private to its creator (and admins).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import mimetypes
import os
import shutil
import tempfile
import zipfile
from typing import Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, UploadFile,
)
from sqlalchemy.orm import Session

from .. import models, schemas, crypto
from ..ai.import_planner import plan_folder
from ..auth import get_current_user
from ..config import settings
from ..database import get_db
from ..permissions import _visible_project_ids
from .reports import extract_title_from_markdown

log = logging.getLogger("irs.imports")
router = APIRouter(prefix="/imports", tags=["imports"])


# ---------------- helpers ----------------

def _staging_root(imp_id: str) -> str:
    return os.path.join(settings.imports_staging_dir, imp_id)


def _safe_rel(rel: str) -> str | None:
    """Return a normalized relpath or None if it tries to escape."""
    if not rel:
        return None
    # Normalize slashes + reject absolute paths / parent refs.
    rel = rel.replace("\\", "/").lstrip("/")
    parts = []
    for p in rel.split("/"):
        if not p or p == ".":
            continue
        if p == "..":
            return None
        parts.append(p)
    if not parts:
        return None
    return "/".join(parts)


def _read_tree(root: str) -> list[schemas.StagedFile]:
    out: list[schemas.StagedFile] = []
    if not os.path.isdir(root):
        return out
    for dirpath, _dn, filenames in os.walk(root):
        for f in filenames:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            out.append(schemas.StagedFile(
                relpath=rel,
                size=size,
                mime=mimetypes.guess_type(f)[0],
            ))
    out.sort(key=lambda x: x.relpath)
    return out


def _to_out(imp: models.FolderImport) -> schemas.FolderImportOut:
    out = schemas.FolderImportOut.model_validate(imp)
    if imp.project_id:
        # Resolve project name on the fly so the SPA can render
        # "Uploading to Foo" without a second fetch.
        from sqlalchemy.orm import object_session
        sess = object_session(imp)
        if sess is not None:
            p = sess.get(models.Project, imp.project_id)
            if p:
                out.project_name = p.name
    return out


def _load_owned(db: Session, viewer: models.User, imp_id: str) -> models.FolderImport:
    imp = db.get(models.FolderImport, imp_id)
    if not imp:
        raise HTTPException(404, "Not found")
    if imp.user_id != viewer.id and viewer.role != models.Role.admin:
        raise HTTPException(403, "Not allowed")
    return imp


def _wipe_staging(imp: models.FolderImport):
    if imp.staging_path and os.path.isdir(imp.staging_path):
        shutil.rmtree(imp.staging_path, ignore_errors=True)


def _is_zip(name: str | None, content_type: str | None) -> bool:
    if (name or "").lower().endswith(".zip"):
        return True
    return (content_type or "") in (
        "application/zip", "application/x-zip-compressed", "application/x-zip",
        "multipart/x-zip",
    )


async def _stage_zip(upload: UploadFile, staging_path: str,
                     written: int, total: int) -> tuple[int, int]:
    """Spool an uploaded .zip to a temp file, then extract its entries into the
    staging dir so source code can be uploaded as a single archive. Enforces
    the same file-count / total-byte limits as a plain folder upload, rejects
    entries that try to escape the staging dir (zip-slip), and skips common
    archive noise (__MACOSX, .DS_Store)."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        tmp_path = tmp.name
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            tmp.write(chunk)
    # Namespace each archive's contents under its own basename so uploading
    # several zips can't silently overwrite each other on a shared path.
    base = _safe_rel(os.path.splitext(os.path.basename(upload.filename or ""))[0]) or "archive"
    try:
        try:
            zf = zipfile.ZipFile(tmp_path)
        except zipfile.BadZipFile:
            raise HTTPException(400, f"{upload.filename!r} is not a valid zip file")
        with zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                inner = _safe_rel(info.filename)
                if inner is None:
                    raise HTTPException(400, f"unsafe path in zip: {info.filename!r}")
                if inner.startswith("__MACOSX/") or os.path.basename(inner) == ".DS_Store":
                    continue
                safe = f"{base}/{inner}"
                written += 1
                if written > settings.imports_max_files:
                    raise HTTPException(
                        413, f"too many files after unzip (max {settings.imports_max_files})")
                dest = os.path.join(staging_path, safe)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > settings.imports_max_total_bytes:
                            raise HTTPException(
                                413,
                                f"unzipped content exceeds {settings.imports_max_total_bytes} bytes")
                        out.write(chunk)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return written, total


# ---------------- list / create ----------------

@router.get("", response_model=list[schemas.FolderImportOut])
def list_imports(
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    q = db.query(models.FolderImport).order_by(models.FolderImport.created_at.desc())
    if viewer.role != models.Role.admin:
        q = q.filter(models.FolderImport.user_id == viewer.id)
    return [_to_out(i) for i in q.limit(50).all()]


@router.post("", response_model=schemas.FolderImportOut, status_code=201)
async def create_import(
    label: str = Form(""),
    project_id: str = Form(""),
    relpaths: list[str] = Form(...),
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Multipart upload of an entire folder.

    The SPA sends one `files` part per uploaded file plus a matching
    `relpaths` field per file (in the same order) so we can reconstruct
    the directory layout on disk.

    `project_id` (optional) pre-pins the upload to a product. The
    planner's project decision gets overridden to point at this product,
    so a user uploading from a product's page lands on a plan already
    targeting the right product. Caller must be a member of the product
    (or admin)."""
    if len(files) != len(relpaths):
        raise HTTPException(400, "files / relpaths length mismatch")
    if len(files) > settings.imports_max_files:
        raise HTTPException(413, f"too many files (max {settings.imports_max_files})")

    pinned_pid: str | None = None
    if project_id.strip():
        proj = db.get(models.Project, project_id.strip())
        if not proj:
            raise HTTPException(400, "Project not found")
        if viewer.role != models.Role.admin and proj.created_by != viewer.id \
                and not any(m.id == viewer.id for m in proj.members):
            raise HTTPException(403, "Not a member of that product")
        pinned_pid = proj.id

    imp = models.FolderImport(
        user_id=viewer.id,
        status=models.ImportStatus.staged,
        label=(label or "").strip()[:255],
        project_id=pinned_pid,
        staging_path="",  # filled after we know the id
    )
    db.add(imp)
    db.flush()
    imp.staging_path = _staging_root(imp.id)
    os.makedirs(imp.staging_path, exist_ok=True)

    total = 0
    written = 0
    try:
        for upload, rel in zip(files, relpaths):
            # A .zip is unpacked into the staging tree rather than stored as-is,
            # so users can upload a whole source-code archive in one shot.
            if _is_zip(upload.filename or rel, upload.content_type):
                written, total = await _stage_zip(upload, imp.staging_path, written, total)
                continue
            safe = _safe_rel(rel)
            if safe is None:
                raise HTTPException(400, f"invalid relpath {rel!r}")
            dest = os.path.join(imp.staging_path, safe)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            # Stream to disk so a large upload doesn't sit in RAM.
            with open(dest, "wb") as out:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > settings.imports_max_total_bytes:
                        raise HTTPException(
                            413,
                            f"upload exceeds {settings.imports_max_total_bytes} bytes",
                        )
                    out.write(chunk)
            written += 1
    except Exception:
        _wipe_staging(imp)
        db.rollback()
        raise

    imp.file_count = written
    imp.total_bytes = total
    db.commit()
    db.refresh(imp)
    log.info("staged import id=%s user=%s files=%d bytes=%d",
             imp.id, viewer.id, written, total)
    return _to_out(imp)


# ---------------- detail ----------------

@router.get("/{imp_id}", response_model=schemas.FolderImportDetail)
def get_import(
    imp_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    imp = _load_owned(db, viewer, imp_id)
    files = _read_tree(imp.staging_path) if imp.status != models.ImportStatus.applied else []
    return schemas.FolderImportDetail(
        **_to_out(imp).model_dump(),
        files=files,
        plan=imp.plan_json,
        plan_log=imp.plan_log,
    )


# ---------------- plan ----------------

@router.post("/{imp_id}/plan", response_model=schemas.FolderImportDetail)
def run_planner(
    imp_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    imp = _load_owned(db, viewer, imp_id)
    if imp.status not in (models.ImportStatus.staged, models.ImportStatus.planned,
                          models.ImportStatus.error):
        raise HTTPException(409, f"can't plan from status={imp.status.value!r}")

    if viewer.role == models.Role.admin:
        proj_rows = db.query(models.Project).order_by(models.Project.name).all()
    else:
        proj_rows = sorted(viewer.projects, key=lambda p: p.name.lower())
    existing_projects = [
        {"id": p.id, "name": p.name, "description": p.description}
        for p in proj_rows
    ]

    imp.status = models.ImportStatus.planning
    db.commit()

    try:
        plan, log_text = plan_folder(
            imp.staging_path,
            existing_projects=existing_projects,
            user_label=imp.label,
        )
    except Exception as e:
        log.exception("planner failed for import %s", imp.id)
        imp.status = models.ImportStatus.error
        imp.error_message = str(e)[:1000]
        db.commit()
        raise HTTPException(500, f"planner failed: {e}")

    # If the upload was pre-pinned to a product (created from the product
    # page), force the plan's project to that product so the user lands
    # on a plan that's already targeting the right place.
    if imp.project_id:
        existing_rationale = (plan.get("project") or {}).get("rationale", "")
        plan["project"] = {
            "kind": "existing",
            "existing_id": imp.project_id,
            "rationale": (
                existing_rationale + " | Pre-pinned at upload time."
            ).strip(" |"),
        }

    imp.plan_json = plan
    imp.plan_log = log_text
    imp.status = models.ImportStatus.planned
    imp.planned_at = dt.datetime.now(dt.timezone.utc)
    imp.error_message = ""
    db.commit()
    db.refresh(imp)
    return schemas.FolderImportDetail(
        **_to_out(imp).model_dump(),
        files=_read_tree(imp.staging_path),
        plan=imp.plan_json,
        plan_log=imp.plan_log,
    )


# ---------------- confirm (apply) ----------------

@router.post("/{imp_id}/confirm", response_model=schemas.FolderImportOut)
def confirm_import(
    imp_id: str,
    body: schemas.FolderImportConfirm,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    imp = _load_owned(db, viewer, imp_id)
    if imp.status not in (models.ImportStatus.planned, models.ImportStatus.error):
        raise HTTPException(409, f"can't confirm from status={imp.status.value!r}")

    plan = body.plan or imp.plan_json or {}
    if not plan:
        raise HTTPException(400, "no plan to apply")
    if not isinstance(plan.get("items"), list):
        raise HTTPException(400, "plan.items must be a list")

    project_id = _resolve_project(db, viewer, plan.get("project") or {"kind": "none"})
    apply_plan(db, viewer, imp, plan, project_id)
    db.commit()
    db.refresh(imp)
    _wipe_staging(imp)
    return _to_out(imp)


def apply_plan(
    db: Session,
    importer: models.User,
    imp: models.FolderImport,
    plan: dict,
    project_id: str | None,
) -> None:
    """Apply a (validated) plan dict against the staged folder.

    Caller is expected to commit + wipe staging afterwards. Extracted from
    the confirm endpoint so the access-request approval path can apply an
    import on the project owner's behalf (with project_id forced to the
    project being approved).
    """
    # Pre-pass: create scans + index by local_id
    scans_by_local: dict[str, models.VulnScan] = {}
    for s in (plan.get("scans") or []):
        local_id = s.get("local_id")
        if not local_id:
            continue
        scan = models.VulnScan(
            user_id=importer.id,
            project_id=project_id,
            state=models.ScanState.draft,
            product=(s.get("product") or "")[:255],
            scan_target=(s.get("scan_target") or "")[:512],
            harness_used=(s.get("harness_used") or "")[:255],
            scan_by=(s.get("scan_by") or "")[:255],
            notes=s.get("notes") or "",
        )
        db.add(scan)
        db.flush()
        scans_by_local[local_id] = scan

    reports_by_local: dict[str, models.Report] = {}
    reports_by_dir: dict[str, models.Report] = {}

    for item in plan["items"]:
        if item.get("kind") != "report":
            continue
        rel = item["relpath"]
        rpt = _import_report(db, importer, imp, item, project_id, scans_by_local)
        if rpt is None:
            continue
        if item.get("local_id"):
            reports_by_local[item["local_id"]] = rpt
        reports_by_dir[os.path.dirname(rel)] = rpt

    for item in plan["items"]:
        if item.get("kind") != "poc":
            continue
        _import_attachment(db, importer, imp, item, scans_by_local,
                            reports_by_local, reports_by_dir)

    for r in (plan.get("runs") or []):
        scan = scans_by_local.get(r.get("scan_local_id"))
        if not scan:
            continue
        date_val = None
        if r.get("date"):
            try:
                date_val = dt.date.fromisoformat(r["date"])
            except (ValueError, TypeError):
                pass
        db.add(models.RunLog(
            scan_id=scan.id, user_id=importer.id,
            day=r.get("day", ""), date=date_val,
            run=r.get("run", ""), box=r.get("box", ""),
            product=r.get("product", ""), harness=r.get("harness", ""),
            prompt=r.get("prompt", ""), results=r.get("results", ""),
            poc=r.get("poc", ""), comment=r.get("comment", ""),
            complete=bool(r.get("complete", False)),
        ))

    imp.status = models.ImportStatus.applied
    imp.applied_at = dt.datetime.now(dt.timezone.utc)


def force_plan_project(plan: dict, project_id: str) -> dict:
    """Return a shallow-copied plan whose project block is rewritten to
    target the given existing project_id. Used by the access-request
    approval flow so the owner doesn't have to chase the importer for a
    project assignment."""
    out = dict(plan or {})
    out["project"] = {
        "kind": "existing",
        "existing_id": project_id,
        "rationale": (
            (plan.get("project") or {}).get("rationale", "")
            + " | Overridden by access-request approval."
        ).strip(" |"),
    }
    return out


def wipe_staging(imp: models.FolderImport):
    _wipe_staging(imp)


def _resolve_project(
    db: Session, viewer: models.User, project_plan: dict,
) -> str | None:
    """Turn the plan's project block into a project_id or None."""
    kind = project_plan.get("kind") or "none"
    if kind == "none":
        return None
    if kind == "existing":
        pid = project_plan.get("existing_id")
        if not pid:
            raise HTTPException(400, "project.kind=existing requires existing_id")
        proj = db.get(models.Project, pid)
        if not proj:
            raise HTTPException(400, f"project {pid!r} not found")
        # User must be a member or admin to attach to it.
        if viewer.role != models.Role.admin:
            if proj.created_by != viewer.id and not any(
                m.id == viewer.id for m in proj.members
            ):
                raise HTTPException(403, "not a member of that project")
        return proj.id
    if kind == "new":
        name = (project_plan.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "project.kind=new requires name")
        proj = models.Project(
            name=name[:255],
            description=(project_plan.get("description") or "")[:2000],
            created_by=viewer.id,
        )
        db.add(proj)
        db.flush()
        # Make creator a member so the project shows up in their list.
        db.add(models.ProjectMember(project_id=proj.id, user_id=viewer.id))
        db.flush()
        return proj.id
    raise HTTPException(400, f"unknown project.kind {kind!r}")


def _read_staged(imp: models.FolderImport, relpath: str) -> bytes:
    base = os.path.abspath(imp.staging_path)
    target = os.path.abspath(os.path.join(base, relpath))
    if not target.startswith(base + os.sep) and target != base:
        raise HTTPException(400, f"path escape attempt: {relpath!r}")
    if not os.path.isfile(target):
        raise HTTPException(400, f"staged file missing: {relpath!r}")
    with open(target, "rb") as fh:
        return fh.read()


def _import_report(
    db: Session, viewer: models.User, imp: models.FolderImport,
    item: dict, project_id: str | None,
    scans_by_local: dict[str, models.VulnScan],
) -> Optional[models.Report]:
    rel = item["relpath"]
    raw = _read_staged(imp, rel)
    sha = hashlib.sha256(raw).hexdigest()

    existing = (
        db.query(models.Report)
        .filter(models.Report.user_id == viewer.id, models.Report.sha256 == sha)
        .first()
    )
    if existing:
        # Still allow scan_id reassignment for an idempotent re-import.
        scan = scans_by_local.get(item.get("scan_local_id") or "")
        if scan and not existing.scan_id:
            existing.scan_id = scan.id
        if project_id and not existing.project_id:
            existing.project_id = project_id
        return existing

    text = raw.decode("utf-8", errors="replace")
    title = (item.get("title") or "").strip()
    if not title:
        title = extract_title_from_markdown(text)
    scan = scans_by_local.get(item.get("scan_local_id") or "")

    rpt = models.Report(
        user_id=viewer.id,
        source_tool=models.SourceTool.other,
        filename=os.path.basename(rel),
        title=title[:255],
        original_path=rel,
        sha256=sha,
        size_bytes=len(raw),
        content_enc=crypto.encrypt(raw),
        project_id=project_id,
        scan_id=scan.id if scan else None,
    )
    db.add(rpt)
    db.flush()
    return rpt


def _import_attachment(
    db: Session, viewer: models.User, imp: models.FolderImport,
    item: dict,
    scans_by_local: dict[str, models.VulnScan],
    reports_by_local: dict[str, models.Report],
    reports_by_dir: dict[str, models.Report],
) -> Optional[models.Attachment]:
    rel = item["relpath"]
    raw = _read_staged(imp, rel)
    sha = hashlib.sha256(raw).hexdigest()
    filename = os.path.basename(rel)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    # Pick a target report, in priority order:
    target_report = None
    if item.get("attach_to_local_id"):
        target_report = reports_by_local.get(item["attach_to_local_id"])
    if not target_report:
        # Try directory-locality: a poc/ file next to a report.
        d = os.path.dirname(rel)
        while d:
            if d in reports_by_dir:
                target_report = reports_by_dir[d]
                break
            d = os.path.dirname(d)

    scan = None
    if item.get("scan_local_id"):
        scan = scans_by_local.get(item["scan_local_id"])
    if scan is None and target_report is not None:
        scan = db.get(models.VulnScan, target_report.scan_id) if target_report.scan_id else None

    existing = (
        db.query(models.Attachment)
        .filter(
            models.Attachment.user_id == viewer.id,
            models.Attachment.sha256 == sha,
            models.Attachment.filename == filename,
        )
        .first()
    )
    if existing:
        if scan and not existing.scan_id:
            existing.scan_id = scan.id
        return existing

    a = models.Attachment(
        user_id=viewer.id,
        session_id=None,
        scan_id=scan.id if scan else None,
        filename=filename,
        original_path=rel,
        content_type=content_type,
        sha256=sha,
        size_bytes=len(raw),
        content_enc=crypto.encrypt(raw),
    )
    db.add(a)
    db.flush()
    return a


# ---------------- cancel ----------------

@router.delete("/{imp_id}", status_code=204)
def cancel_import(
    imp_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    imp = _load_owned(db, viewer, imp_id)
    _wipe_staging(imp)
    if imp.status != models.ImportStatus.applied:
        imp.status = models.ImportStatus.cancelled
        db.commit()
    return None
