"""Projects: group runs/reports + a membership list that grants visibility.

A logged-in user can create a project; the creator is auto-added as the first
member. Project members and the creator can edit/delete the project. Admins
can edit any project.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, schemas, storage
from ..ai.component_analyzer import analyze_component
from ..ai.import_planner import _NOISE_DIRS
from ..auth import get_current_user
from ..config import settings
from ..database import get_db
from ..permissions import assert_can_delete
from .remote import _gc_blobs

log = logging.getLogger("irs.projects")

api = APIRouter(prefix="/projects", tags=["projects"])
ui = APIRouter(prefix="/ui/projects", tags=["projects-ui"])
templates = Jinja2Templates(directory="app/templates")


# ---------- helpers ----------

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


def _is_member(p: models.Project, user: models.User) -> bool:
    return any(m.id == user.id for m in p.members)


def _is_owner(p: models.Project, user: models.User) -> bool:
    return p.created_by == user.id


def _can_edit(p: models.Project, user: models.User) -> bool:
    """Admin or the owner. Members no longer have edit rights."""
    return user.role == models.Role.admin or _is_owner(p, user)


def _can_view_contents(p: models.Project, user: models.User) -> bool:
    """Admin + owner + members see the contents (members, runs, scans, reports).
    Everyone else only sees the project's name + description on the detail page."""
    return (
        user.role == models.Role.admin
        or _is_owner(p, user)
        or _is_member(p, user)
    )


# ---------- API (Bearer) ----------

@api.get("", response_model=list[schemas.ProjectOut])
def list_projects(
    db: Session = Depends(get_db), viewer: models.User = Depends(get_current_user)
):
    # Project lists are visible to every logged-in user; contents are gated
    # separately on the detail page.
    q = db.query(models.Project).order_by(models.Project.created_at.desc())
    out: list[schemas.ProjectOut] = []
    for p in q.all():
        po = schemas.ProjectOut.model_validate(p)
        po.i_am_owner = _is_owner(p, viewer)
        po.i_am_member = _is_member(p, viewer)
        out.append(po)
    return out


