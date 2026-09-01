"""Self-service "create + download my agent" UI.

The plaintext API key is generated server-side, **only the sha256 hash is
stored** (see auth.new_api_key), shown to the user once, and baked into the
generated installer script. If lost, the user must mint a new agent.

Routes (logged-in users only):
  GET  /ui/agent                 list user's agents + form
  POST /ui/agent/new             create new agent, show key once, download link
  GET  /ui/agent/install.sh      stream the install script (auth via cookie + hash)
  GET  /ui/agent/source.tar.gz   stream the bundled agent source tarball
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import logging
import socket
import tarfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import models
from ..auth import new_api_key
from ..config import settings
from ..database import get_db

log = logging.getLogger("irs.agent_install")
router = APIRouter(tags=["agent_install"])
templates = Jinja2Templates(directory="app/templates")

# Source is COPYed into the image by the Dockerfile (see server/Dockerfile).
_AGENT_SRC = Path(__file__).resolve().parent.parent.parent / "agent_src"


def _user_from_request(request: Request, db: Session) -> Optional[models.User]:
    """Accept either Authorization: Bearer JWT (SPA) or the irs_token cookie
    (legacy Jinja UI). Returning None means neither is present/valid."""
    from jose import jwt, JWTError

    # 1. Bearer
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        try:
            payload = jwt.decode(
                auth.split(" ", 1)[1], settings.secret_key, algorithms=["HS256"]
            )
            u = db.get(models.User, payload.get("sub"))
            if u:
                return u
        except JWTError:
            pass

    # 2. Cookie
    tok = request.cookies.get("irs_token")
    if tok:
        try:
            payload = jwt.decode(tok, settings.secret_key, algorithms=["HS256"])
            return db.get(models.User, payload.get("sub"))
        except JWTError:
            pass

    return None


# Kept for backward compatibility with the rest of this module.
_user_from_cookie = _user_from_request


def _require_user(request: Request, db: Session) -> models.User:
    u = _user_from_request(request, db)
    if not u:
        raise HTTPException(401, "Not logged in")
    return u


def _server_url(request: Request) -> str:
    """Pick the public URL the *client* used; falls back to host header."""
    return str(request.base_url).rstrip("/")


@router.get("/ui/agent", response_class=HTMLResponse)
def list_agents(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    agents = (
        db.query(models.Agent)
        .filter(models.Agent.user_id == user.id)
        .order_by(models.Agent.created_at.desc())
        .all()
    )
    default_host = socket.gethostname()
    return templates.TemplateResponse(
        request,
        "agent_install.html",
        {"user": user, "agents": agents, "default_host": default_host},
    )


@router.post("/ui/agent/new", response_class=HTMLResponse)
def create_agent(
    request: Request,
    hostname: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    hostname = hostname.strip() or "unknown"

    plaintext, key_hash = new_api_key()
    agent = models.Agent(user_id=user.id, hostname=hostname, api_key_hash=key_hash)
    db.add(agent)
    db.commit()
    db.refresh(agent)
    log.info(
        "minted agent id=%s user=%s host=%s key_prefix=%s",
        agent.id, user.email, hostname, plaintext[:12],
    )

    # We don't persist plaintext. We need to ship it back to the browser
    # exactly once, then immediately bake it into a downloadable .sh.
    return templates.TemplateResponse(
        request,
        "agent_created.html",
        {
            "user": user,
            "agent": agent,
            "api_key": plaintext,
            "server_url": _server_url(request),
        },
    )


@router.post("/ui/agent/{agent_id}/rename")
def rename_agent(
    request: Request,
    agent_id: str,
    hostname: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    agent = db.get(models.Agent, agent_id)
    if not agent or (agent.user_id != user.id and user.role != models.Role.admin):
        raise HTTPException(404, "Agent not found")
    new = hostname.strip()
    if not new:
        raise HTTPException(400, "Hostname cannot be empty")
    agent.hostname = new
    db.commit()
    log.info("renamed agent id=%s -> %r by %s", agent.id, new, user.email)
    return RedirectResponse("/ui/agent", status_code=303)


@router.get("/ui/agent/install.sh")
def download_install_sh(
    request: Request,
    agent_id: str,
    api_key: str,
    db: Session = Depends(get_db),
):
    """Render the install script. Auth is the (agent_id, api_key) pair itself:
    knowing the high-entropy api_key implies authorisation to install for that
    agent. No cookie / Bearer required — this URL is designed to be curled
    from a brand-new machine that has never logged in to the portal."""
    agent = db.get(models.Agent, agent_id)
    if not agent or hashlib.sha256(api_key.encode()).hexdigest() != agent.api_key_hash:
        # Don't leak whether the agent exists.
        raise HTTPException(404, "Not found")

    owner = db.get(models.User, agent.user_id)
    body = templates.get_template("install_sh.j2").render(
        server_url=_server_url(request),
        api_key=api_key,
        user_email=owner.email if owner else "",
        agent_id=agent.id,
        hostname=agent.hostname,
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )
    return Response(
        content=body,
        media_type="text/x-shellscript",
        headers={
            "Content-Disposition": 'attachment; filename="irs-agent-install.sh"'
        },
    )


@router.get("/ui/agent/source.tar.gz")
def download_source(request: Request, db: Session = Depends(get_db)):
    """Stream the agent source as a pip-installable tarball.

    Auth: a logged-in user OR a valid X-Agent-Key header (the installer
    script supplies the latter so curl can re-fetch on retries without
    a browser cookie).
    """
    if not (_user_from_cookie(request, db) or _agent_from_header(request, db)):
        raise HTTPException(401, "Not authorised")

    if not _AGENT_SRC.is_dir():
        raise HTTPException(500, f"Agent source not present at {_AGENT_SRC}")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # The pyproject.toml in agent_src expects to find ./irs_agent + ./pyproject.toml
        # at the root of the archive — that's exactly what we add.
        for child in sorted(_AGENT_SRC.iterdir()):
            if child.name.startswith(".") or child.name == "__pycache__":
                continue
            tar.add(child, arcname=child.name, recursive=True,
                    filter=_strip_pycache)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/gzip",
        headers={
            "Content-Disposition": 'attachment; filename="irs-agent.tar.gz"'
        },
    )


def _strip_pycache(tarinfo: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
    if "__pycache__" in tarinfo.name or tarinfo.name.endswith(".pyc"):
        return None
    return tarinfo


def _agent_from_header(request: Request, db: Session) -> Optional[models.Agent]:
    key = request.headers.get("x-agent-key")
    if not key:
        return None
    h = hashlib.sha256(key.encode()).hexdigest()
    return db.query(models.Agent).filter(models.Agent.api_key_hash == h).first()
