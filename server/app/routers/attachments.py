"""Endpoints for POC / artifact files collected by the agent.

Same shape as the Reports ingest path, but for non-markdown binaries (crash
inputs, scripts, screenshots, tarballs of poc/ dirs, etc.). Encrypted at rest
with the same AES-GCM key.

  POST /attachments/ingest          agent ships a file (Bearer X-Agent-Key)
  GET  /attachments?session_id=...  list (JWT)
  GET  /attachments/{id}/download   stream decrypted bytes (cookie OR JWT)
"""
from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import models, schemas, crypto
from ..auth import get_current_agent, get_current_user
from ..config import settings
from ..database import get_db
from ..permissions import assert_can_view_report, scope_reports

templates = Jinja2Templates(directory="app/templates")

# Cap inline text preview at 1 MiB so a huge log doesn't blow up the page.
_PREVIEW_MAX = 1 * 1024 * 1024

# Heuristics for "this is text we can render in a <pre>"
_TEXT_PREFIXES = ("text/", "application/json", "application/xml")
_TEXT_EXTS = {
    ".txt", ".md", ".log", ".json", ".xml", ".yml", ".yaml", ".csv",
    ".py", ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".java", ".js",
    ".ts", ".tsx", ".jsx", ".sh", ".rb", ".php", ".html", ".css",
    ".sql", ".diff", ".patch", ".ini", ".toml", ".conf",
}


def _is_textual(content_type: str, filename: str) -> bool:
    ct = (content_type or "").lower()
    if any(ct.startswith(p) for p in _TEXT_PREFIXES):
        return True
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    return ext in _TEXT_EXTS


def _is_image(content_type: str) -> bool:
    return (content_type or "").lower().startswith("image/")


def _is_pdf(content_type: str, filename: str) -> bool:
    ct = (content_type or "").lower()
    return ct == "application/pdf" or filename.lower().endswith(".pdf")

log = logging.getLogger("irs.attachments")
router = APIRouter(prefix="/attachments", tags=["attachments"])


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


def _viewer_for_request(request: Request, db: Session) -> Optional[models.User]:
    """Accept either an Authorization: Bearer JWT or the cookie session.
    Used by the download endpoint so browsers + API clients both work."""
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        from jose import jwt, JWTError
        token = auth.split(" ", 1)[1]
        try:
            payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
            u = db.get(models.User, payload.get("sub"))
            if u:
                return u
        except JWTError:
            pass
    return _user_from_cookie(request, db)


def _attach_to_out(a: models.Attachment) -> schemas.AttachmentOut:
    return schemas.AttachmentOut.model_validate(a)


def _viewer_can_see_attachment(db: Session, viewer: models.User, a: models.Attachment) -> bool:
    """An attachment is visible if the viewer can see *any* report in the
    same session_id (uses existing report scope, which already understands
    user, team, project membership)."""
    if a.user_id == viewer.id or viewer.role == models.Role.admin:
        return True
    if not a.session_id:
        return False
    # If any sibling report is visible to this user, the attachment is too.
    q = db.query(models.Report).filter(models.Report.session_id == a.session_id)
    q = scope_reports(q, db, viewer)
    return q.first() is not None


# ---------------- agent ingest ----------------