@api.post("", response_model=schemas.ProjectOut, status_code=201)
def create_project(
    body: schemas.ProjectCreate,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = models.Project(name=body.name.strip(), description=body.description,
                       created_by=viewer.id)
    if not p.name:
        raise HTTPException(400, "Project name is required")
    p.members.append(viewer)  # creator is the first member
    db.add(p)
    db.commit()
    db.refresh(p)
    return schemas.ProjectOut.model_validate(p)


@api.get("/{project_id}", response_model=schemas.ProjectDetail)
def get_project(
    project_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    return _detail(db, p, viewer)


@api.get("/{project_id}/files", response_model=schemas.ProjectFilesOut)
def list_project_files(
    project_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_view_contents(p, viewer):
        raise HTTPException(403, "Not a member of this product")
    rows = (
        db.query(models.ProjectFile, models.User.email)
        .outerjoin(models.User, models.User.id == models.ProjectFile.uploaded_by)
        .filter(models.ProjectFile.project_id == project_id)
        .order_by(models.ProjectFile.relpath)
        .all()
    )
    files = [
        schemas.ProjectFileOut(
            id=pf.id, relpath=pf.relpath, content_type=pf.content_type,
            sha256=pf.sha256, size_bytes=pf.size_bytes,
            uploaded_by_email=email, created_at=pf.created_at,
        )
        for pf, email in rows
    ]
    return schemas.ProjectFilesOut(
        count=len(files),
        total_bytes=sum(f.size_bytes for f in files),
        files=files,
    )


@api.delete("/{project_id}/files/{file_id}", status_code=204)
def delete_project_file(
    project_id: str,
    file_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_view_contents(p, viewer):
        raise HTTPException(403, "Not a member of this product")
    pf = db.get(models.ProjectFile, file_id)
    if not pf or pf.project_id != project_id:
        raise HTTPException(404, "File not found")
    key = pf.storage_key
    db.delete(pf)
    _bump_sessions(db, project_id)
    db.commit()
    _gc_blobs(db, {key})


@api.delete("/{project_id}/files", status_code=204)
def clear_project_files(
    project_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_edit(p, viewer):
        raise HTTPException(403, "Only the product owner can clear all files")
    rows = (db.query(models.ProjectFile)
              .filter(models.ProjectFile.project_id == project_id).all())
    keys = {r.storage_key for r in rows}
    for r in rows:
        db.delete(r)
    _bump_sessions(db, project_id)
    db.commit()
    _gc_blobs(db, keys)


def _bump_sessions(db: Session, project_id: str) -> None:
    (db.query(models.RemoteSession)
       .filter(models.RemoteSession.project_id == project_id)
       .update({models.RemoteSession.pending_bundle: True}))


# ---------- source-code components ----------

_KEY_FILE_NAMES = {
    "readme", "readme.md", "readme.txt", "readme.rst", "package.json",
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "go.mod",
    "cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts", "composer.json",
    "gemfile", "makefile", "dockerfile", "manifest.json", "pubspec.yaml",
}
_KEY_FILE_BYTES = 6_000
_KEY_FILE_MAX = 6


def _safe_member(name: str) -> str | None:
    name = name.replace("\\", "/").lstrip("/")
    parts = []
    for p in name.split("/"):
        if not p or p == ".":
            continue
        if p == "..":
            return None
        parts.append(p)
    return "/".join(parts) or None


def _extract_component_zip(tmp_zip: str, dest: str) -> None:
    """Extract a component archive into ``dest``, pruning noise dirs, skipping
    unsafe paths, and enforcing the session-upload caps."""
    total = files = 0
    with zipfile.ZipFile(tmp_zip) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = _safe_member(info.filename)
            if rel is None:
                raise HTTPException(400, f"unsafe path in zip: {info.filename!r}")
            segs = rel.split("/")
            if any(s in _NOISE_DIRS for s in segs[:-1]):
                continue
            if rel.startswith("__MACOSX/") or os.path.basename(rel) == ".DS_Store":
                continue
            files += 1
            if files > settings.session_upload_max_files:
                raise HTTPException(413, f"too many files (max {settings.session_upload_max_files})")
            out_path = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with zf.open(info) as src, open(out_path, "wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > settings.session_upload_max_total_bytes:
                        raise HTTPException(413, "component exceeds the size limit")
                    out.write(chunk)


def _summarize_tree(root: str) -> tuple[str, int, int]:
    """Return (summary_text, file_count, total_bytes) for an extracted tree."""
    from collections import Counter
    paths: list[str] = []
    by_dir: Counter = Counter()
    by_ext: Counter = Counter()
    n = total = 0
    for dp, _dn, fns in os.walk(root):
        for f in fns:
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                total += os.path.getsize(full)
            except OSError:
                continue
            n += 1
            if len(paths) < 60:
                paths.append(rel)
            top = rel.split("/", 1)[0] if "/" in rel else "(root)"
            by_dir[top] += 1
            ext = ("." + f.rsplit(".", 1)[-1].lower()) if "." in f else "(none)"
            by_ext[ext] += 1
    lines = [f"{n} files, {total} bytes"]
    lines.append("top-level dirs: " + ", ".join(f"{d} ({c})" for d, c in by_dir.most_common(20)))
    lines.append("extensions: " + ", ".join(f"{e} ({c})" for e, c in by_ext.most_common(15)))
    lines.append("sample paths:")
    lines += [f"  {p}" for p in sorted(paths)]
    return "\n".join(lines), n, total


def _key_files(root: str) -> list[tuple[str, str]]:
    picked: list[tuple[str, str]] = []
    for dp, _dn, fns in os.walk(root):
        depth = os.path.relpath(dp, root).count(os.sep)
        for f in fns:
            if len(picked) >= _KEY_FILE_MAX:
                return picked
            low = f.lower()
            is_root_md = depth == 0 and low.endswith(".md")
            if low in _KEY_FILE_NAMES or is_root_md:
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                try:
                    with open(full, "rb") as fh:
                        raw = fh.read(_KEY_FILE_BYTES)
                    picked.append((rel, raw.decode("utf-8", errors="replace")))
                except OSError:
                    continue
    return picked


@api.get("/{project_id}/components", response_model=list[schemas.ProductComponentOut])
def list_components(
    project_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_view_contents(p, viewer):
        raise HTTPException(403, "Not a member of this product")
    rows = (db.query(models.ProductComponent)
              .filter(models.ProductComponent.project_id == project_id)
              .order_by(models.ProductComponent.created_at).all())
    return [schemas.ProductComponentOut.model_validate(c) for c in rows]


@api.post("/{project_id}/components", response_model=list[schemas.ProductComponentOut], status_code=201)
async def upload_components(
    project_id: str,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Upload one or more source-code archives. Each .zip becomes a Component:
    its files are stored as product source (so scans can use them) and the AI
    identifies what the component is in the context of the product."""
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_view_contents(p, viewer):
        raise HTTPException(403, "Not a member of this product")

    existing = [
        {"name": c.name, "description": c.description}
        for c in db.query(models.ProductComponent)
                   .filter(models.ProductComponent.project_id == project_id).all()
    ]
    taken = {c["name"] for c in existing}
    created: list[models.ProductComponent] = []

    for upload in files:
        if not (upload.filename or "").lower().endswith(".zip"):
            raise HTTPException(400, f"{upload.filename!r}: only .zip archives are accepted here")
        base = os.path.splitext(os.path.basename(upload.filename))[0] or "component"
        workdir = tempfile.mkdtemp(prefix="comp-")
        tmp_zip = os.path.join(workdir, "_archive.zip")
        extract_dir = os.path.join(workdir, "x")
        os.makedirs(extract_dir, exist_ok=True)
        try:
            with open(tmp_zip, "wb") as fh:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            try:
                _extract_component_zip(tmp_zip, extract_dir)
            except zipfile.BadZipFile:
                raise HTTPException(400, f"{upload.filename!r} is not a valid zip file")
            os.unlink(tmp_zip)

            summary, n_files, n_bytes = _summarize_tree(extract_dir)
            if n_files == 0:
                raise HTTPException(400, f"{upload.filename!r} contains no usable files")
            key_files = _key_files(extract_dir)

            comp = models.ProductComponent(
                project_id=project_id, name=base, source_name=upload.filename or base,
                file_count=n_files, total_bytes=n_bytes, created_by=viewer.id,
            )
            db.add(comp)
            db.flush()  # need comp.id for the ProjectFile back-reference

            # store every file as product source, namespaced under the component
            for dp, _dn, fns in os.walk(extract_dir):
                for f in fns:
                    full = os.path.join(dp, f)
                    rel = os.path.relpath(full, extract_dir).replace(os.sep, "/")
                    with open(full, "rb") as src:
                        skey, sha, size = storage.put_stream(src)
                    import mimetypes
                    ct = mimetypes.guess_type(f)[0] or "application/octet-stream"
                    db.add(models.ProjectFile(
                        project_id=project_id, relpath=f"{base}/{rel}",
                        content_type=ct, sha256=sha, size_bytes=size,
                        storage_key=skey, uploaded_by=viewer.id, component_id=comp.id,
                    ))

            # AI: identify the component in the context of the product
            try:
                res = analyze_component(
                    product_name=p.name, product_desc=p.description or "",
                    existing=existing, archive_name=upload.filename or base,
                    tree_summary=summary, key_files=key_files,
                )
            except Exception as e:
                log.warning("component analysis failed for %s: %s", upload.filename, e)
                res = {"name": base, "description": "", "role": "",
                       "ai_rationale": f"(analysis unavailable: {e})"}

            name = (res.get("name") or base).strip()
            while name in taken:
                name = f"{name}-2"
            taken.add(name)
            comp.name = name[:255]
            comp.description = (res.get("description") or "")[:4000]
            comp.role = (res.get("role") or "")[:4000]
            comp.ai_rationale = (res.get("ai_rationale") or "")[:8000]
            existing.append({"name": comp.name, "description": comp.description})
            created.append(comp)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    _bump_sessions(db, project_id)
    db.commit()
    for c in created:
        db.refresh(c)
    log.info("created %d component(s) on product %s by %s", len(created), project_id, viewer.email)
    return [schemas.ProductComponentOut.model_validate(c) for c in created]


@api.delete("/{project_id}/components/{cid}", status_code=204)
def delete_component(
    project_id: str,
    cid: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_view_contents(p, viewer):
        raise HTTPException(403, "Not a member of this product")
    comp = db.get(models.ProductComponent, cid)
    if not comp or comp.project_id != project_id:
        raise HTTPException(404, "Component not found")
    pfs = (db.query(models.ProjectFile)
             .filter(models.ProjectFile.component_id == cid).all())
    keys = {pf.storage_key for pf in pfs}
    for pf in pfs:
        db.delete(pf)
    db.delete(comp)
    _bump_sessions(db, project_id)
    db.commit()
    _gc_blobs(db, keys)


class _MemberBody(__import__("pydantic").BaseModel):
    email: str


@api.patch("/{project_id}", response_model=schemas.ProjectDetail)
def api_update_project(
    project_id: str,
    body: schemas.ProjectUpdate,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_edit(p, viewer):
        raise HTTPException(403, "Only the owner or an admin can edit a product")
    payload = body.model_dump(exclude_unset=True)
    if "name" in payload and payload["name"] is not None:
        n = payload["name"].strip()
        if not n:
            raise HTTPException(400, "name cannot be empty")
        p.name = n[:255]
    if "description" in payload and payload["description"] is not None:
        p.description = payload["description"].strip()[:2000]
    from .teams import _apply_ai_pin
    _apply_ai_pin(p, payload)
    db.commit()
    db.refresh(p)
    return _detail(db, p, viewer)


@api.delete("/{project_id}", status_code=204)
def api_delete_project(
    project_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.Project, project_id)
    if not p:
        return
    assert_can_delete(viewer, p.created_by, label="product")
    # Detach explicitly rather than relying on ON DELETE SET NULL — the live
    # Postgres schema predates several of these FKs and the manual ALTERs did
    # not all carry the cascade clause.
    for model in (models.Report, models.Run, models.VulnScan, models.Harness):
        db.query(model).filter(model.project_id == p.id).update({"project_id": None})
    db.delete(p)
    db.commit()


@api.post("/{project_id}/merge", response_model=schemas.ProjectDetail)
def api_merge_project(
    project_id: str,
    body: schemas.ProjectMerge,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Move every FK pointing at the source project to the target, union
    members, then drop the source row.

    Admin-only — merging affects more than one product's membership and
    isn't something a single product owner should be able to trigger.
    """
    if viewer.role != models.Role.admin:
        raise HTTPException(403, "Only admins can merge products")
    if project_id == body.into_id:
        raise HTTPException(400, "Can't merge a product into itself")
    src = db.get(models.Project, project_id)
    if not src:
        raise HTTPException(404, "source product not found")
    dst = db.get(models.Project, body.into_id)
    if not dst:
        raise HTTPException(404, "target product not found")

    # 1. Re-target every direct FK.
    db.query(models.Report).filter(models.Report.project_id == src.id).update(
        {"project_id": dst.id}, synchronize_session=False
    )
    db.query(models.Run).filter(models.Run.project_id == src.id).update(
        {"project_id": dst.id}, synchronize_session=False
    )
    db.query(models.VulnScan).filter(models.VulnScan.project_id == src.id).update(
        {"project_id": dst.id}, synchronize_session=False
    )
    db.query(models.Harness).filter(models.Harness.project_id == src.id).update(
        {"project_id": dst.id}, synchronize_session=False
    )
    db.query(models.ProjectInvite).filter(models.ProjectInvite.project_id == src.id).update(
        {"project_id": dst.id}, synchronize_session=False
    )
    db.query(models.ProjectAccessRequest).filter(
        models.ProjectAccessRequest.project_id == src.id
    ).update({"project_id": dst.id}, synchronize_session=False)

    # 2. Union the membership. Use raw SQL via the join table so we don't
    # round-trip a list of users + risk duplicate inserts.
    dst_ids = {m.id for m in dst.members}
    for member in list(src.members):
        if member.id not in dst_ids:
            dst.members.append(member)
            dst_ids.add(member.id)
    # NB: the source row's CASCADE on project_members will clean up the
    # source-side join rows when we delete src below.

    # 3. Notify members of the source product about the merge so they can
    # find their data on the new product page.
    affected: set[str] = {src.created_by}
    for m in src.members:
        affected.add(m.id)
    affected.discard(viewer.id)  # don't notify the admin who clicked merge
    for uid in affected:
        db.add(models.Notification(
            user_id=uid,
            kind=models.NotificationKind.project_member_added,
            title=f"Product '{src.name}' was merged into '{dst.name}'",
            body=(
                f"Admin {viewer.email} merged the product. All reports, scans, "
                "and harnesses are now under the destination product."
            ),
            link=f"/products/{dst.id}",
            data={"merged_from": src.id, "merged_into": dst.id},
            actor_user_id=viewer.id,
        ))

    db.delete(src)
    db.commit()
    db.refresh(dst)
    return _detail(db, dst, viewer)


def _detail(db: Session, p: models.Project, viewer: models.User) -> schemas.ProjectDetail:
    """Build a ProjectDetail with viewer-relative flags. Note: ProjectOut
    already carries i_am_owner / i_am_member with default False, so we
    overwrite those keys in the dict before constructing ProjectDetail —
    otherwise the explicit kwargs collide with the model_dump."""
    show = _can_view_contents(p, viewer)
    base = schemas.ProjectOut.model_validate(p).model_dump()
    base["i_am_owner"] = _is_owner(p, viewer)
    base["i_am_member"] = _is_member(p, viewer)
    n, b = 0, 0
    if show:
        n, b = (db.query(func.count(models.ProjectFile.id),
                         func.coalesce(func.sum(models.ProjectFile.size_bytes), 0))
                  .filter(models.ProjectFile.project_id == p.id).one())
    return schemas.ProjectDetail(
        **base,
        members=(
            [schemas.UserMini.model_validate(u) for u in p.members] if show else []
        ),
        can_edit=_can_edit(p, viewer),
        file_count=int(n), file_bytes=int(b),
    )


def _scans_for_project(db: Session, project_id: str) -> list[models.VulnScan]:
    """Scans whose project_id matches OR whose source_session_id maps to a
    Run pinned to this project — same rule the scans-scope code uses, just
    inlined here so we don't depend on the viewer's permissions object."""
    direct = (
        db.query(models.VulnScan)
        .filter(models.VulnScan.project_id == project_id)
        .all()
    )
    via_run = (
        db.query(models.VulnScan)
        .join(models.Run,
              models.VulnScan.source_session_id == models.Run.session_id)
        .filter(models.Run.project_id == project_id)
        .all()
    )
    by_id: dict[str, models.VulnScan] = {s.id: s for s in direct}
    for s in via_run:
        by_id.setdefault(s.id, s)
    return list(by_id.values())


@api.get(
    "/{project_id}/findings/summary",
    response_model=schemas.ProductFindingsSummary,
)
def api_findings_summary(
    project_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_view_contents(p, viewer):
        raise HTTPException(403, "Not a member of that product")
    scans = _scans_for_project(db, p.id)
    out = schemas.ProductFindingsSummary(total=0, scan_count=len(scans))
    # Per scan, use whichever data exists. Scans with uploaded per-finding rows
    # are counted in full detail (severity / AI verdict / tags). Scans that only
    # carry the aggregate summary counts (e.g. imported from a spreadsheet, no
    # findings uploaded) fall back to those counts so the product total still
    # adds up instead of silently dropping them.
    for s in scans:
        rows = s.finding_rows
        if rows:
            out.total += len(rows)
            for f in rows:
                out.by_status[f.status.value] = out.by_status.get(f.status.value, 0) + 1
                out.by_ai_verdict[f.ai_verdict.value] = out.by_ai_verdict.get(f.ai_verdict.value, 0) + 1
                out.by_severity[f.severity.value] = out.by_severity.get(f.severity.value, 0) + 1
                for t in (f.tags or []):
                    out.by_tag[t] = out.by_tag.get(t, 0) + 1
        else:
            out.total += s.findings
            if s.tp:
                out.by_status["true_positive"] = out.by_status.get("true_positive", 0) + s.tp
            if s.fp:
                out.by_status["false_positive"] = out.by_status.get("false_positive", 0) + s.fp
            if s.untriaged:
                out.by_status["open"] = out.by_status.get("open", 0) + s.untriaged
            if s.sbp:
                out.by_tag["sbp"] = out.by_tag.get("sbp", 0) + s.sbp
    return out


def _scan_rank_map(scans: list[models.VulnScan]) -> dict[str, int]:
    """Same stable rank used in the product-scoped scans list."""
    ordered = sorted(scans, key=lambda s: s.created_at)
    return {s.id: i + 1 for i, s in enumerate(ordered)}


@api.get(
    "/{project_id}/findings",
    response_model=list[schemas.ProductFindingRow],
)
def api_findings_list(
    project_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_view_contents(p, viewer):
        raise HTTPException(403, "Not a member of that product")
    scans = _scans_for_project(db, p.id)
    ranks = _scan_rank_map(scans)

    out: list[schemas.ProductFindingRow] = []
    for s in scans:
        for f in s.finding_rows:
            row = schemas.ProductFindingRow.model_validate(f)
            row.scan_product = s.product or ""
            row.scan_target = s.scan_target or ""
            row.scan_rank = ranks.get(s.id, 0)
            out.append(row)
    out.sort(key=lambda r: r.created_at, reverse=True)
    return out


@api.get("/{project_id}/findings/export")
def api_findings_export(
    project_id: str,
    format: str = "csv",
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """CSV/JSON export of every finding under this product. Mirrors the
    list endpoint above plus a server-stamped filename."""
    import csv
    import io
    import json
    from fastapi.responses import Response

    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_view_contents(p, viewer):
        raise HTTPException(403, "Not a member of that product")
    scans = _scans_for_project(db, p.id)
    ranks = _scan_rank_map(scans)

    rows: list[dict] = []
    for s in scans:
        for f in s.finding_rows:
            rows.append({
                "scan_rank": ranks.get(s.id, 0),
                "scan_id": s.id,
                "scan_product": s.product or "",
                "scan_target": s.scan_target or "",
                "finding_id": f.id,
                "title": f.title,
                "severity": f.severity.value,
                "dev_verdict": f.status.value,
                "ai_verdict": f.ai_verdict.value,
                "ai_rationale": f.ai_rationale,
                "tags": ";".join(f.tags or []),
                "cwe": f.cwe,
                "cve": f.cve,
                "affected_component": f.affected_component,
                "triaged_by": f.triaged_by,
                "triaged_at": f.triaged_at.isoformat() if f.triaged_at else "",
                "assigned_to": f.assigned_to,
                "created_at": f.created_at.isoformat(),
            })
    # Newest first to match the list endpoint.
    rows.sort(key=lambda r: r["created_at"], reverse=True)

    safe_name = (p.name or "product").replace("/", "_").replace(" ", "_")[:80]
    if format == "json":
        return Response(
            content=json.dumps(rows, indent=2),
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}-findings.json"',
            },
        )

    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    else:
        # Empty product — still emit a sensible header so curl-ing it doesn't
        # return a zero-byte file with no schema.
        buf.write(
            "scan_rank,scan_id,scan_product,scan_target,finding_id,title,severity,"
            "dev_verdict,ai_verdict,ai_rationale,tags,cwe,cve,affected_component,"
            "triaged_by,triaged_at,assigned_to,created_at\n"
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}-findings.csv"',
        },
    )


@api.post("/{project_id}/members", response_model=schemas.ProjectDetail)
def api_add_member(
    project_id: str,
    body: _MemberBody,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_edit(p, viewer):
        raise HTTPException(403, "Only the owner or an admin can manage members")
    target = db.query(models.User).filter(models.User.email == body.email.strip().lower()).first()
    if not target:
        raise HTTPException(404, f"No user found with email {body.email!r}")
    if not _is_member(p, target):
        p.members.append(target)
        db.commit()
    return _detail(db, p, viewer)


@api.delete("/{project_id}/members/{user_id}", response_model=schemas.ProjectDetail)
def api_remove_member(
    project_id: str,
    user_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_edit(p, viewer):
        raise HTTPException(403, "Only the owner or an admin can manage members")
    u = db.get(models.User, user_id)
    if u and _is_member(p, u):
        p.members.remove(u)
        db.commit()
    return _detail(db, p, viewer)


# ---------- UI (cookie) ----------

@ui.get("", response_class=HTMLResponse)
def ui_list(request: Request, db: Session = Depends(get_db)):
    viewer = _require_user(request, db)
    projects = (
        db.query(models.Project).order_by(models.Project.created_at.desc()).all()
    )
    return templates.TemplateResponse(
        request, "projects_list.html",
        {"user": viewer, "projects": projects},
    )


@ui.get("/new", response_class=HTMLResponse)
def ui_new_form(request: Request, db: Session = Depends(get_db)):
    viewer = _require_user(request, db)
    return templates.TemplateResponse(request, "project_new.html", {"user": viewer})


@ui.post("/new")
def ui_new_submit(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    viewer = _require_user(request, db)
    name = name.strip()
    if not name:
        raise HTTPException(400, "Project name is required")
    p = models.Project(name=name, description=description, created_by=viewer.id)
    p.members.append(viewer)
    db.add(p)
    db.commit()
    db.refresh(p)
    log.info("created project id=%s name=%r by=%s", p.id, p.name, viewer.email)
    return RedirectResponse(f"/ui/projects/{p.id}", status_code=303)


@ui.get("/{project_id}", response_class=HTMLResponse)
def ui_detail(project_id: str, request: Request, db: Session = Depends(get_db)):
    viewer = _require_user(request, db)
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    # Anyone can see the project's name + description; only members/owners
    # see the membership + contents.
    show = _can_view_contents(p, viewer)
    can_edit = _can_edit(p, viewer)
    runs = direct_reports = direct_scans = []
    members: list = []
    if show:
        members = list(p.members)
        runs = (
            db.query(models.Run).filter(models.Run.project_id == p.id)
            .order_by(models.Run.created_at.desc()).all()
        )
        direct_reports = (
            db.query(models.Report).filter(models.Report.project_id == p.id)
            .order_by(models.Report.created_at.desc()).all()
        )
        direct_scans = (
            db.query(models.VulnScan).filter(models.VulnScan.project_id == p.id)
            .order_by(models.VulnScan.created_at.desc()).all()
        )
    return templates.TemplateResponse(
        request, "project_detail.html",
        {
            "user": viewer, "project": p, "members": members,
            "runs": runs, "direct_reports": direct_reports,
            "direct_scans": direct_scans,
            "can_edit": can_edit,
            "show_contents": show,
        },
    )


@ui.post("/{project_id}/edit")
def ui_edit(
    project_id: str,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    viewer = _require_user(request, db)
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_edit(p, viewer):
        raise HTTPException(403, "Not allowed")
    p.name = name.strip() or p.name
    p.description = description
    db.commit()
    return RedirectResponse(f"/ui/projects/{p.id}", status_code=303)


@ui.post("/{project_id}/members/add")
def ui_add_member(
    project_id: str,
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    viewer = _require_user(request, db)
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_edit(p, viewer):
        raise HTTPException(403, "Not allowed")
    email = email.strip().lower()
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user:
        # Render the project page with an inline error.
        return templates.TemplateResponse(
            request, "project_detail.html",
            {
                "user": viewer, "project": p, "members": p.members,
                "runs": db.query(models.Run).filter(models.Run.project_id == p.id).all(),
                "direct_reports": db.query(models.Report).filter(
                    models.Report.project_id == p.id).all(),
                "direct_scans": db.query(models.VulnScan).filter(
                    models.VulnScan.project_id == p.id).all(),
                "can_edit": _can_edit(p, viewer),
                "error": f"No user found with email {email!r}.",
            },
            status_code=404,
        )
    if not _is_member(p, user):
        p.members.append(user)
        db.commit()
        log.info("added user=%s to project=%s by=%s", user.email, p.id, viewer.email)
    return RedirectResponse(f"/ui/projects/{p.id}", status_code=303)


@ui.post("/{project_id}/members/{user_id}/remove")
def ui_remove_member(
    project_id: str, user_id: str, request: Request, db: Session = Depends(get_db)
):
    viewer = _require_user(request, db)
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_edit(p, viewer):
        raise HTTPException(403, "Not allowed")
    u = db.get(models.User, user_id)
    if u and _is_member(p, u):
        p.members.remove(u)
        db.commit()
        log.info("removed user=%s from project=%s by=%s", u.email, p.id, viewer.email)
    return RedirectResponse(f"/ui/projects/{p.id}", status_code=303)


@ui.post("/{project_id}/delete")
def ui_delete(project_id: str, request: Request, db: Session = Depends(get_db)):
    viewer = _require_user(request, db)
    p = db.get(models.Project, project_id)
    if not p:
        return RedirectResponse("/ui/projects", status_code=303)
    if not (viewer.role == models.Role.admin or p.created_by == viewer.id):
        raise HTTPException(403, "Only the creator (or an admin) can delete a project")
    db.delete(p)
    db.commit()
    return RedirectResponse("/ui/projects", status_code=303)
