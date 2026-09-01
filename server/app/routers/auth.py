from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import (
    create_access_token, get_current_user, hash_password, verify_password,
)
from ..database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/token", response_model=schemas.Token)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == form.username).first()
    if not user or not verify_password(form.password, user.password_hash):
        raise HTTPException(401, "Incorrect email or password")
    return schemas.Token(access_token=create_access_token(user.id))


@router.get("/me", response_model=schemas.UserOut)
def me(user: models.User = Depends(get_current_user)):
    return user


@router.post("/change-password", status_code=204)
def change_password(
    body: schemas.PasswordChange,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    if len(body.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    user.password_hash = hash_password(body.new_password)
    db.commit()


@router.post("/finish_onboarding", response_model=schemas.UserOut)
def finish_onboarding(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    if user.onboarded_at is None:
        import datetime as _dt
        user.onboarded_at = _dt.datetime.now(_dt.timezone.utc)
        db.commit()
        db.refresh(user)
    return user


@router.post("/restart_onboarding", response_model=schemas.UserOut)
def restart_onboarding(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    user.onboarded_at = None
    db.commit()
    db.refresh(user)
    return user
