"""Shareable project invite links.

Two surfaces:

  /projects/{id}/invites          (owner + admin) — mint, list, revoke
  /invites/{token}                (public) — preview the project so the
                                  signup / login page can display
                                  "Join 'Acme Gateway'" before auth.
  /invites/{token}/redeem         (auth) — add caller as project member,
                                  bump uses_count, idempotent if already
                                  a member.

Self-service registration also redeems automatically: passing
?invite=<token> to /ui/register flows the token through the form and
the new account gets added on the first request (see registration.py).
"""
from __future__ import annotations

import datetime as dt
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db

log = logging.getLogger("irs.invites")
api = APIRouter(tags=["invites"])


# ---------------- helpers ----------------

def _gen_token() -> str:
    # 32 bytes = ~43 chars urlsafe base64. Plenty of entropy, fits in the
    # 64-char column with room to spare.
    return secrets.token_urlsafe(32)


def _is_owner(p: models.Project, u: models.User) -> bool:
    return p.created_by == u.id


def _can_manage(p: models.Project, u: models.User) -> bool:
    return u.role == models.Role.admin or _is_owner(p, u)


def _is_member(p: models.Project, u: models.User) -> bool:
    return any(m.id == u.id for m in p.members)


def _invite_status(inv: models.ProjectInvite) -> str:
    if inv.revoked_at is not None:
        return "revoked"
    if inv.expires_at is not None and inv.expires_at < dt.datetime.now(dt.timezone.utc):
        return "expired"
    if inv.max_uses is not None and inv.uses_count >= inv.max_uses:
        return "used_up"
    return "active"


def _to_out(inv: models.ProjectInvite) -> schemas.ProjectInviteOut:
    return schemas.ProjectInviteOut.model_validate(inv)


# ---------------- owner CRUD (mounted under /projects/{id}/invites) ----------------

@api.post(
    "/projects/{project_id}/invites",
    response_model=schemas.ProjectInviteOut, status_code=201,
)
def create_invite(
    project_id: str,
    body: schemas.ProjectInviteCreate,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Project not found")
    if not _can_manage(p, viewer):
        raise HTTPException(403, "Only the project owner or an admin can mint invites")

    expires_at = None
    if body.expires_in_days is not None and body.expires_in_days > 0:
        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=body.expires_in_days)
    max_uses = body.max_uses if (body.max_uses is None or body.max_uses > 0) else None

    inv = models.ProjectInvite(
        project_id=p.id,
        token=_gen_token(),
        created_by=viewer.id,
        expires_at=expires_at,
        max_uses=max_uses,
        note=(body.note or "")[:255],
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    log.info("minted invite id=%s proj=%s by=%s", inv.id, p.id, viewer.email)
    return _to_out(inv)


@api.get(
    "/projects/{project_id}/invites",
    response_model=list[schemas.ProjectInviteOut],
)
def list_invites(
    project_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Project not found")
    if not _can_manage(p, viewer):
        raise HTTPException(403, "Only the project owner or an admin can see invites")
    rows = (
        db.query(models.ProjectInvite)
        .filter(models.ProjectInvite.project_id == p.id)
        .order_by(models.ProjectInvite.created_at.desc())
        .all()
    )
    return [_to_out(r) for r in rows]


@api.delete(
    "/projects/{project_id}/invites/{invite_id}",
    response_model=schemas.ProjectInviteOut,
)
def revoke_invite(
    project_id: str,
    invite_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.Project, project_id)
    if not p:
        raise HTTPException(404, "Project not found")
    if not _can_manage(p, viewer):
        raise HTTPException(403, "Only the project owner or an admin can revoke invites")
    inv = db.get(models.ProjectInvite, invite_id)
    if not inv or inv.project_id != p.id:
        raise HTTPException(404, "Invite not found")
    if inv.revoked_at is None:
        inv.revoked_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
        db.refresh(inv)
    return _to_out(inv)


# ---------------- public preview / redeem (mounted under /invites/{token}) ----------------

@api.get("/invites/{token}", response_model=schemas.ProjectInvitePreview)
def preview_invite(token: str, db: Session = Depends(get_db)):
    """Unauthenticated. Bit of info-leakage on project name+description, but
    knowing a 32-byte URL-safe token is itself the authorisation."""
    inv = (
        db.query(models.ProjectInvite)
        .filter(models.ProjectInvite.token == token)
        .first()
    )
    if not inv:
        # Don't 404 — that distinguishes "wrong token" from "expired". Return
        # a synthetic preview with status=unknown so the UI can render a
        # uniform "invalid link" page.
        return schemas.ProjectInvitePreview(
            project_id="", project_name="", status="unknown",
        )
    proj = db.get(models.Project, inv.project_id)
    inviter = db.get(models.User, inv.created_by)
    return schemas.ProjectInvitePreview(
        project_id=inv.project_id,
        project_name=proj.name if proj else "(missing)",
        project_description=proj.description if proj else "",
        inviter_email=inviter.email if inviter else "",
        expires_at=inv.expires_at,
        status=_invite_status(inv),
    )


@api.post("/invites/{token}/redeem")
def redeem_invite(
    token: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Add the caller to the invite's project. Idempotent — calling on a
    project the user is already in is a no-op (still 200)."""
    inv = (
        db.query(models.ProjectInvite)
        .filter(models.ProjectInvite.token == token)
        .first()
    )
    if not inv:
        raise HTTPException(404, "Invite not found")
    status = _invite_status(inv)
    if status != "active":
        raise HTTPException(410, f"invite {status}")
    proj = db.get(models.Project, inv.project_id)
    if not proj:
        raise HTTPException(404, "Project gone")

    already = _is_member(proj, viewer) or _is_owner(proj, viewer)
    if not already:
        proj.members.append(viewer)
        inv.uses_count += 1
        # Notify the inviter so they know the link did its job.
        if inv.created_by != viewer.id:
            db.add(models.Notification(
                user_id=inv.created_by,
                kind=models.NotificationKind.project_member_added,
                title=f"{viewer.email} joined '{proj.name}' via invite",
                body=f"Invite uses: {inv.uses_count}"
                     + (f" / {inv.max_uses}" if inv.max_uses else ""),
                link=f"/projects/{proj.id}",
                data={"project_id": proj.id, "invite_id": inv.id},
                actor_user_id=viewer.id,
            ))
        db.commit()
        log.info("invite redeemed token=%s... proj=%s user=%s",
                 token[:8], proj.id, viewer.email)
    return {
        "ok": True,
        "already_member": already,
        "project_id": proj.id,
        "project_name": proj.name,
    }
