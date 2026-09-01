"""User notification inbox.

Notifications are written by other handlers (project access requests,
approvals, etc.) — this router only serves them.

Endpoints (logged-in user only; you only see your own):
  GET    /notifications              latest 100 for me
  GET    /notifications/unread_count {"unread": N}
  POST   /notifications/{id}/read    mark one read
  POST   /notifications/read_all     mark all of mine read
  DELETE /notifications/{id}         remove from inbox
"""
from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db

log = logging.getLogger("irs.notifications")
router = APIRouter(prefix="/notifications", tags=["notifications"])


def _to_out(n: models.Notification) -> schemas.NotificationOut:
    return schemas.NotificationOut.model_validate(n)


@router.get("", response_model=list[schemas.NotificationOut])
def list_notifications(
    unread_only: bool = False,
    limit: int = 100,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    q = (
        db.query(models.Notification)
        .filter(models.Notification.user_id == viewer.id)
        .order_by(models.Notification.created_at.desc())
    )
    if unread_only:
        q = q.filter(models.Notification.read_at.is_(None))
    return [_to_out(n) for n in q.limit(min(max(limit, 1), 200)).all()]


@router.get("/unread_count", response_model=schemas.NotificationCount)
def unread_count(
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    n = (
        db.query(models.Notification)
        .filter(
            models.Notification.user_id == viewer.id,
            models.Notification.read_at.is_(None),
        )
        .count()
    )
    return schemas.NotificationCount(unread=n)


@router.post("/{notif_id}/read", response_model=schemas.NotificationOut)
def mark_read(
    notif_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    n = db.get(models.Notification, notif_id)
    if not n or n.user_id != viewer.id:
        raise HTTPException(404, "Not found")
    if n.read_at is None:
        n.read_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
        db.refresh(n)
    return _to_out(n)


@router.post("/read_all", response_model=schemas.NotificationCount)
def mark_all_read(
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    now = dt.datetime.now(dt.timezone.utc)
    db.query(models.Notification).filter(
        models.Notification.user_id == viewer.id,
        models.Notification.read_at.is_(None),
    ).update({"read_at": now}, synchronize_session=False)
    db.commit()
    return schemas.NotificationCount(unread=0)


@router.delete("/{notif_id}", status_code=204)
def delete_notification(
    notif_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    n = db.get(models.Notification, notif_id)
    if not n or n.user_id != viewer.id:
        raise HTTPException(404, "Not found")
    db.delete(n)
    db.commit()