@router.post("/ingest", response_model=schemas.AttachmentOut, status_code=201)
def ingest(
    body: schemas.AttachmentIngest,
    db: Session = Depends(get_db),
    agent: models.Agent = Depends(get_current_agent),
):
    import datetime as _dt
    raw = base64.b64decode(body.content_b64)
    sha = hashlib.sha256(raw).hexdigest()
    if sha != body.sha256:
        raise HTTPException(400, "sha256 mismatch")
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(413, "attachment too large (max 50 MiB)")
    # Same heartbeat-touch as the report ingest path so the onboarding UI
    # can show the agent's last_seen even when only POCs are flowing.
    agent.last_seen = _dt.datetime.now(_dt.timezone.utc)

    # Dedup: same user + sha + filename + session is idempotent.
    existing = (
        db.query(models.Attachment)
        .filter(
            models.Attachment.user_id == agent.user_id,
            models.Attachment.sha256 == sha,
            models.Attachment.filename == body.filename,
            models.Attachment.session_id == body.session_id,
        )
        .first()
    )
    if existing:
        return _attach_to_out(existing)

    # If a VulnScan exists for this session, auto-link.
    scan = None
    if body.session_id:
        scan = (
            db.query(models.VulnScan)
            .filter(models.VulnScan.source_session_id == body.session_id)
            .first()
        )

    a = models.Attachment(
        user_id=agent.user_id,
        agent_id=agent.id,
        session_id=body.session_id,
        scan_id=scan.id if scan else None,
        filename=body.filename,
        original_path=body.original_path,
        content_type=body.content_type or "application/octet-stream",
        sha256=sha,
        size_bytes=len(raw),
        content_enc=crypto.encrypt(raw),
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    log.info(
        "ingest attachment id=%s user=%s session=%s file=%r size=%d",
        a.id, agent.user_id, body.session_id, body.filename, a.size_bytes,
    )
    return _attach_to_out(a)


# ---------------- list ----------------

@router.get("", response_model=list[schemas.AttachmentOut])
def list_attachments(
    session_id: str | None = None,
    scan_id: str | None = None,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    q = db.query(models.Attachment).order_by(models.Attachment.created_at.desc())
    if session_id:
        q = q.filter(models.Attachment.session_id == session_id)
    if scan_id:
        q = q.filter(models.Attachment.scan_id == scan_id)
    rows = q.limit(500).all()
    return [_attach_to_out(a) for a in rows if _viewer_can_see_attachment(db, viewer, a)]


# ---------------- download ----------------

def _load_for_viewer(att_id: str, request: Request, db: Session):
    viewer = _viewer_for_request(request, db)
    if viewer is None:
        raise HTTPException(401, "Not authenticated")
    a = db.get(models.Attachment, att_id)
    if not a:
        raise HTTPException(404, "Not found")
    if not _viewer_can_see_attachment(db, viewer, a):
        raise HTTPException(403, "Not allowed")
    return viewer, a


@router.get("/{att_id}/meta", response_model=schemas.AttachmentOut)
def meta(att_id: str, request: Request, db: Session = Depends(get_db)):
    """JSON metadata for the attachment — used by the React viewer to decide
    whether to render text inline, embed an image, etc., before fetching bytes."""
    _, a = _load_for_viewer(att_id, request, db)
    return _attach_to_out(a)


@router.get("/{att_id}/download")
def download(att_id: str, request: Request, db: Session = Depends(get_db)):
    _, a = _load_for_viewer(att_id, request, db)
    body = crypto.decrypt(a.content_enc)
    return Response(
        content=body,
        media_type=a.content_type or mimetypes.guess_type(a.filename)[0] or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{a.filename}"'},
    )


@router.get("/{att_id}/inline")
def inline(att_id: str, request: Request, db: Session = Depends(get_db)):
    """Serve raw bytes with Content-Disposition: inline — used by <img src>
    and <iframe src> embeds on the view page."""
    _, a = _load_for_viewer(att_id, request, db)
    body = crypto.decrypt(a.content_enc)
    return Response(
        content=body,
        media_type=a.content_type or mimetypes.guess_type(a.filename)[0] or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{a.filename}"'},
    )


@router.get("/{att_id}", response_class=HTMLResponse)
def view(att_id: str, request: Request, db: Session = Depends(get_db)):
    """HTML view page — preview text, images, PDFs inline; fall back to
    'binary, download' for unknown types."""
    viewer, a = _load_for_viewer(att_id, request, db)

    kind = "binary"
    text_preview: str | None = None
    truncated = False
    if _is_image(a.content_type):
        kind = "image"
    elif _is_pdf(a.content_type, a.filename):
        kind = "pdf"
    elif _is_textual(a.content_type, a.filename):
        kind = "text"
        raw = crypto.decrypt(a.content_enc)
        if len(raw) > _PREVIEW_MAX:
            raw = raw[:_PREVIEW_MAX]
            truncated = True
        text_preview = raw.decode("utf-8", errors="replace")

    # Find the scan/run this attachment belongs to so the back link is useful
    back_url = "/"
    back_label = "back"
    if a.scan_id:
        back_url = f"/ui/scans/{a.scan_id}"; back_label = "back to scan"
    elif a.session_id:
        back_url = f"/ui/runs/{a.session_id}"; back_label = "back to run"

    return templates.TemplateResponse(
        request,
        "attachment_view.html",
        {
            "user": viewer,
            "a": a,
            "kind": kind,
            "text_preview": text_preview,
            "truncated": truncated,
            "back_url": back_url,
            "back_label": back_label,
        },
    )
