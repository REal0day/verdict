"""Share-link guest triage.

Two surfaces:

  /scans/{id}/share        (Bearer auth) — mint, list, revoke share links.
                            Anyone who can edit the scan can manage its links.

  /share/{token}           (public) — server-rendered triage page. The
                            token alone is the bearer credential; no cookie
                            or User row required. Guests can set per-finding
                            TP/FP/SBP/duplicate + dev_notes and nothing else.
"""
from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user, hash_share_token, new_share_token
from ..database import get_db
from ..permissions import assert_can_edit_scan

log = logging.getLogger("irs.share")

api = APIRouter(prefix="/scans", tags=["scans-share"])
public = APIRouter(prefix="/share", tags=["share"])
templates = Jinja2Templates(directory="app/templates")

# Statuses a guest may set. `fixed` is excluded — that's the dev team's
# downstream signal after remediation lands, not a triage verdict.
GUEST_STATUSES = (
    models.FindingStatus.open,
    models.FindingStatus.true_positive,
    models.FindingStatus.false_positive,
    models.FindingStatus.sbp,
    models.FindingStatus.duplicate,
)


# ---------------- helpers ----------------

def _as_utc(d: dt.datetime | None) -> dt.datetime | None:
    """SQLite drops tzinfo on round-trip; treat naive values as UTC so the
    same comparison works on both Postgres and SQLite."""
    if d is None or d.tzinfo is not None:
        return d
    return d.replace(tzinfo=dt.timezone.utc)


def _link_status(link: models.ShareLink) -> str:
    if link.revoked_at is not None:
        return "revoked"
    exp = _as_utc(link.expires_at)
    if exp is not None and exp < dt.datetime.now(dt.timezone.utc):
        return "expired"
    return "active"


def _to_out(link: models.ShareLink) -> schemas.ShareLinkOut:
    out = schemas.ShareLinkOut.model_validate(link)
    out.status = _link_status(link)
    if link.creator:
        out.created_by_email = link.creator.email
    return out


def recompute_scan_rollups(scan: models.VulnScan) -> None:
    """Recount tp/fp/sbp/duplicates/untriaged + highest_severity from the
    scan's Finding rows. Called after any guest-driven status change so the
    summary card stays in sync without the security team having to re-edit.
    """
    counts = {s: 0 for s in models.FindingStatus}
    sev_rank = {
        models.Severity.critical: 0, models.Severity.high: 1,
        models.Severity.medium: 2,   models.Severity.low: 3,
        models.Severity.info: 4,     models.Severity.unknown: 5,
    }
    highest = models.Severity.unknown
    for f in scan.finding_rows:
        counts[f.status] += 1
        if sev_rank[f.severity] < sev_rank[highest]:
            highest = f.severity
    scan.findings = len(scan.finding_rows)
    scan.tp = counts[models.FindingStatus.true_positive]
    scan.fp = counts[models.FindingStatus.false_positive]
    scan.sbp = counts[models.FindingStatus.sbp]
    scan.duplicates = counts[models.FindingStatus.duplicate]
    scan.untriaged = counts[models.FindingStatus.open]
    scan.highest_severity = highest


def _resolve_token(
    db: Session, token: str
) -> tuple[models.ShareLink | None, models.VulnScan | None, str]:
    """Look up a share link by token. Returns (link, scan, status).

    status is one of: active | expired | revoked | invalid. ``link`` and
    ``scan`` are None when status == invalid."""
    link = (
        db.query(models.ShareLink)
        .filter(models.ShareLink.token_hash == hash_share_token(token))
        .first()
    )
    if not link:
        return None, None, "invalid"
    return link, link.scan, _link_status(link)


# ---------------- authed: mint / list / revoke ----------------

