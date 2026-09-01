"""Project access requests + a tiny notification helper.

A user with no membership in a project can request access. The project
creator (and any admin) sees the request as a Notification and on the
project detail page; approving it adds the requester as a member and
emits a follow-up notification back to them. Denial works the same way
with no membership change.

Endpoints:
  POST   /project_requests              body={project_id, reason}
  GET    /project_requests?incoming=1   pending requests I can approve
  GET    /project_requests?mine=1       requests I have made
  POST   /project_requests/{id}/approve body={reason}
  POST   /project_requests/{id}/deny    body={reason}
  POST   /project_requests/{id}/cancel  requester withdraws
"""
from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db

log = logging.getLogger("irs.project_requests")
router = APIRouter(prefix="/project_requests", tags=["project_requests"])


# ---------------- helpers ----------------

def _is_owner(p: models.Project, u: models.User) -> bool:
    return p.created_by == u.id


def _is_member(p: models.Project, u: models.User) -> bool:
    return any(m.id == u.id for m in p.members)


def _can_decide(p: models.Project, u: models.User) -> bool:
    return u.role == models.Role.admin or _is_owner(p, u)


def _notify(
    db: Session,
    *,
    user_id: str,
    kind: models.NotificationKind,
    title: str,
    body: str = "",
    link: str = "",
    data: dict | None = None,
    actor_user_id: str | None = None,
) -> models.Notification:
    """Create + persist a Notification row. Caller is expected to commit."""
    n = models.Notification(
        user_id=user_id,
        kind=kind,
        title=title[:255],
        body=body,
        link=link[:512],
        data=data,
        actor_user_id=actor_user_id,
    )
    db.add(n)
    return n


def _to_out(r: models.ProjectAccessRequest, db: Session) -> schemas.ProjectAccessRequestOut:
    proj = db.get(models.Project, r.project_id)
    user = db.get(models.User, r.user_id)
    o = schemas.ProjectAccessRequestOut.model_validate(r)
    o.project_name = proj.name if proj else ""
    o.user_email = user.email if user else ""
    if r.import_id:
        imp = db.get(models.FolderImport, r.import_id)
        if imp:
            o.import_file_count = imp.file_count
            o.import_status = imp.status
    return o


# ---------------- create ----------------

