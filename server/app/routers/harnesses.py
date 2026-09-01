"""Harness uploads + management.

A "harness" is a folder of prompts/config/tooling that the user wants
Claude to run inside. Reusable across many Claude sessions/runs. Files
are stored per-row in `harness_files`, encrypted at rest with the same
AES-GCM key as reports/attachments.

Endpoints:

  POST   /harnesses                multipart upload (relpaths + files)
  GET    /harnesses                list visible
  GET    /harnesses/{id}           detail + file tree
  PATCH  /harnesses/{id}           rename / change project / change description
  GET    /harnesses/{id}/files/raw?relpath=...  stream one file
  DELETE /harnesses/{id}           remove harness + cascades files

Visibility:
  * owner sees their own
  * project members see harnesses pinned to their products
  * admins see everything
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
from typing import Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile,
)
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, schemas, crypto
from ..auth import get_current_user
from ..config import settings
from ..database import get_db
from ..permissions import _visible_project_ids, assert_can_delete

log = logging.getLogger("irs.harnesses")
router = APIRouter(prefix="/harnesses", tags=["harnesses"])


# Per-harness caps. Smaller than imports — these are config bundles, not
# bulk artefact dumps. Override per-deployment with env vars if needed.
MAX_TOTAL_BYTES = 50 * 1024 * 1024     # 50 MiB
MAX_FILES = 500
MAX_SINGLE_FILE = 10 * 1024 * 1024     # 10 MiB


def _safe_rel(rel: str) -> str | None:
    if not rel:
        return None
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


def _visible(viewer: models.User, h: models.Harness) -> bool:
    if viewer.role == models.Role.admin:
        return True
    if h.user_id == viewer.id:
        return True
    if h.project_id and h.project_id in _visible_project_ids(viewer):
        return True
    return False


def _can_edit(viewer: models.User, h: models.Harness) -> bool:
    return viewer.role == models.Role.admin or h.user_id == viewer.id


def _to_out(h: models.Harness) -> schemas.HarnessOut:
    o = schemas.HarnessOut.model_validate(h)
    if h.project:
        o.project_name = h.project.name
    return o


# ---------------- list / create ----------------

@router.get("", response_model=list[schemas.HarnessOut])
def list_harnesses(
    project_id: str | None = None,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    q = db.query(models.Harness).order_by(models.Harness.updated_at.desc())
    if project_id:
        q = q.filter(models.Harness.project_id == project_id)
    rows = q.all()
    return [_to_out(h) for h in rows if _visible(viewer, h)]


@router.post("", response_model=schemas.HarnessOut, status_code=201)
async def create_harness(
    name: str = Form(...),
    description: str = Form(""),
    project_id: str = Form(""),
    relpaths: list[str] = Form(...),
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    if not name.strip():
        raise HTTPException(400, "name is required")
    if len(files) != len(relpaths):
        raise HTTPException(400, "files / relpaths length mismatch")
    if len(files) > MAX_FILES:
        raise HTTPException(413, f"too many files (max {MAX_FILES})")

    pid: str | None = None
    if project_id.strip():
        proj = db.get(models.Project, project_id.strip())
        if not proj:
            raise HTTPException(400, "project not found")
        if viewer.role != models.Role.admin and proj.created_by != viewer.id \
                and not any(m.id == viewer.id for m in proj.members):
            raise HTTPException(403, "not a member of that project")
        pid = proj.id

    h = models.Harness(
        user_id=viewer.id,
        project_id=pid,
        name=name.strip()[:255],
        description=description.strip()[:2000],
    )
    db.add(h)
    db.flush()  # need h.id for files

    total = 0
    written = 0
    seen_rel: set[str] = set()
    try:
        for upload, rel in zip(files, relpaths):
            safe = _safe_rel(rel)
            if safe is None:
                raise HTTPException(400, f"invalid relpath {rel!r}")
            if safe in seen_rel:
                raise HTTPException(400, f"duplicate relpath {safe!r}")
            seen_rel.add(safe)

            raw = await upload.read()
            if len(raw) > MAX_SINGLE_FILE:
                raise HTTPException(413, f"{safe!r} exceeds {MAX_SINGLE_FILE} bytes")
            total += len(raw)
            if total > MAX_TOTAL_BYTES:
                raise HTTPException(413, f"upload exceeds {MAX_TOTAL_BYTES} bytes")
            ct = upload.content_type or mimetypes.guess_type(safe)[0] or "application/octet-stream"

            db.add(models.HarnessFile(
                harness_id=h.id,
                relpath=safe,
                content_type=ct,
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                content_enc=crypto.encrypt(raw),
            ))
            written += 1
    except Exception:
        db.rollback()
        raise

    h.file_count = written
    h.total_bytes = total
    db.commit()
    db.refresh(h)
    log.info("created harness id=%s user=%s files=%d bytes=%d",
             h.id, viewer.id, written, total)
    return _to_out(h)


# ---------------- detail / patch / delete ----------------

@router.get("/{harness_id}", response_model=schemas.HarnessDetail)
def get_harness(
    harness_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    h = db.get(models.Harness, harness_id)
    if not h:
        raise HTTPException(404, "Not found")
    if not _visible(viewer, h):
        raise HTTPException(403, "Not allowed")
    return schemas.HarnessDetail(
        **_to_out(h).model_dump(),
        files=[schemas.HarnessFileOut.model_validate(f) for f in h.files],
    )


@router.patch("/{harness_id}", response_model=schemas.HarnessOut)
def patch_harness(
    harness_id: str,
    body: schemas.HarnessUpdate,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    h = db.get(models.Harness, harness_id)
    if not h:
        raise HTTPException(404, "Not found")
    if not _can_edit(viewer, h):
        raise HTTPException(403, "Only the owner or an admin can edit")
    payload = body.model_dump(exclude_unset=True)
    if "name" in payload and payload["name"] is not None:
        h.name = payload["name"].strip()[:255]
    if "description" in payload and payload["description"] is not None:
        h.description = payload["description"].strip()[:2000]
    if "project_id" in payload:
        pid = payload["project_id"]
        if pid in (None, ""):
            h.project_id = None
        else:
            proj = db.get(models.Project, pid)
            if not proj:
                raise HTTPException(400, "project not found")
            if viewer.role != models.Role.admin and proj.created_by != viewer.id \
                    and not any(m.id == viewer.id for m in proj.members):
                raise HTTPException(403, "not a member of that project")
            h.project_id = proj.id
    db.commit()
    db.refresh(h)
    return _to_out(h)


@router.delete("/{harness_id}", status_code=204)
def delete_harness(
    harness_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    h = db.get(models.Harness, harness_id)
    if not h:
        return None
    assert_can_delete(viewer, h.user_id, label="harness")
    # Detach any runs pointing at this harness before delete so the FK
    # SET NULL fires cleanly (cascade on files via relationship).
    db.query(models.Run).filter(models.Run.harness_id == h.id).update({"harness_id": None})
    db.delete(h)
    db.commit()


# ---------------- file content ----------------

@router.get("/{harness_id}/files/raw")
def download_harness_file(
    harness_id: str,
    relpath: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    h = db.get(models.Harness, harness_id)
    if not h:
        raise HTTPException(404, "Not found")
    if not _visible(viewer, h):
        raise HTTPException(403, "Not allowed")
    safe = _safe_rel(relpath)
    if safe is None:
        raise HTTPException(400, "invalid relpath")
    f = (
        db.query(models.HarnessFile)
        .filter(
            models.HarnessFile.harness_id == h.id,
            models.HarnessFile.relpath == safe,
        )
        .first()
    )
    if not f:
        raise HTTPException(404, "file not in harness")
    body = crypto.decrypt(f.content_enc)
    return Response(
        content=body,
        media_type=f.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{os.path.basename(safe)}"',
        },
    )


def _recount(db: Session, h: models.Harness) -> None:
    n, b = (db.query(func.count(models.HarnessFile.id),
                     func.coalesce(func.sum(models.HarnessFile.size_bytes), 0))
              .filter(models.HarnessFile.harness_id == h.id).one())
    h.file_count = int(n)
    h.total_bytes = int(b)


@router.put("/{harness_id}/files", response_model=schemas.HarnessFileOut)
def upsert_harness_file(
    harness_id: str,
    body: schemas.HarnessFileEdit,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Create or overwrite one text file in the harness. Owner/admin only."""
    h = db.get(models.Harness, harness_id)
    if not h:
        raise HTTPException(404, "Not found")
    if not _can_edit(viewer, h):
        raise HTTPException(403, "Only the owner or an admin can edit")
    safe = _safe_rel(body.relpath)
    if safe is None:
        raise HTTPException(400, "invalid relpath")
    raw = body.content.encode("utf-8")
    if len(raw) > MAX_SINGLE_FILE:
        raise HTTPException(413, f"file exceeds {MAX_SINGLE_FILE} bytes")
    ct = mimetypes.guess_type(safe)[0] or "text/plain"
    f = (db.query(models.HarnessFile)
           .filter(models.HarnessFile.harness_id == h.id,
                   models.HarnessFile.relpath == safe).first())
    if f:
        if h.total_bytes - f.size_bytes + len(raw) > MAX_TOTAL_BYTES:
            raise HTTPException(413, f"harness exceeds {MAX_TOTAL_BYTES} bytes")
        f.content_enc = crypto.encrypt(raw)
        f.sha256 = hashlib.sha256(raw).hexdigest()
        f.size_bytes = len(raw)
        f.content_type = ct
    else:
        if h.file_count + 1 > MAX_FILES:
            raise HTTPException(413, f"too many files (max {MAX_FILES})")
        if h.total_bytes + len(raw) > MAX_TOTAL_BYTES:
            raise HTTPException(413, f"harness exceeds {MAX_TOTAL_BYTES} bytes")
        f = models.HarnessFile(
            harness_id=h.id, relpath=safe, content_type=ct,
            sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw),
            content_enc=crypto.encrypt(raw),
        )
        db.add(f)
    db.flush()
    _recount(db, h)
    db.commit()
    db.refresh(f)
    return schemas.HarnessFileOut.model_validate(f)


@router.delete("/{harness_id}/files", status_code=204)
def delete_harness_file(
    harness_id: str,
    relpath: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    h = db.get(models.Harness, harness_id)
    if not h:
        raise HTTPException(404, "Not found")
    if not _can_edit(viewer, h):
        raise HTTPException(403, "Only the owner or an admin can edit")
    safe = _safe_rel(relpath)
    if safe is None:
        raise HTTPException(400, "invalid relpath")
    f = (db.query(models.HarnessFile)
           .filter(models.HarnessFile.harness_id == h.id,
                   models.HarnessFile.relpath == safe).first())
    if not f:
        raise HTTPException(404, "file not in harness")
    db.delete(f)
    db.flush()
    _recount(db, h)
    db.commit()
