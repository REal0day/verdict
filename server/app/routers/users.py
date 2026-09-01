from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user, hash_password
from ..database import get_db
from ..models import Role
from ..permissions import require_role

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=schemas.UserOut)
def create_user(
    body: schemas.UserCreate,
    db: Session = Depends(get_db),
    actor: models.User = Depends(get_current_user),
):
    require_role(actor, Role.admin)
    if db.query(models.User).filter(models.User.email == body.email).first():
        raise HTTPException(409, "Email already exists")
    import datetime as _dt
    u = models.User(
        email=body.email,
        password_hash=hash_password(body.password),
        role=body.role,
        team_id=body.team_id,
        # Admin-provisioned users skip the onboarding wizard — the admin
        # already placed them in the right projects.
        onboarded_at=_dt.datetime.now(_dt.timezone.utc),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@router.get("", response_model=list[schemas.UserOut])
def list_users(
    db: Session = Depends(get_db), actor: models.User = Depends(get_current_user)
):
    require_role(actor, Role.admin, Role.manager)
    q = db.query(models.User)
    if actor.role == Role.manager:
        q = q.filter(models.User.team_id == actor.team_id)
    return q.all()


@router.patch("/{user_id}/team", response_model=schemas.UserOut)
def assign_team(
    user_id: str,
    team_id: str,
    db: Session = Depends(get_db),
    actor: models.User = Depends(get_current_user),
):
    require_role(actor, Role.admin)
    u = db.get(models.User, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    u.team_id = team_id
    db.commit()
    db.refresh(u)
    return u


@router.patch("/{user_id}", response_model=schemas.UserOut)
def update_user(
    user_id: str,
    body: schemas.UserUpdate,
    db: Session = Depends(get_db),
    actor: models.User = Depends(get_current_user),
):
    require_role(actor, Role.admin)
    u = db.get(models.User, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    payload = body.model_dump(exclude_unset=True)

    if "email" in payload and payload["email"] is not None:
        new_email = payload["email"].lower()
        if new_email != u.email:
            if db.query(models.User).filter(models.User.email == new_email).first():
                raise HTTPException(409, "Email already exists")
            u.email = new_email

    if "role" in payload and payload["role"] is not None:
        # Don't let an admin demote themselves into a no-admin state — that
        # would lock the system. Belt-and-braces: ensure at least one admin
        # remains after the change.
        new_role = payload["role"]
        if u.id == actor.id and new_role != Role.admin:
            other_admins = (
                db.query(models.User)
                .filter(models.User.role == Role.admin, models.User.id != u.id)
                .count()
            )
            if other_admins == 0:
                raise HTTPException(400, "Refusing to demote the last admin")
        u.role = new_role

    if "team_id" in payload:
        # explicit null clears; explicit value sets after verifying it exists
        new_team = payload["team_id"]
        if new_team in (None, ""):
            u.team_id = None
        else:
            if not db.get(models.Team, new_team):
                raise HTTPException(400, "Team not found")
            u.team_id = new_team

    db.commit()
    db.refresh(u)
    return u


class _UserProjectsBody(__import__("pydantic").BaseModel):
    project_ids: list[str]


@router.get("/{user_id}/projects", response_model=list[str])
def get_user_projects(
    user_id: str,
    db: Session = Depends(get_db),
    actor: models.User = Depends(get_current_user),
):
    """Project ids the user is a member of. Admin only."""
    require_role(actor, Role.admin)
    u = db.get(models.User, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    return [p.id for p in u.projects]


@router.put("/{user_id}/projects", response_model=list[str])
def set_user_projects(
    user_id: str,
    body: _UserProjectsBody,
    db: Session = Depends(get_db),
    actor: models.User = Depends(get_current_user),
):
    """Replace the user's project membership set. Admin only.

    Any project id in `project_ids` that doesn't exist is silently
    dropped (so a stale client doesn't 400). Returns the resulting list
    of project ids.
    """
    require_role(actor, Role.admin)
    u = db.get(models.User, user_id)
    if not u:
        raise HTTPException(404, "User not found")

    target_ids = {pid for pid in body.project_ids if pid}
    # Resolve to real Project rows; drop ids that don't exist.
    target_projects = (
        db.query(models.Project)
        .filter(models.Project.id.in_(target_ids))
        .all()
    ) if target_ids else []

    current_ids = {p.id for p in u.projects}
    new_ids = {p.id for p in target_projects}

    # Add missing.
    for p in target_projects:
        if p.id not in current_ids:
            p.members.append(u)
    # Remove extras. Preserve the project's *creator* membership — a
    # project's owner must remain a member, even via this admin path.
    for p in list(u.projects):
        if p.id not in new_ids and p.created_by != u.id:
            p.members.remove(u)

    db.commit()
    db.refresh(u)
    return [p.id for p in u.projects]


@router.post("/{user_id}/reset-password", status_code=204)
def reset_password(
    user_id: str,
    body: schemas.PasswordReset,
    db: Session = Depends(get_db),
    actor: models.User = Depends(get_current_user),
):
    require_role(actor, Role.admin)
    if len(body.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    u = db.get(models.User, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    u.password_hash = hash_password(body.new_password)
    db.commit()