@router.post(
    "", response_model=schemas.ProjectAccessRequestOut, status_code=201,
)
def request_access(
    body: schemas.ProjectAccessRequestCreate,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    proj = db.get(models.Project, body.project_id)
    if not proj:
        raise HTTPException(404, "Project not found")
    if _is_member(proj, viewer) or _is_owner(proj, viewer):
        raise HTTPException(400, "You're already a member of that project")

    # One pending request per (project, user) — re-use it instead of
    # spamming the owner with duplicates.
    existing = (
        db.query(models.ProjectAccessRequest)
        .filter(
            models.ProjectAccessRequest.project_id == proj.id,
            models.ProjectAccessRequest.user_id == viewer.id,
            models.ProjectAccessRequest.status == models.AccessRequestStatus.pending,
        )
        .first()
    )
    if existing:
        if body.reason and body.reason != existing.reason:
            existing.reason = body.reason
        if body.import_id and body.import_id != existing.import_id:
            # Re-check ownership of the attached import.
            imp = db.get(models.FolderImport, body.import_id)
            if not imp or imp.user_id != viewer.id:
                raise HTTPException(400, "import_id must point to a folder import you own")
            existing.import_id = imp.id
        db.commit()
        return _to_out(existing, db)

    import_id: str | None = None
    if body.import_id:
        imp = db.get(models.FolderImport, body.import_id)
        if not imp or imp.user_id != viewer.id:
            raise HTTPException(400, "import_id must point to a folder import you own")
        if imp.status not in (
            models.ImportStatus.staged, models.ImportStatus.planned,
            models.ImportStatus.error,
        ):
            raise HTTPException(
                400,
                f"can't attach import in status {imp.status.value!r} — "
                "only staged/planned imports can be attached",
            )
        import_id = imp.id

    req = models.ProjectAccessRequest(
        project_id=proj.id,
        user_id=viewer.id,
        reason=body.reason or "",
        import_id=import_id,
    )
    db.add(req)
    db.flush()  # need req.id for the notification link

    # Notify the owner + every admin (admins can also decide).
    notif_link = f"/projects/{proj.id}?req={req.id}"
    recipients: set[str] = set()
    if proj.created_by:
        recipients.add(proj.created_by)
    for admin in (
        db.query(models.User).filter(models.User.role == models.Role.admin).all()
    ):
        recipients.add(admin.id)
    # Don't ping the requester even if they're an admin.
    recipients.discard(viewer.id)
    for uid in recipients:
        _notify(
            db,
            user_id=uid,
            kind=models.NotificationKind.access_request,
            title=f"{viewer.email} wants to join '{proj.name}'",
            body=body.reason or "",
            link=notif_link,
            data={"project_id": proj.id, "request_id": req.id},
            actor_user_id=viewer.id,
        )
    db.commit()
    db.refresh(req)
    log.info(
        "access_request created id=%s proj=%s requester=%s notified=%d",
        req.id, proj.id, viewer.email, len(recipients),
    )
    return _to_out(req, db)


# ---------------- list ----------------

@router.get("", response_model=list[schemas.ProjectAccessRequestOut])
def list_requests(
    incoming: bool = False,
    mine: bool = False,
    project_id: str | None = None,
    status_filter: models.AccessRequestStatus | None = None,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    q = (
        db.query(models.ProjectAccessRequest)
        .order_by(models.ProjectAccessRequest.created_at.desc())
    )
    if project_id:
        q = q.filter(models.ProjectAccessRequest.project_id == project_id)
    if status_filter:
        q = q.filter(models.ProjectAccessRequest.status == status_filter)
    if mine:
        q = q.filter(models.ProjectAccessRequest.user_id == viewer.id)
    if incoming:
        if viewer.role == models.Role.admin:
            pass  # admins see all
        else:
            # Projects this user owns (created_by == viewer.id).
            owned_ids = [
                p.id for p in db.query(models.Project).filter(
                    models.Project.created_by == viewer.id
                ).all()
            ]
            if not owned_ids:
                return []
            q = q.filter(models.ProjectAccessRequest.project_id.in_(owned_ids))
    elif not mine:
        # Default to "things you can decide on" so the empty filter is useful.
        if viewer.role != models.Role.admin:
            owned_ids = [
                p.id for p in db.query(models.Project).filter(
                    models.Project.created_by == viewer.id
                ).all()
            ]
            mine_q = (
                models.ProjectAccessRequest.user_id == viewer.id
            )
            if owned_ids:
                q = q.filter(
                    (models.ProjectAccessRequest.project_id.in_(owned_ids)) | mine_q
                )
            else:
                q = q.filter(mine_q)
    rows = q.limit(200).all()
    return [_to_out(r, db) for r in rows]


# ---------------- approve / deny / cancel ----------------

def _load_pending(db: Session, viewer: models.User, req_id: str) -> tuple[models.ProjectAccessRequest, models.Project]:
    req = db.get(models.ProjectAccessRequest, req_id)
    if not req:
        raise HTTPException(404, "Not found")
    proj = db.get(models.Project, req.project_id)
    if not proj:
        raise HTTPException(404, "Project gone")
    if req.status != models.AccessRequestStatus.pending:
        raise HTTPException(409, f"request already {req.status.value}")
    return req, proj


@router.post("/{req_id}/approve", response_model=schemas.ProjectAccessRequestOut)
def approve_request(
    req_id: str,
    body: schemas.ProjectAccessRequestDecision,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    req, proj = _load_pending(db, viewer, req_id)
    if not _can_decide(proj, viewer):
        raise HTTPException(403, "Only the project owner or an admin can decide")

    requester = db.get(models.User, req.user_id)
    if requester and not _is_member(proj, requester):
        proj.members.append(requester)

    # Apply an attached import (if any) onto THIS project. We force the plan's
    # project to the project being approved so the importer doesn't have to
    # re-confirm. Membership has just been granted above, so permission checks
    # pass.
    imported_summary = ""
    if req.import_id and requester is not None:
        from .imports import apply_plan, force_plan_project, wipe_staging
        imp = db.get(models.FolderImport, req.import_id)
        if imp and imp.user_id == requester.id:
            if imp.status == models.ImportStatus.planned and isinstance(imp.plan_json, dict):
                plan = force_plan_project(imp.plan_json, proj.id)
                try:
                    apply_plan(db, requester, imp, plan, proj.id)
                    imported_summary = (
                        f" (also imported their attached folder of "
                        f"{imp.file_count} files)"
                    )
                except Exception as e:
                    log.exception("attached-import apply failed req=%s imp=%s", req.id, imp.id)
                    imp.status = models.ImportStatus.error
                    imp.error_message = str(e)[:1000]
            elif imp.status == models.ImportStatus.staged:
                # No plan yet — leave the import alone. Owner approved the
                # access; the user can run the planner + confirm themselves
                # now that they're a member.
                imported_summary = (
                    " (their attached folder is still staged — they'll need to "
                    "open it and run the planner to import it)"
                )

    req.status = models.AccessRequestStatus.approved
    req.decided_by = viewer.id
    req.decided_at = dt.datetime.now(dt.timezone.utc)
    req.decision_reason = body.reason or ""

    _notify(
        db,
        user_id=req.user_id,
        kind=models.NotificationKind.access_approved,
        title=f"Approved: you're now a member of '{proj.name}'",
        body=(body.reason or "") + imported_summary,
        link=f"/projects/{proj.id}",
        data={"project_id": proj.id, "request_id": req.id},
        actor_user_id=viewer.id,
    )
    db.commit()
    db.refresh(req)
    # Wipe staging *after* commit so the row state is durable first.
    if req.import_id:
        imp_after = db.get(models.FolderImport, req.import_id)
        if imp_after and imp_after.status == models.ImportStatus.applied:
            from .imports import wipe_staging
            wipe_staging(imp_after)
    log.info("access_request approved id=%s by=%s", req.id, viewer.email)
    return _to_out(req, db)


@router.post("/{req_id}/deny", response_model=schemas.ProjectAccessRequestOut)
def deny_request(
    req_id: str,
    body: schemas.ProjectAccessRequestDecision,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    req, proj = _load_pending(db, viewer, req_id)
    if not _can_decide(proj, viewer):
        raise HTTPException(403, "Only the project owner or an admin can decide")

    req.status = models.AccessRequestStatus.denied
    req.decided_by = viewer.id
    req.decided_at = dt.datetime.now(dt.timezone.utc)
    req.decision_reason = body.reason or ""

    _notify(
        db,
        user_id=req.user_id,
        kind=models.NotificationKind.access_denied,
        title=f"Denied: access request for '{proj.name}'",
        body=body.reason or "",
        link=f"/projects",
        data={"project_id": proj.id, "request_id": req.id},
        actor_user_id=viewer.id,
    )
    db.commit()
    db.refresh(req)
    log.info("access_request denied id=%s by=%s", req.id, viewer.email)
    return _to_out(req, db)


@router.post("/{req_id}/cancel", response_model=schemas.ProjectAccessRequestOut)
def cancel_request(
    req_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    req = db.get(models.ProjectAccessRequest, req_id)
    if not req:
        raise HTTPException(404, "Not found")
    if req.user_id != viewer.id and viewer.role != models.Role.admin:
        raise HTTPException(403, "Only the requester can cancel")
    if req.status != models.AccessRequestStatus.pending:
        raise HTTPException(409, f"request already {req.status.value}")
    req.status = models.AccessRequestStatus.cancelled
    req.decided_at = dt.datetime.now(dt.timezone.utc)
    req.decided_by = viewer.id
    db.commit()
    db.refresh(req)
    return _to_out(req, db)
