"""
RBAC:
  user    -> own reports only
  manager -> own + all reports of users in same team
  admin   -> everything
"""
from fastapi import HTTPException, status
from sqlalchemy.orm import Query, Session

from . import models
from .models import Role


def require_role(user: models.User, *allowed: Role):
    if user.role not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")


def can_delete(viewer: models.User, owner_id: str | None) -> bool:
    """Global delete rule: admins and managers can delete anything; otherwise
    only the row's owner. Applies to projects, reports, harnesses, agents."""
    return viewer.role in (Role.admin, Role.manager) or (
        owner_id is not None and viewer.id == owner_id
    )


def assert_can_delete(viewer: models.User, owner_id: str | None, *, label: str = "resource"):
    if not can_delete(viewer, owner_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Not allowed to delete this {label}")


def visible_user_ids(db: Session, viewer: models.User) -> list[str] | None:
    """
    Return list of user_ids whose reports/scans the viewer can see directly
    (i.e. before applying project membership). None means "everyone" (admin).

    Note: managers no longer see team-wide reports. Visibility for managers
    and regular users is identical (own uploads + anything a project they're
    in surfaces); manager is just a label until/unless we re-add elevated
    privileges. `team_id` is kept on the User row but stops affecting scope.
    """
    if viewer.role == Role.admin:
        return None
    return [viewer.id]


def _visible_project_ids(viewer: models.User) -> set[str]:
    """Projects whose runs/reports the viewer can see by membership."""
    return {p.id for p in viewer.projects}


def scope_reports(q: Query, db: Session, viewer: models.User) -> Query:
    """Reports visible to the viewer.

    Visible if any of:
      - admin (sees everything)
      - same user / same team manager (existing role-based scope)
      - report is in a project the viewer belongs to (direct or via Run)
    """
    ids = visible_user_ids(db, viewer)
    if ids is None:
        return q
    project_ids = _visible_project_ids(viewer)
    if not project_ids:
        return q.filter(models.Report.user_id.in_(ids))
    # Reports whose run.project_id is one of mine, or whose direct project_id is one of mine.
    visible_session_ids = (
        db.query(models.Run.session_id)
        .filter(models.Run.project_id.in_(project_ids))
        .subquery()
    )
    return q.filter(
        (models.Report.user_id.in_(ids))
        | (models.Report.project_id.in_(project_ids))
        | (models.Report.session_id.in_(visible_session_ids))
    )


def assert_can_view_report(db: Session, viewer: models.User, report: models.Report):
    ids = visible_user_ids(db, viewer)
    if ids is None or report.user_id in ids:
        return
    project_ids = _visible_project_ids(viewer)
    if report.project_id and report.project_id in project_ids:
        return
    if report.session_id:
        run = db.get(models.Run, report.session_id)
        if run and run.project_id and run.project_id in project_ids:
            return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")


def scope_scans(q: Query, db: Session, viewer: models.User) -> Query:
    ids = visible_user_ids(db, viewer)
    if ids is None:
        return q
    project_ids = _visible_project_ids(viewer)
    if not project_ids:
        return q.filter(models.VulnScan.user_id.in_(ids))
    visible_session_ids = (
        db.query(models.Run.session_id)
        .filter(models.Run.project_id.in_(project_ids))
        .subquery()
    )
    return q.filter(
        (models.VulnScan.user_id.in_(ids))
        | (models.VulnScan.project_id.in_(project_ids))
        | (models.VulnScan.source_session_id.in_(visible_session_ids))
    )


def assert_can_view_scan(db: Session, viewer: models.User, scan: models.VulnScan):
    ids = visible_user_ids(db, viewer)
    if ids is None or scan.user_id in ids:
        return
    project_ids = _visible_project_ids(viewer)
    if scan.project_id and scan.project_id in project_ids:
        return
    if scan.source_session_id:
        run = db.get(models.Run, scan.source_session_id)
        if run and run.project_id and run.project_id in project_ids:
            return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")


def assert_can_edit_scan(db: Session, viewer: models.User, scan: models.VulnScan):
    """Admin, scan owner, or members of a project the scan belongs to can edit."""
    if viewer.role == Role.admin:
        return
    if scan.user_id == viewer.id:
        return
    project_ids = _visible_project_ids(viewer)
    if scan.project_id and scan.project_id in project_ids:
        return
    if scan.source_session_id:
        run = db.get(models.Run, scan.source_session_id)
        if run and run.project_id and run.project_id in project_ids:
            return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")
