"""Runtime server settings editable from the admin portal.

Currently: the Anthropic API key used by the import planner and analytics
chat. It's persisted (encrypted) in the app_settings table and loaded into
the live `provider_keys` singleton, so updating it takes effect immediately
— no redeploy — and survives restarts. A DB value overrides the env var.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import crypto, models
from ..auth import get_current_user
from ..config import provider_keys
from ..database import get_db
from ..models import Role
from ..permissions import require_role

log = logging.getLogger("irs.settings")
router = APIRouter(prefix="/settings", tags=["settings"])

ANTHROPIC_KEY = "anthropic_api_key"


# ---------------- store helpers ----------------

def get_setting(db: Session, key: str) -> str | None:
    row = db.get(models.AppSetting, key)
    if not row:
        return None
    try:
        return crypto.decrypt_str(row.value_enc)
    except Exception as e:  # corrupt/unreadable — treat as unset
        log.warning("could not decrypt app setting %r: %s", key, e)
        return None


def set_setting(db: Session, key: str, value: str, user_id: str | None):
    row = db.get(models.AppSetting, key)
    enc = crypto.encrypt_str(value)
    if row:
        row.value_enc = enc
        row.updated_by = user_id
    else:
        db.add(models.AppSetting(key=key, value_enc=enc, updated_by=user_id))


def delete_setting(db: Session, key: str):
    row = db.get(models.AppSetting, key)
    if row:
        db.delete(row)


def load_ai_settings(db: Session):
    """Called at startup: push any DB-stored AI key into the live singleton so
    a key set via the portal is used after a restart too."""
    k = get_setting(db, ANTHROPIC_KEY)
    if k:
        provider_keys.anthropic_api_key = k
        log.info("loaded Anthropic API key from app_settings (overrides env)")


# ---------------- schema ----------------

class AISettingsOut(BaseModel):
    configured: bool            # is a key available at all (db or env)?
    source: str                 # "db" | "env" | "none"
    hint: str | None            # masked tail of the active key
    model: str


class AISettingsIn(BaseModel):
    anthropic_api_key: str      # "" clears the DB override (revert to env)


def _mask(key: str | None) -> str | None:
    if not key:
        return None
    return ("…" + key[-4:]) if len(key) > 4 else "…"


# ---------------- endpoints ----------------

@router.get("/ai", response_model=AISettingsOut)
def get_ai_settings(
    db: Session = Depends(get_db),
    actor: models.User = Depends(get_current_user),
):
    require_role(actor, Role.admin)
    db_key = get_setting(db, ANTHROPIC_KEY)
    active = provider_keys.anthropic_api_key
    source = "db" if db_key else ("env" if active else "none")
    return AISettingsOut(
        configured=bool(active),
        source=source,
        hint=_mask(active),
        model=provider_keys.anthropic_model,
    )


@router.put("/ai", response_model=AISettingsOut)
def put_ai_settings(
    body: AISettingsIn,
    db: Session = Depends(get_db),
    actor: models.User = Depends(get_current_user),
):
    require_role(actor, Role.admin)
    key = body.anthropic_api_key.strip()
    if key:
        set_setting(db, ANTHROPIC_KEY, key, actor.id)
        provider_keys.anthropic_api_key = key   # take effect immediately
        log.info("Anthropic API key updated via portal by %s", actor.email)
    else:
        # clearing the override — fall back to whatever the env provides
        delete_setting(db, ANTHROPIC_KEY)
        from ..config import ProviderKeys
        provider_keys.anthropic_api_key = ProviderKeys().anthropic_api_key
        log.info("Anthropic API key cleared via portal by %s", actor.email)
    db.commit()
    return get_ai_settings(db=db, actor=actor)


@router.post("/ai/test")
def test_ai_key(
    actor: models.User = Depends(get_current_user),
):
    """Make a 1-token call to verify the currently-active key works."""
    require_role(actor, Role.admin)
    key = provider_keys.anthropic_api_key
    if not key:
        raise HTTPException(400, "No Anthropic API key is configured")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        client.messages.create(
            model=provider_keys.anthropic_model,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


class AIStatusOut(BaseModel):
    """Non-sensitive view of AI availability, readable by any signed-in user.

    The chat surfaces poll this so they can warn *before* someone types a
    prompt and waits on a request that was never going to work. Deliberately
    omits the key hint that `GET /ai` exposes to admins.
    """
    configured: bool
    provider: str
    model: str


@router.get("/ai/status", response_model=AIStatusOut)
def get_ai_status(_: models.User = Depends(get_current_user)):
    from ..config import settings as app_settings

    return AIStatusOut(
        configured=bool(provider_keys.anthropic_api_key),
        provider=app_settings.default_ai_provider,
        model=provider_keys.anthropic_model,
    )