@api.post("/{scan_id}/share", response_model=schemas.ShareLinkOut, status_code=201)
def create_share_link(
    scan_id: str,
    body: schemas.ShareLinkCreate,
    request: Request,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    s = db.get(models.VulnScan, scan_id)
    if not s:
        raise HTTPException(404, "Scan not found")
    assert_can_edit_scan(db, viewer, s)

    expires_at = None
    if body.expires_in_days is not None and body.expires_in_days > 0:
        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=body.expires_in_days)

    tok, tok_hash, prefix = new_share_token()
    link = models.ShareLink(
        scan_id=s.id,
        token_hash=tok_hash,
        token_prefix=prefix,
        label=(body.label or "")[:255],
        created_by=viewer.id,
        allow_poc=bool(body.allow_poc),
        expires_at=expires_at,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    log.info(
        "share link minted id=%s scan=%s by=%s prefix=%s",
        link.id, s.id, viewer.email, prefix,
    )

    out = _to_out(link)
    out.token = tok
    out.url = str(request.url_for("share_view", token=tok))
    return out


@api.get("/{scan_id}/share", response_model=list[schemas.ShareLinkOut])
def list_share_links(
    scan_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    s = db.get(models.VulnScan, scan_id)
    if not s:
        raise HTTPException(404, "Scan not found")
    assert_can_edit_scan(db, viewer, s)
    rows = (
        db.query(models.ShareLink)
        .filter(models.ShareLink.scan_id == s.id)
        .order_by(models.ShareLink.created_at.desc())
        .all()
    )
    return [_to_out(r) for r in rows]


@api.delete("/{scan_id}/share/{link_id}", response_model=schemas.ShareLinkOut)
def revoke_share_link(
    scan_id: str,
    link_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    s = db.get(models.VulnScan, scan_id)
    if not s:
        raise HTTPException(404, "Scan not found")
    assert_can_edit_scan(db, viewer, s)
    link = db.get(models.ShareLink, link_id)
    if not link or link.scan_id != s.id:
        raise HTTPException(404, "Link not found")
    if link.revoked_at is None:
        link.revoked_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
        db.refresh(link)
    return _to_out(link)


# ---------------- public: guest triage page ----------------

_SEV_ORDER = {
    "critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "unknown": 5,
}


def _render(
    request: Request, *, link, scan, status, token, ok=None, err=None
):
    findings = []
    assignees: list[str] = []
    if scan is not None:
        findings = sorted(
            scan.finding_rows,
            key=lambda f: (_SEV_ORDER.get(f.severity.value, 9), f.title.lower()),
        )
        assignees = sorted(
            {(f.assigned_to or "").strip() for f in findings if (f.assigned_to or "").strip()},
            key=str.lower,
        )
    return templates.TemplateResponse(
        request,
        "share_triage.html",
        {
            "link": link,
            "scan": scan,
            "status": status,
            "token": token,
            "findings": findings,
            "assignees": assignees,
            "statuses": GUEST_STATUSES,
            "ok": ok,
            "err": err,
        },
    )


@public.get("/{token}", response_class=HTMLResponse, name="share_view")
def share_view(token: str, request: Request, db: Session = Depends(get_db)):
    link, scan, status = _resolve_token(db, token)
    if status == "active":
        link.last_used_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
    return _render(
        request, link=link, scan=scan, status=status, token=token,
        ok=request.query_params.get("ok"),
    )


@public.post("/{token}/findings/{finding_id}")
def share_update_finding(
    token: str,
    finding_id: str,
    request: Request,
    status: str = Form(...),
    dev_notes: str = Form(""),
    assigned_to: str = Form(""),
    reviewer: str = Form(""),
    db: Session = Depends(get_db),
):
    link, scan, link_status = _resolve_token(db, token)
    if link_status != "active":
        return _render(
            request, link=link, scan=scan, status=link_status, token=token,
            err="This link is no longer active.",
        )

    f = db.get(models.Finding, finding_id)
    if not f or f.scan_id != scan.id:
        # Don't leak whether the finding id exists elsewhere.
        raise HTTPException(404, "Finding not found")

    try:
        new_status = models.FindingStatus(status)
    except ValueError:
        raise HTTPException(400, "Invalid status")
    if new_status not in GUEST_STATUSES:
        raise HTTPException(400, "Status not permitted via share link")

    reviewer = (reviewer or "").strip()[:120]
    attribution = (
        f"{reviewer} (via share {link.token_prefix}…)"
        if reviewer else f"(via share {link.token_prefix}…)"
    )

    changed_status = f.status != new_status
    f.status = new_status
    f.dev_notes = (dev_notes or "")[:8000]
    f.assigned_to = (assigned_to or "").strip()[:255]
    f.triaged_by = attribution
    f.triaged_at = dt.datetime.now(dt.timezone.utc)
    link.last_used_at = dt.datetime.now(dt.timezone.utc)

    if changed_status:
        recompute_scan_rollups(scan)

    db.commit()
    log.info(
        "guest triage: link=%s scan=%s finding=%s -> %s by=%r",
        link.token_prefix, scan.id, f.id, new_status.value, reviewer or "(anon)",
    )

    resp = RedirectResponse(
        f"/share/{token}?ok=saved#f-{f.id}", status_code=303
    )
    if reviewer:
        resp.set_cookie(
            "irs_share_reviewer", reviewer,
            max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax",
        )
    return resp
