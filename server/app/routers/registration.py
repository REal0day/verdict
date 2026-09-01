"""Self-service signup, domain-gated, no email verification.

Flow:
  GET  /ui/register   -> form
  POST /ui/register   -> validate domain + password, create User, log in,
                         redirect to /.

Disabled unless IRS_SIGNUP_ENABLED=1. Allowed email domains are configured
via IRS_SIGNUP_ALLOWED_DOMAINS (comma-separated, no @).
"""
from __future__ import annotations

import logging

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import models
from ..auth import create_access_token, hash_password
from ..config import settings
from ..database import get_db

log = logging.getLogger("irs.registration")
router = APIRouter(tags=["registration"])
templates = Jinja2Templates(directory="app/templates")


def _allowed_domains() -> set[str]:
    return {
        d.strip().lower().lstrip("@")
        for d in (settings.signup_allowed_domains or "").split(",")
        if d.strip()
    }


def _signup_guard() -> None:
    if not settings.signup_enabled:
        raise HTTPException(404, "Signup is disabled on this server.")


def _render_form(
    request: Request,
    *,
    error: str | None = None,
    status: int = 200,
    invite_token: str | None = None,
    invite_preview=None,
):
    return templates.TemplateResponse(
        request,
        "register.html",
        {
            "allowed_domains": sorted(_allowed_domains()),
            "error": error,
            "invite_token": invite_token,
            "invite_preview": invite_preview,
        },
        status_code=status,
    )


def _load_invite_preview(db: Session, token: str | None):
    """Best-effort preview lookup. Returns None for no token, otherwise a
    SimpleNamespace shaped like ProjectInvitePreview so the Jinja template
    can use dot-access."""
    if not token:
        return None
    from .invites import preview_invite
    return preview_invite(token, db)


@router.get("/ui/register", response_class=HTMLResponse)
def register_form(
    request: Request,
    invite: str | None = None,
    db: Session = Depends(get_db),
):
    _signup_guard()
    return _render_form(
        request,
        invite_token=invite,
        invite_preview=_load_invite_preview(db, invite),
    )


@router.post("/ui/register")
def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    invite: str = Form(""),
    db: Session = Depends(get_db),
):
    _signup_guard()

    invite_token = invite.strip() or None
    invite_preview = _load_invite_preview(db, invite_token)

    try:
        valid = validate_email(email, check_deliverability=False)
        email_norm = valid.normalized.lower()
    except EmailNotValidError as e:
        return _render_form(
            request, error=f"Invalid email: {e}", status=400,
            invite_token=invite_token, invite_preview=invite_preview,
        )

    domain = email_norm.rsplit("@", 1)[-1]
    allowed = _allowed_domains()
    if allowed and domain not in allowed:
        return _render_form(
            request,
            error=f"Email domain '{domain}' is not allowed to register here.",
            status=403,
            invite_token=invite_token, invite_preview=invite_preview,
        )

    if len(password) < 8:
        return _render_form(
            request, error="Password must be at least 8 characters.", status=400,
            invite_token=invite_token, invite_preview=invite_preview,
        )

    if db.query(models.User).filter(models.User.email == email_norm).first():
        # Tell them straight — without email verification there's no enumeration
        # benefit to being vague, and a silent no-op would be confusing.
        return _render_form(
            request,
            error="An account with that email already exists. Sign in instead.",
            status=409,
            invite_token=invite_token, invite_preview=invite_preview,
        )

    user = models.User(
        email=email_norm,
        password_hash=hash_password(password),
        role=models.Role.admin,
        team_id=None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log.info("created user email=%s id=%s", user.email, user.id)

    # Auto-redeem an invite if one was passed. We replay the redeem-invite
    # logic inline here rather than via HTTP because we don't have a JWT yet.
    joined_project: str | None = None
    if invite_token:
        from .invites import _invite_status, _is_member, _is_owner
        inv = (
            db.query(models.ProjectInvite)
            .filter(models.ProjectInvite.token == invite_token)
            .first()
        )
        if inv and _invite_status(inv) == "active":
            proj = db.get(models.Project, inv.project_id)
            if proj and not (_is_member(proj, user) or _is_owner(proj, user)):
                proj.members.append(user)
                inv.uses_count += 1
                if inv.created_by != user.id:
                    db.add(models.Notification(
                        user_id=inv.created_by,
                        kind=models.NotificationKind.project_member_added,
                        title=f"{user.email} joined '{proj.name}' via invite",
                        body=f"Invite uses: {inv.uses_count}"
                             + (f" / {inv.max_uses}" if inv.max_uses else ""),
                        link=f"/projects/{proj.id}",
                        data={"project_id": proj.id, "invite_id": inv.id},
                        actor_user_id=user.id,
                    ))
                # New invite-redeeming users skip onboarding step 1 (they
                # already have a project) — but still see the wizard.
                db.commit()
                joined_project = proj.id
                log.info(
                    "invite-on-signup redeemed token=%s... user=%s proj=%s",
                    invite_token[:8], user.email, proj.id,
                )

    target = "/app/welcome"
    if joined_project:
        target = f"/app/projects/{joined_project}"
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(
        "irs_token", create_access_token(user.id), httponly=True, samesite="lax"
    )
    return resp
