"""Portal → remote Claude on the user's own machine, brokered through the
agent. Strictly owner-only: only the user who installed the agent
(`agent.user_id`) may talk to it — not managers, not admins.

v2: persistent multi-turn sessions. A RemoteSession is a conversation;
each turn is a RemotePrompt. The agent streams structured events
(`/chunk`) so the UI can show "thinking / using tool X / done" phases,
and reports the Claude `session_id` so follow-up turns use `--resume`.

The v1 one-shot endpoints under /agents/{aid}/remote are kept for the
legacy Agents page and existing tests.
"""
import asyncio
import datetime as dt
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import tarfile
import tempfile
import zipfile

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, crypto, database, storage
from ..auth import get_current_user, get_current_agent
from ..config import settings
from ..database import get_db
from .harnesses import _safe_rel, _visible as _harness_visible


_ARCHIVE_EXTS = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".tar.xz", ".txz")


_POLL_TIMEOUT_S = 25
_DONE: dict[str, asyncio.Event] = {}


def _event_for(rid: str) -> asyncio.Event:
    ev = _DONE.get(rid)
    if ev is None:
        ev = _DONE[rid] = asyncio.Event()
    return ev


def _own_agent(db: Session, viewer: models.User, agent_id: str) -> models.Agent:
    a = db.get(models.Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    if a.user_id != viewer.id:
        raise HTTPException(403, "Only the agent's owner can use it")
    return a


def _own_session(db: Session, viewer: models.User, sid: str) -> models.RemoteSession:
    s = db.get(models.RemoteSession, sid)
    if not s:
        raise HTTPException(404, "Session not found")
    if s.user_id != viewer.id:
        raise HTTPException(403, "Not your session")
    return s


def _decode_events(blob: bytes | None) -> list[dict]:
    if not blob:
        return []
    out: list[dict] = []
    for line in crypto.decrypt_str(blob).splitlines():
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"type": "text", "text": line})
    return out


def _to_result(rp: models.RemotePrompt) -> "PromptResult":
    return PromptResult(
        request_id=rp.id,
        session_id=rp.session_id,
        status=rp.status.value,
        prompt=crypto.decrypt_str(rp.prompt_enc),
        cwd=rp.cwd,
        output=crypto.decrypt_str(rp.output_enc) if rp.output_enc else None,
        events=_decode_events(rp.events_enc),
        error=rp.error,
        created_at=rp.created_at,
        completed_at=rp.completed_at,
    )


def _project_file_totals(db: Session, project_id: str) -> tuple[int, int]:
    n, b = (db.query(func.count(models.ProjectFile.id),
                     func.coalesce(func.sum(models.ProjectFile.size_bytes), 0))
              .filter(models.ProjectFile.project_id == project_id).one())
    return int(n), int(b)


def _to_session(db: Session, s: models.RemoteSession) -> "SessionOut":
    n_proj, b_proj = _project_file_totals(db, s.project_id) if s.project_id else (0, 0)
    return SessionOut(
        id=s.id, agent_id=s.agent_id, title=s.title, cwd=s.cwd,
        status=s.status.value, claude_session_id=s.claude_session_id,
        project_id=s.project_id,
        project_name=s.project.name if s.project else None,
        harness_id=s.harness_id,
        harness_name=s.harness.name if s.harness else None,
        model=s.model,
        cli=s.cli,
        pending_bundle=s.pending_bundle,
        turn_count=len(s.turns),
        upload_count=len(s.uploads) + n_proj,
        upload_bytes=sum(u.size_bytes for u in s.uploads) + b_proj,
        created_at=s.created_at, last_activity_at=s.last_activity_at,
    )


def _attach_harness(db: Session, viewer: models.User,
                    s: models.RemoteSession, harness_id: str | None):
    if not harness_id:
        s.harness_id = None
        return
    h = db.get(models.Harness, harness_id)
    if not h:
        raise HTTPException(400, "Harness not found")
    if not _harness_visible(viewer, h):
        raise HTTPException(403, "You can't use that harness")
    s.harness_id = h.id
    s.pending_bundle = True


def _gc_blobs(db: Session, keys: set[str]) -> None:
    """Remove on-disk blobs that no SessionUpload or ProjectFile row
    references any more (content-addressed → the same key may back rows
    across sessions and products)."""
    for k in keys:
        if not k:
            continue
        if (db.query(models.SessionUpload.id)
              .filter(models.SessionUpload.storage_key == k).first()):
            continue
        if (db.query(models.ProjectFile.id)
              .filter(models.ProjectFile.storage_key == k).first()):
            continue
        storage.remove(k)


_ACTIVE_TESTING_SENTINEL = "ACTIVE_TESTING_AUTHORIZED"


def _active_testing_authorized(s: models.RemoteSession) -> bool:
    """Opt-in: active-testing scope requires the sentinel to be deliberately placed in the
    harness or project description. A name match is intentionally NOT sufficient — enabling
    active network testing must be an explicit, auditable action, never a default."""
    for blob in ((s.harness.description if s.harness else None),
                 (s.project.description if s.project else None)):
        if blob and _ACTIVE_TESTING_SENTINEL in blob:
            return True
    return False


