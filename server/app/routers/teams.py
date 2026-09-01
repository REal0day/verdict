from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db
from ..models import Role
from ..permissions import require_role

router = APIRouter(prefix="/teams", tags=["teams"])


def _team_with_count(db: Session, t: models.Team) -> schemas.TeamOut:
    n = (
        db.query(func.count(models.User.id))
        .filter(models.User.team_id == t.id)
        .scalar()
    ) or 0
    return schemas.TeamOut(id=t.id, name=t.name, member_count=int(n))


@router.post("", response_model=schemas.TeamOut, status_code=201)
def create_team(
    body: schemas.TeamCreate,
    db: Session = Depends(get_db),
    actor: models.User = Depends(get_current_user),
):
    require_role(actor, Role.admin)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Team name is required")
    if db.query(models.Team).filter(models.Team.name == name).first():
        raise HTTPException(409, "Team exists")
    t = models.Team(name=name)
    db.add(t)
    db.commit()
    db.refresh(t)
    return _team_with_count(db, t)


@router.get("", response_model=list[schemas.TeamOut])
def list_teams(
    db: Session = Depends(get_db), actor: models.User = Depends(get_current_user)
):
    require_role(actor, Role.admin, Role.manager)
    teams = db.query(models.Team).order_by(models.Team.created_at.desc()).all()
    return [_team_with_count(db, t) for t in teams]


@router.patch("/{team_id}", response_model=schemas.TeamOut)
def update_team(
    team_id: str,
    body: schemas.TeamUpdate,
    db: Session = Depends(get_db),
    actor: models.User = Depends(get_current_user),
):
    require_role(actor, Role.admin)
    t = db.get(models.Team, team_id)
    if not t:
        raise HTTPException(404, "Team not found")
    payload = body.model_dump(exclude_unset=True)
    if "name" in payload and payload["name"]:
        new_name = payload["name"].strip()
        if new_name != t.name:
            if db.query(models.Team).filter(models.Team.name == new_name).first():
                raise HTTPException(409, "Team name already exists")
            t.name = new_name
    db.commit()
    db.refresh(t)
    return _team_with_count(db, t)


@router.delete("/{team_id}", status_code=204)
def delete_team(
    team_id: str,
    db: Session = Depends(get_db),
    actor: models.User = Depends(get_current_user),
):
    require_role(actor, Role.admin)
    t = db.get(models.Team, team_id)
    if not t:
        return
    # Unassign any users currently on this team so we don't leave a dangling FK.
    (
        db.query(models.User)
        .filter(models.User.team_id == t.id)
        .update({models.User.team_id: None})
    )
    db.delete(t)
    db.commit()
