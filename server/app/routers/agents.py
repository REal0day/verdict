import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crypto, models, schemas
from ..auth import get_current_user, new_api_key
from ..database import get_db
from ..permissions import assert_can_delete

router = APIRouter(prefix="/agents", tags=["agents"])

_AGENT_SRC = Path(__file__).resolve().parent.parent.parent / "agent_src"


def _bundled_agent_version() -> str | None:
    for p in (_AGENT_SRC / "irs_agent" / "__init__.py",
              _AGENT_SRC / "pyproject.toml"):
        try:
            m = re.search(r'(?:__version__|version)\s*=\s*["\']([^"\']+)',
                          p.read_text())
            if m:
                return m.group(1)
        except FileNotFoundError:
            continue
    return None


def _key_last4(a: models.Agent) -> str | None:
    if not a.anthropic_key_enc:
        return None
    try:
        return crypto.decrypt_str(a.anthropic_key_enc)[-4:]
    except Exception:
        return "????"


def _out(a: models.Agent, latest: str | None) -> schemas.AgentOut:
    return schemas.AgentOut(
        id=a.id, hostname=a.hostname, last_seen=a.last_seen, last_ip=a.last_ip,
        version=a.version, pending_upgrade=a.pending_upgrade,
        update_available=bool(latest and a.version and a.version != latest),
        anthropic_key_last4=_key_last4(a),
        anthropic_key_expires_at=a.anthropic_key_expires_at,
        anthropic_key_pushed_at=a.anthropic_key_pushed_at,
        pending_key_push=a.pending_key_push,
    )


def _own(db: Session, viewer: models.User, agent_id: str) -> models.Agent:
    a = db.get(models.Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    if a.user_id != viewer.id:
        raise HTTPException(403, "Only the agent's owner can manage it")
    return a


@router.get("/latest-version")
def latest_version():
    return {"version": _bundled_agent_version()}


@router.post("", response_model=schemas.AgentOut)
def register_agent(
    body: schemas.AgentRegister,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """User registers a new agent for one of their machines; returns API key once."""
    key, key_hash = new_api_key()
    a = models.Agent(user_id=user.id, hostname=body.hostname, api_key_hash=key_hash)
    db.add(a)
    db.commit()
    db.refresh(a)
    return schemas.AgentOut(id=a.id, hostname=a.hostname, api_key=key, last_seen=None)


@router.get("", response_model=list[schemas.AgentOut])
def my_agents(
    db: Session = Depends(get_db), user: models.User = Depends(get_current_user)
):
    latest = _bundled_agent_version()
    rows = db.query(models.Agent).filter(models.Agent.user_id == user.id).all()
    return [_out(a, latest) for a in rows]


@router.post("/{agent_id}/upgrade", response_model=schemas.AgentOut)
def request_upgrade(
    agent_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    a = _own(db, viewer, agent_id)
    a.pending_upgrade = True
    db.commit()
    return _out(a, _bundled_agent_version())


@router.put("/{agent_id}/anthropic-key", response_model=schemas.AgentOut)
def set_anthropic_key(
    agent_id: str,
    body: schemas.AnthropicKeyIn,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    a = _own(db, viewer, agent_id)
    key = (body.key or "").strip()
    if not key:
        raise HTTPException(400, "Key is empty")
    a.anthropic_key_enc = crypto.encrypt_str(key)
    a.anthropic_key_expires_at = body.expires_at
    a.anthropic_key_pushed_at = None
    a.pending_key_push = True
    db.commit()
    return _out(a, _bundled_agent_version())


@router.delete("/{agent_id}/anthropic-key", response_model=schemas.AgentOut)
def clear_anthropic_key(
    agent_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    a = _own(db, viewer, agent_id)
    a.anthropic_key_enc = None
    a.anthropic_key_expires_at = None
    a.anthropic_key_pushed_at = None
    a.pending_key_push = True
    db.commit()
    return _out(a, _bundled_agent_version())


@router.delete("/{agent_id}", status_code=204)
def delete_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    a = db.get(models.Agent, agent_id)
    if not a:
        return
    assert_can_delete(viewer, a.user_id, label="agent")
    # Detach reports/attachments so they survive the agent removal.
    db.query(models.Report).filter(models.Report.agent_id == a.id).update({"agent_id": None})
    db.query(models.Attachment).filter(models.Attachment.agent_id == a.id).update({"agent_id": None})
    db.delete(a)
    db.commit()