def _active_testing_targets(s: models.RemoteSession) -> list[str]:
    """The explicitly in-scope host(s) named alongside the sentinel, e.g.
    `ACTIVE_TESTING_AUTHORIZED target=host1,host2`. The authorisation names the target —
    CLAUDE.md does not just defer to the harness config."""
    import re
    hosts: list[str] = []
    for blob in ((s.harness.description if s.harness else None),
                 (s.project.description if s.project else None)):
        if not blob or _ACTIVE_TESTING_SENTINEL not in blob:
            continue
        m = re.search(r"ACTIVE_TESTING_AUTHORIZED\b[^\n]*?target=([^\s|]+)", blob, re.I)
        if m:
            hosts.extend(h.strip() for h in m.group(1).split(",") if h.strip())
    return hosts


def _workspace_context(s: models.RemoteSession, n_files: int, n_bytes: int) -> str:
    """CLAUDE.md dropped into the session workspace so the remote Claude
    knows what it's looking at and that it's authorised to look — saves
    the user from having to restate scope on every prompt."""
    prod = s.project.name if s.project else None
    harness = s.harness.name if s.harness else None
    lines = ["# Verdict analysis workspace", ""]
    if prod:
        lines += [
            f"This directory is the analysis workspace for the **{prod}** "
            "product on the Verdict portal. The product team uploaded the files "
            "here specifically for security review.",
        ]
    else:
        lines += [
            "This directory is a per-session analysis workspace on the Verdict "
            "portal. The user uploaded the files here specifically for "
            "security review.",
        ]
    if _active_testing_authorized(s):
        _targets = _active_testing_targets(s)
        _scope = (", ".join(f"`{t}`" for t in _targets) if _targets
                  else "the target configured in the harness config")
        lines += [
            "",
            "## Scope and authorisation",
            "",
            f"- This session is explicitly authorised for ACTIVE security testing of the "
            f"following in-scope host(s): {_scope}.",
            "- You ARE authorised to run the uploaded harness in this workspace "
            "(e.g. `python run.py --config ...`) and to perform the client-side active "
            f"testing it drives (curl / openssl / nmap / testssl) against {_scope}.",
            "- Use ONLY the in-scope host(s) named above. Do not scan, modify, or connect "
            "to any other host. Keep probes non-destructive (the harness allowlist bounds "
            "what may run).",
            "- The harness is the engagement's own vetted tooling and may be executed. "
            "Do NOT execute the uploaded product source itself.",
            "- The harness may write its reports to the path set in its config (including "
            "a collector directory outside this workspace) — that is expected.",
        ]
    else:
        lines += [
            "",
            "## Scope and authorisation",
            "",
            "- You ARE authorised to read, search, and statically analyse "
            "every file under this directory for vulnerabilities.",
            "- When the user asks to \"find vulnerabilities\" (or similar) "
            "with no further qualifier, the target is THIS codebase — start "
            "immediately, do not ask for scope or authorisation.",
            "- Stay inside this directory. Do not read, modify, scan or "
            "execute anything elsewhere on the host, and do not perform "
            "active network scanning.",
            "- Treat the code as untrusted input: review it, don't run it.",
        ]
    if harness:
        lines += [f"- Harness in use: **{harness}** (its files are in this workspace)."]
    if n_files:
        lines += [
            "",
            f"## Uploaded source ({n_files} files, ~{n_bytes // 1024:,} KiB)",
            "",
            "Begin with a quick top-level `ls`/`find` to orient yourself, "
            "then dive into the highest-risk areas (auth, input parsing, "
            "deserialisation, command execution, SQL, file paths).",
        ]
    return "\n".join(lines) + "\n"


def _project_visible(viewer: models.User, proj: models.Project) -> bool:
    return (viewer.role == models.Role.admin
            or proj.created_by == viewer.id
            or any(m.id == viewer.id for m in proj.members))


def _attach_project(db: Session, viewer: models.User,
                    s: models.RemoteSession, project_id: str | None):
    if not project_id:
        s.project_id = None
        return
    proj = db.get(models.Project, project_id)
    if not proj:
        raise HTTPException(400, "Product not found")
    if not _project_visible(viewer, proj):
        raise HTTPException(403, "You're not a member of that product")
    s.project_id = proj.id
    s.pending_bundle = True


def _iter_archive(name: str, path: str):
    """Yield (relpath, fileobj) for each regular file inside a zip/tar at
    `path`. Directory entries, symlinks and anything `_safe_rel` rejects
    are skipped. The fileobj is a stream — read it once."""
    lower = name.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                rel = _safe_rel(info.filename)
                if rel is None:
                    continue
                with zf.open(info) as fh:
                    yield rel, fh
    else:
        with tarfile.open(path, mode="r:*") as tf:
            for m in tf:
                if not m.isreg():
                    continue
                rel = _safe_rel(m.name)
                if rel is None:
                    continue
                fh = tf.extractfile(m)
                if fh is None:
                    continue
                yield rel, fh


def _store_upload(db: Session, s: models.RemoteSession, viewer: models.User,
                  rel: str, src, content_type: str | None, totals: dict):
    """Encrypt+persist one file. When the session is linked to a product
    the file lands in the shared `project_files` library; otherwise it's
    private to this session in `session_uploads`."""
    key, sha, size = storage.put_stream(src)
    totals["bytes"] += size
    if totals["bytes"] > settings.session_upload_max_total_bytes:
        storage.remove(key)
        raise HTTPException(
            413, f"uploads exceed {settings.session_upload_max_total_bytes} bytes")
    ct = content_type or mimetypes.guess_type(rel)[0] or "application/octet-stream"
    if s.project_id:
        row = (db.query(models.ProjectFile)
                 .filter_by(project_id=s.project_id, relpath=rel).first())
        if row:
            totals["bytes"] -= row.size_bytes
            row.content_type = ct; row.sha256 = sha; row.size_bytes = size
            row.storage_key = key; row.uploaded_by = viewer.id
        else:
            if totals["files"] + 1 > settings.session_upload_max_files:
                storage.remove(key)
                raise HTTPException(
                    413, f"too many files (max {settings.session_upload_max_files})")
            db.add(models.ProjectFile(
                project_id=s.project_id, relpath=rel, content_type=ct,
                sha256=sha, size_bytes=size, storage_key=key,
                uploaded_by=viewer.id,
            ))
            totals["files"] += 1
        return
    row = (db.query(models.SessionUpload)
             .filter_by(session_id=s.id, relpath=rel).first())
    if row:
        totals["bytes"] -= row.size_bytes
        row.content_type = ct; row.sha256 = sha; row.size_bytes = size
        row.storage_key = key; row.content_enc = None
    else:
        if totals["files"] + 1 > settings.session_upload_max_files:
            storage.remove(key)
            raise HTTPException(
                413, f"too many files (max {settings.session_upload_max_files})")
        db.add(models.SessionUpload(
            session_id=s.id, relpath=rel, content_type=ct,
            sha256=sha, size_bytes=size, storage_key=key,
        ))
        totals["files"] += 1


# ---- schemas ---------------------------------------------------------------

class PromptIn(BaseModel):
    prompt: str
    cwd: str | None = None


class PromptRef(BaseModel):
    request_id: str
    session_id: str | None = None


class PromptResult(BaseModel):
    request_id: str
    session_id: str | None = None
    status: str
    prompt: str
    cwd: str | None = None
    output: str | None = None
    events: list[dict] = []
    error: str | None = None
    created_at: dt.datetime
    completed_at: dt.datetime | None = None


class SessionIn(BaseModel):
    agent_id: str
    title: str = ""
    cwd: str | None = None
    project_id: str | None = None
    harness_id: str | None = None
    model: str | None = None


class SessionPatch(BaseModel):
    title: str | None = None
    cwd: str | None = None
    archived: bool | None = None
    project_id: str | None = None
    harness_id: str | None = None


class SessionOut(BaseModel):
    id: str
    agent_id: str
    title: str
    cwd: str | None
    status: str
    claude_session_id: str | None
    project_id: str | None = None
    project_name: str | None = None
    harness_id: str | None = None
    harness_name: str | None = None
    model: str | None = None
    cli: str | None = None
    pending_bundle: bool = False
    turn_count: int
    upload_count: int = 0
    upload_bytes: int = 0
    created_at: dt.datetime
    last_activity_at: dt.datetime


class UploadOut(BaseModel):
    id: str
    relpath: str
    size_bytes: int
    content_type: str
    sha256: str
    created_at: dt.datetime
    source: Literal["session", "project"] = "session"
    uploaded_by_email: str | None = None


def _to_uploads(db: Session, s: models.RemoteSession) -> list[UploadOut]:
    out: list[UploadOut] = []
    if s.project_id:
        rows = (db.query(models.ProjectFile, models.User.email)
                  .outerjoin(models.User,
                             models.ProjectFile.uploaded_by == models.User.id)
                  .filter(models.ProjectFile.project_id == s.project_id)
                  .order_by(models.ProjectFile.relpath).all())
        for f, email in rows:
            out.append(UploadOut(
                id=f.id, relpath=f.relpath, size_bytes=f.size_bytes,
                content_type=f.content_type, sha256=f.sha256,
                created_at=f.created_at, source="project",
                uploaded_by_email=email))
    for u in s.uploads:
        out.append(UploadOut(
            id=u.id, relpath=u.relpath, size_bytes=u.size_bytes,
            content_type=u.content_type, sha256=u.sha256,
            created_at=u.created_at, source="session"))
    return out


class SessionDetail(SessionOut):
    turns: list[PromptResult]
    uploads: list[UploadOut] = []


# ---- sessions (Bearer JWT) -------------------------------------------------

sess_api = APIRouter(prefix="/remote/sessions", tags=["remote"])


@sess_api.get("", response_model=list[SessionOut])
def list_sessions(
    agent_id: str | None = None,
    include_archived: bool = False,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    q = db.query(models.RemoteSession).filter(models.RemoteSession.user_id == viewer.id)
    if agent_id:
        _own_agent(db, viewer, agent_id)
        q = q.filter(models.RemoteSession.agent_id == agent_id)
    if not include_archived:
        q = q.filter(models.RemoteSession.status != models.RemoteSessionStatus.archived)
    rows = q.order_by(models.RemoteSession.last_activity_at.desc()).all()
    return [_to_session(db, s) for s in rows]


@sess_api.post("", response_model=SessionOut)
def create_session(
    body: SessionIn,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    _own_agent(db, viewer, body.agent_id)
    s = models.RemoteSession(
        agent_id=body.agent_id,
        user_id=viewer.id,
        title=(body.title or "").strip()[:255],
        cwd=(body.cwd or "").strip() or None,
        model=(body.model or "").strip()[:128] or None,
    )
    _attach_project(db, viewer, s, body.project_id)
    _attach_harness(db, viewer, s, body.harness_id)
    db.add(s)
    db.commit()
    db.refresh(s)
    return _to_session(db, s)


@sess_api.get("/{sid}", response_model=SessionDetail)
def get_session(
    sid: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    s = _own_session(db, viewer, sid)
    return SessionDetail(
        **_to_session(db, s).model_dump(),
        turns=[_to_result(t) for t in s.turns],
        uploads=_to_uploads(db, s),
    )


@sess_api.patch("/{sid}", response_model=SessionOut)
def patch_session(
    sid: str,
    body: SessionPatch,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    s = _own_session(db, viewer, sid)
    if body.title is not None:
        s.title = body.title.strip()[:255]
    if body.cwd is not None:
        s.cwd = body.cwd.strip() or None
    if body.archived is not None:
        s.status = (models.RemoteSessionStatus.archived if body.archived
                    else models.RemoteSessionStatus.idle)
    if "project_id" in body.model_fields_set:
        _attach_project(db, viewer, s, body.project_id)
    if "harness_id" in body.model_fields_set:
        _attach_harness(db, viewer, s, body.harness_id)
    db.commit()
    db.refresh(s)
    return _to_session(db, s)


@sess_api.delete("/{sid}", status_code=204)
def delete_session(
    sid: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    s = _own_session(db, viewer, sid)
    keys = {u.storage_key for u in s.uploads if u.storage_key}
    db.delete(s)
    db.commit()
    _gc_blobs(db, keys)


@sess_api.get("/{sid}/files", response_model=list[UploadOut])
def list_session_files(
    sid: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    s = _own_session(db, viewer, sid)
    return _to_uploads(db, s)


@sess_api.post("/{sid}/files", response_model=SessionOut)
async def upload_session_files(
    sid: str,
    relpaths: list[str] = Form(default=[]),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Stage files into the session workspace. Loose files are stored at
    their relpath; archives (.zip/.tar/.tar.gz/.tgz) are unpacked
    server-side. Re-uploading the same relpath replaces the row. Any
    successful write re-arms `pending_bundle` so the next prompt
    re-materializes the workspace on the agent."""
    s = _own_session(db, viewer, sid)
    if not files:
        raise HTTPException(400, "no files")
    if relpaths and len(relpaths) != len(files):
        raise HTTPException(400, "files / relpaths length mismatch")

    if s.project_id:
        n0, b0 = _project_file_totals(db, s.project_id)
    else:
        n0 = db.query(models.SessionUpload).filter_by(session_id=s.id).count()
        b0 = sum(u.size_bytes for u in s.uploads)
    totals = {"files": n0, "bytes": b0}
    spool_dir = storage._root()
    written = 0
    try:
        for i, upload in enumerate(files):
            name = (relpaths[i] if relpaths else upload.filename) or ""
            if name.lower().endswith(_ARCHIVE_EXTS):
                # Spool the archive to a real temp file so zip/tar can
                # seek/stream through it without loading it into RAM.
                with tempfile.NamedTemporaryFile(
                        dir=spool_dir, prefix=".arc-", delete=True) as tmp:
                    shutil.copyfileobj(upload.file, tmp, 1024 * 1024)
                    tmp.flush()
                    try:
                        for rel, fh in _iter_archive(name, tmp.name):
                            _store_upload(db, s, viewer, rel, fh, None, totals)
                            written += 1
                    except (zipfile.BadZipFile, tarfile.TarError) as e:
                        raise HTTPException(400, f"{name}: not a valid archive ({e})")
            else:
                rel = _safe_rel(name)
                if rel is None:
                    raise HTTPException(400, f"invalid relpath {name!r}")
                _store_upload(db, s, viewer, rel, upload.file,
                              upload.content_type, totals)
                written += 1
    except Exception:
        db.rollback()
        raise
    if written:
        s.last_activity_at = dt.datetime.now(dt.timezone.utc)
        if s.project_id:
            (db.query(models.RemoteSession)
               .filter(models.RemoteSession.project_id == s.project_id)
               .update({models.RemoteSession.pending_bundle: True}))
        s.pending_bundle = True
    db.commit()
    db.refresh(s)
    return _to_session(db, s)


@sess_api.delete("/{sid}/files/{file_id}", status_code=204)
def delete_session_file(
    sid: str,
    file_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    s = _own_session(db, viewer, sid)
    row = db.get(models.SessionUpload, file_id)
    if row and row.session_id == s.id:
        key = row.storage_key
        db.delete(row)
        s.pending_bundle = True
        db.commit()
        _gc_blobs(db, {key} if key else set())
        return
    if s.project_id:
        pf = db.get(models.ProjectFile, file_id)
        if pf and pf.project_id == s.project_id:
            key = pf.storage_key
            db.delete(pf)
            (db.query(models.RemoteSession)
               .filter(models.RemoteSession.project_id == s.project_id)
               .update({models.RemoteSession.pending_bundle: True}))
            db.commit()
            _gc_blobs(db, {key})
            return
    raise HTTPException(404, "File not found")


@sess_api.delete("/{sid}/files", status_code=204)
def clear_session_files(
    sid: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    s = _own_session(db, viewer, sid)
    keys = {u.storage_key for u in s.uploads if u.storage_key}
    db.query(models.SessionUpload).filter_by(session_id=s.id).delete()
    s.pending_bundle = True
    db.commit()
    _gc_blobs(db, keys)


@sess_api.post("/{sid}/prompt", response_model=PromptRef)
def send_session_prompt(
    sid: str,
    body: PromptIn,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    s = _own_session(db, viewer, sid)
    if s.status == models.RemoteSessionStatus.running:
        raise HTTPException(409, "This session already has a turn in flight")
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "Prompt is empty")
    if body.cwd is not None:
        s.cwd = body.cwd.strip() or None
    # If the session has files but Claude hasn't been pointed at a
    # workspace yet, force a bundle fetch so the agent materializes one
    # and runs Claude inside it instead of defaulting to $HOME.
    if not s.cwd and not s.pending_bundle and (
        s.harness_id or s.uploads
        or (s.project_id and _project_file_totals(db, s.project_id)[0])
    ):
        s.pending_bundle = True
    if not s.title:
        s.title = prompt[:60]
    rp = models.RemotePrompt(
        agent_id=s.agent_id,
        user_id=viewer.id,
        session_id=s.id,
        cwd=s.cwd,
        prompt_enc=crypto.encrypt_str(prompt),
    )
    s.status = models.RemoteSessionStatus.running
    s.last_activity_at = dt.datetime.now(dt.timezone.utc)
    db.add(rp)
    db.commit()
    return PromptRef(request_id=rp.id, session_id=s.id)


# ---- legacy one-shot (Bearer JWT) ------------------------------------------

api = APIRouter(prefix="/agents", tags=["remote"])


@api.post("/{agent_id}/remote", response_model=PromptRef)
def send_prompt(
    agent_id: str,
    body: PromptIn,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    _own_agent(db, viewer, agent_id)
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "Prompt is empty")
    rp = models.RemotePrompt(
        agent_id=agent_id,
        user_id=viewer.id,
        cwd=(body.cwd or "").strip() or None,
        prompt_enc=crypto.encrypt_str(prompt),
    )
    db.add(rp)
    db.commit()
    return PromptRef(request_id=rp.id)


@api.get("/{agent_id}/remote", response_model=list[PromptResult])
def list_prompts(
    agent_id: str,
    limit: int = 20,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    _own_agent(db, viewer, agent_id)
    rows = (
        db.query(models.RemotePrompt)
        .filter(models.RemotePrompt.agent_id == agent_id)
        .order_by(models.RemotePrompt.created_at.desc())
        .limit(min(limit, 100))
        .all()
    )
    return [_to_result(r) for r in rows]


@api.get("/{agent_id}/remote/{request_id}", response_model=PromptResult)
async def get_result(
    agent_id: str,
    request_id: str,
    wait: int = 0,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    _own_agent(db, viewer, agent_id)
    rp = db.get(models.RemotePrompt, request_id)
    if not rp or rp.agent_id != agent_id:
        raise HTTPException(404, "Unknown request")
    if wait and rp.status in (models.RemotePromptStatus.pending,
                              models.RemotePromptStatus.running):
        try:
            await asyncio.wait_for(_event_for(rp.id).wait(),
                                   timeout=min(wait, _POLL_TIMEOUT_S))
        except asyncio.TimeoutError:
            pass
        db.refresh(rp)
    return _to_result(rp)


class SaveIn(BaseModel):
    title: str
    project_id: str | None = None


class SaveOut(BaseModel):
    report_id: str
    scan_id: str


@api.post("/{agent_id}/remote/{request_id}/save", response_model=SaveOut)
def save_as_report(
    agent_id: str,
    request_id: str,
    body: SaveIn,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    a = _own_agent(db, viewer, agent_id)
    rp = db.get(models.RemotePrompt, request_id)
    if not rp or rp.agent_id != agent_id:
        raise HTTPException(404, "Unknown request")
    if rp.status not in (models.RemotePromptStatus.done,
                         models.RemotePromptStatus.error):
        raise HTTPException(400, "Prompt is still running")
    text = crypto.decrypt_str(rp.output_enc) if rp.output_enc else ""
    if not text.strip():
        raise HTTPException(400, "No output to save")

    title = (body.title or "").strip()[:255]
    if not title:
        raise HTTPException(400, "Title is required")

    project_id = (body.project_id or "").strip() or None
    if project_id:
        proj = db.get(models.Project, project_id)
        if not proj:
            raise HTTPException(400, "Product not found")
        if (viewer.role != models.Role.admin
                and proj.created_by != viewer.id
                and not any(m.id == viewer.id for m in proj.members)):
            raise HTTPException(403, "You're not a member of that product")

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()[:48] or "remote"
    raw = text.encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()

    rpt = (
        db.query(models.Report)
        .filter(models.Report.user_id == viewer.id, models.Report.sha256 == sha)
        .first()
    )
    if rpt is None:
        rpt = models.Report(
            user_id=viewer.id,
            agent_id=a.id,
            source_tool=models.SourceTool.generated,
            filename=f"{slug}-{rp.id[:8]}.md",
            title=title,
            sha256=sha,
            size_bytes=len(raw),
            content_enc=crypto.encrypt(raw),
            project_id=project_id,
        )
        db.add(rpt)
        db.flush()

    scan = models.VulnScan(
        user_id=viewer.id,
        source_report_id=rpt.id,
        project_id=project_id,
        state=models.ScanState.draft,
        product=title,
        scan_target=rp.cwd or "",
        scan_by=viewer.email,
    )
    db.add(scan)
    db.commit()
    return SaveOut(report_id=rpt.id, scan_id=scan.id)


@api.delete("/{agent_id}/remote/{request_id}", status_code=204)
def delete_prompt(
    agent_id: str,
    request_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    _own_agent(db, viewer, agent_id)
    rp = db.get(models.RemotePrompt, request_id)
    if rp and rp.agent_id == agent_id:
        db.delete(rp)
        db.commit()
    _DONE.pop(request_id, None)


# ---- agent-facing (X-Agent-Key) -------------------------------------------

agent_api = APIRouter(prefix="/agent/remote", tags=["remote"])


class Job(BaseModel):
    request_id: str
    prompt: str
    cwd: str | None = None
    resume: str | None = None
    session_id: str | None = None
    bundle_url: str | None = None
    model: str | None = None
    append_system_prompt: str | None = None
    bypass_permissions: bool = False


def _active_testing_system_prompt(s: models.RemoteSession) -> str | None:
    """For an active-testing-authorised session, inject the engagement
    authorisation at the system-prompt layer (claude --append-system-prompt),
    not only the workspace CLAUDE.md. The system prompt carries more weight and
    survives every --resume turn regardless of cwd, so the remote Claude actually
    conducts the black-box testing instead of hedging on scope/permission. The
    detailed scope (in-scope hosts, rules) stays in CLAUDE.md; this is the
    high-level clearance. Returns None for static-analysis sessions."""
    if not _active_testing_authorized(s):
        return None
    harness = s.harness.name if s.harness else None
    where = f"the uploaded **{harness}** harness" if harness else "the uploaded harness"
    return (
        "You are running inside the Verdict security portal on an explicitly authorised, "
        "scoped penetration-testing engagement. The session owner has authorised ACTIVE, "
        "black-box security testing of the in-scope target(s) defined in this workspace's "
        f"CLAUDE.md and the harness config. You ARE cleared to execute {where} and to run "
        "the client-side active probes it drives (e.g. curl, openssl, nmap, testssl) against "
        "those in-scope target(s) only. Keep probes non-destructive, never touch any host "
        "outside the named scope, and never echo or exfiltrate credentials. This "
        "authorisation is established for this engagement — proceed with the testing without "
        "asking the user to re-confirm scope or permission."
    )


class JobResult(BaseModel):
    ok: bool
    output: str = ""
    error: str = ""
    claude_session_id: str | None = None
    workspace: str | None = None
    # Which agent CLI produced this turn ("claude", "generic", ...). Older
    # agents don't send it, so it stays None for them.
    cli: str | None = None


def _claim_pending(agent_id: str) -> "Job | None":
    db = database.SessionLocal()
    try:
        rp = (
            db.query(models.RemotePrompt)
            .filter(
                models.RemotePrompt.agent_id == agent_id,
                models.RemotePrompt.status == models.RemotePromptStatus.pending,
            )
            .order_by(models.RemotePrompt.created_at)
            .with_for_update(skip_locked=True)
            .first()
        )
        if not rp:
            return None
        rp.status = models.RemotePromptStatus.running
        resume = None
        bundle_url = None
        model = None
        sys_prompt = None
        if rp.session_id:
            s = db.get(models.RemoteSession, rp.session_id)
            if s:
                resume = s.claude_session_id
                model = s.model
                sys_prompt = _active_testing_system_prompt(s)
                if s.pending_bundle:
                    bundle_url = f"/agent/remote/sessions/{s.id}/bundle.tar.gz"
        db.commit()
        return Job(
            request_id=rp.id,
            prompt=crypto.decrypt_str(rp.prompt_enc),
            cwd=rp.cwd,
            resume=resume,
            session_id=rp.session_id,
            bundle_url=bundle_url,
            model=model,
            append_system_prompt=sys_prompt,
            # EVERY remote session runs headless — there is never a human to
            # answer Claude Code's permission prompts, so without this the agent
            # can't even Write/Edit a file or run non-trivial Bash (pipes,
            # redirects, find globs); that breaks plain "review this code"
            # sessions, not just the active-testing harness. The agent runs on
            # the owner's own machine at their explicit request, so we bypass.
            # (Caution about running untrusted uploaded code lives in the
            # workspace CLAUDE.md, which a headless permission sandbox can't
            # enforce anyway.)
            bypass_permissions=True,
        )
    finally:
        db.close()


@agent_api.get("/poll")
async def poll(
    response: Response,
    db: Session = Depends(get_db),
    agent: models.Agent = Depends(get_current_agent),
):
    if agent.pending_key_push:
        key = crypto.decrypt_str(agent.anthropic_key_enc) if agent.anthropic_key_enc else ""
        # Leave pending_key_push set until the agent acks via /key-applied. The
        # command is idempotent, so re-sending on the next poll after a dropped
        # response or a crash mid-apply is harmless and self-heals the delivery —
        # rather than clearing the flag here and silently losing the key.
        return {"command": "set_anthropic_key", "key": key}
    if agent.pending_upgrade:
        agent.pending_upgrade = False
        db.commit()
        return {"command": "upgrade"}
    for _ in range(_POLL_TIMEOUT_S):
        job = await asyncio.to_thread(_claim_pending, agent.id)
        if job:
            return job
        await asyncio.sleep(1)
    response.status_code = 204
    return


@agent_api.post("/key-applied", status_code=204)
def key_applied(
    db: Session = Depends(get_db),
    agent: models.Agent = Depends(get_current_agent),
):
    """Agent confirms it saved the pushed Anthropic key. Only now do we clear
    the pending flag — so a delivery lost to a dropped response or an agent
    crash re-sends on the next poll instead of being silently dropped."""
    agent.pending_key_push = False
    agent.anthropic_key_pushed_at = dt.datetime.now(dt.timezone.utc)
    db.commit()


class _TarSink:
    """tarfile writes here; the request generator drains `buf` after each
    member so the gzip stream goes out incrementally instead of building
    a multi-GB tarball in memory."""
    def __init__(self): self.buf = bytearray()
    def write(self, b: bytes) -> int: self.buf += b; return len(b)
    def drain(self) -> bytes: out = bytes(self.buf); self.buf.clear(); return out


@agent_api.get("/sessions/{sid}/bundle.tar.gz")
def session_bundle(
    sid: str,
    db: Session = Depends(get_db),
    agent: models.Agent = Depends(get_current_agent),
):
    """Streaming tarball of everything the agent should materialize into
    the per-session workspace: harness files first, then session uploads
    (so uploads win on relpath collision). Uploads are decrypted off disk
    chunk-by-chunk; nothing is held fully in memory."""
    s = db.get(models.RemoteSession, sid)
    if not s or s.agent_id != agent.id:
        raise HTTPException(404, "Unknown session")

    harness_rows = []
    if s.harness_id:
        harness_rows = (db.query(models.HarnessFile)
                          .filter(models.HarnessFile.harness_id == s.harness_id)
                          .order_by(models.HarnessFile.relpath).all())
    project_rows: list[models.ProjectFile] = []
    if s.project_id:
        proj = db.get(models.Project, s.project_id)
        owner = db.get(models.User, agent.user_id)
        if proj and owner and _project_visible(owner, proj):
            project_rows = (db.query(models.ProjectFile)
                              .filter(models.ProjectFile.project_id == s.project_id)
                              .order_by(models.ProjectFile.relpath).all())
    upload_rows = (db.query(models.SessionUpload)
                     .filter(models.SessionUpload.session_id == s.id)
                     .order_by(models.SessionUpload.relpath).all())
    # session uploads overlay product files; both overlay harness files
    merged: dict[str, models.ProjectFile | models.SessionUpload] = {
        f.relpath: f for f in project_rows
    }
    for u in upload_rows:
        merged[u.relpath] = u
    upload_paths = set(merged)
    harness_paths = {f.relpath for f in harness_rows}

    context_md: bytes | None = None
    if "CLAUDE.md" not in upload_paths and "CLAUDE.md" not in harness_paths:
        context_md = _workspace_context(
            s, len(merged), sum(u.size_bytes for u in merged.values()),
        ).encode("utf-8")

    def gen():
        sink = _TarSink()
        tf = tarfile.open(fileobj=sink, mode="w|gz")
        BS = tarfile.BLOCKSIZE
        try:
            def add_bytes(relpath: str, raw: bytes):
                info = tarfile.TarInfo(name=relpath)
                info.size = len(raw); info.mode = 0o644
                tf.addfile(info, io.BytesIO(raw))

            def add_stream(relpath: str, size: int, reader):
                # Py3.13 tarfile.addfile() rejects fileobj=None for non-empty
                # members, so emit the header ourselves and stream the body.
                info = tarfile.TarInfo(name=relpath)
                info.size = size; info.mode = 0o644
                buf = info.tobuf(tf.format, tf.encoding, tf.errors)
                tf.fileobj.write(buf)
                tf.offset += len(buf)
                remaining = size
                while remaining > 0:
                    chunk = reader.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise IOError(f"short read for {relpath}")
                    tf.fileobj.write(chunk)
                    remaining -= len(chunk)
                    yield sink.drain()
                pad = (-size) % BS
                if pad:
                    tf.fileobj.write(b"\0" * pad)
                tf.offset += size + pad

            if context_md:
                add_bytes("CLAUDE.md", context_md)
                yield sink.drain()
            for f in harness_rows:
                if f.relpath in upload_paths:
                    continue
                add_bytes(f.relpath, crypto.decrypt(f.content_enc))
                yield sink.drain()
            for rel, u in sorted(merged.items()):
                if u.storage_key:
                    with storage.open_raw(u.storage_key) as fh:
                        yield from add_stream(rel, u.size_bytes,
                                              crypto.DecryptReader(fh))
                elif getattr(u, "content_enc", None):
                    add_bytes(rel, crypto.decrypt(u.content_enc))
                yield sink.drain()
        finally:
            tf.close()
        yield sink.drain()

    return StreamingResponse(gen(), media_type="application/gzip")


class Chunk(BaseModel):
    text: str | None = None
    event: dict | None = None


@agent_api.post("/{request_id}/chunk", status_code=204)
def post_chunk(
    request_id: str,
    body: Chunk,
    db: Session = Depends(get_db),
    agent: models.Agent = Depends(get_current_agent),
):
    rp = db.get(models.RemotePrompt, request_id)
    if not rp or rp.agent_id != agent.id:
        raise HTTPException(404, "Unknown request")
    if rp.status == models.RemotePromptStatus.pending:
        rp.status = models.RemotePromptStatus.running

    if body.event is not None:
        cur = crypto.decrypt_str(rp.events_enc) if rp.events_enc else ""
        rp.events_enc = crypto.encrypt_str(cur + json.dumps(body.event) + "\n")
        # Capture session_id eagerly so a follow-up sent before /result lands
        # still resumes the right conversation.
        if (rp.session_id and body.event.get("type") == "system"
                and body.event.get("session_id")):
            s = db.get(models.RemoteSession, rp.session_id)
            if s and not s.claude_session_id:
                s.claude_session_id = body.event["session_id"]
    elif body.text:
        cur = crypto.decrypt_str(rp.output_enc) if rp.output_enc else ""
        rp.output_enc = crypto.encrypt_str(cur + body.text)

    db.commit()


@agent_api.post("/{request_id}/result", status_code=204)
def post_result(
    request_id: str,
    body: JobResult,
    db: Session = Depends(get_db),
    agent: models.Agent = Depends(get_current_agent),
):
    rp = db.get(models.RemotePrompt, request_id)
    if not rp or rp.agent_id != agent.id:
        raise HTTPException(404, "Unknown request")
    rp.output_enc = crypto.encrypt_str(body.output) if body.output else None
    if body.ok:
        rp.status = models.RemotePromptStatus.done
        rp.error = None
    else:
        rp.status = models.RemotePromptStatus.error
        rp.error = body.error or "agent reported failure"
    rp.completed_at = dt.datetime.now(dt.timezone.utc)

    if rp.session_id:
        s = db.get(models.RemoteSession, rp.session_id)
        if s:
            if body.claude_session_id:
                s.claude_session_id = body.claude_session_id
            if body.workspace:
                s.cwd = body.workspace[:1024]
            if body.cli:
                s.cli = body.cli[:32]
            # Always cleared: the workspace is materialized once, and older
            # agents don't send `cli` at all.
            s.pending_bundle = False
            s.status = models.RemoteSessionStatus.idle
            s.last_activity_at = rp.completed_at

    db.commit()
    ev = _DONE.pop(request_id, None)
    if ev:
        ev.set()
