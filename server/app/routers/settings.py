"""Runtime AI settings, editable from the admin portal.

Credentials and endpoints for every provider are persisted (encrypted) in the
`app_settings` table and pushed into the live `provider_keys` singleton, so a
change takes effect immediately — no redeploy — and survives restarts. A DB
value overrides the corresponding env var.

Rows are keyed `ai_key:<provider>`, `ai_model:<provider>`,
`ai_base_url:<provider>`, plus a single `ai_active_provider`.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import crypto, models
from ..ai.base import (
    PROVIDERS,
    canonical,
    get_provider,
    is_configured,
    normalise_base_url,
    resolve,
    resolve_local_url,
)
from ..auth import get_current_user
from ..config import ProviderKeys, provider_keys, settings as app_settings
from ..database import get_db
from ..models import Role
from ..permissions import require_role

log = logging.getLogger("irs.settings")
router = APIRouter(prefix="/settings", tags=["settings"])

ACTIVE_PROVIDER = "ai_active_provider"


def _key_row(provider: str) -> str:
    return f"ai_key:{provider}"


def _model_row(provider: str) -> str:
    return f"ai_model:{provider}"


def _base_url_row(provider: str) -> str:
    return f"ai_base_url:{provider}"


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
    """Startup hook: push every DB-stored AI setting into the live singleton.

    Previously only Anthropic's key was rehydrated, so a portal-configured
    OpenAI or local deployment silently reverted to env values on restart.
    """
    for name, info in PROVIDERS.items():
        if k := get_setting(db, _key_row(name)):
            setattr(provider_keys, f"{info.attr}_api_key", k)
        if m := get_setting(db, _model_row(name)):
            setattr(provider_keys, f"{info.attr}_model", m)
        if b := get_setting(db, _base_url_row(name)):
            setattr(provider_keys, f"{info.attr}_base_url", b)
    if active := get_setting(db, ACTIVE_PROVIDER):
        if active in PROVIDERS:
            app_settings.default_ai_provider = active
    log.info(
        "AI settings loaded; active provider=%s configured=%s",
        app_settings.default_ai_provider,
        [n for n in PROVIDERS if is_configured(n)],
    )


# ---------------- schema ----------------

class ProviderOut(BaseModel):
    name: str
    display_name: str
    configured: bool
    source: str            # "db" | "env" | "none"
    hint: str | None       # masked tail of the active key
    model: str
    base_url: str | None   # only meaningful for self-hosted endpoints
    requires_key: bool
    supports_tools: bool
    self_hosted: bool
    is_active: bool


class AISettingsOut(BaseModel):
    active_provider: str
    providers: list[ProviderOut]


class AISettingsIn(BaseModel):
    """Update one provider. Omitted fields are left alone; "" clears."""
    provider: str
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None


class ActiveProviderIn(BaseModel):
    provider: str


class AIStatusOut(BaseModel):
    """Non-sensitive view, readable by any signed-in user.

    Chat surfaces poll this so they can warn *before* someone types a prompt
    and waits on a request that was never going to work.
    """
    configured: bool
    provider: str
    display_name: str
    model: str
    any_configured: bool


def _mask(key: str | None) -> str | None:
    if not key:
        return None
    return ("…" + key[-4:]) if len(key) > 4 else "…"


def _describe(db: Session, name: str) -> ProviderOut:
    info = PROVIDERS[name]
    r = resolve(name)
    db_key = get_setting(db, _key_row(name))
    db_base = get_setting(db, _base_url_row(name))
    if info.self_hosted:
        source = "db" if db_base else ("env" if r.base_url else "none")
    else:
        source = "db" if db_key else ("env" if r.api_key else "none")
    return ProviderOut(
        name=name,
        display_name=info.display_name,
        configured=is_configured(name),
        source=source,
        hint=_mask(r.api_key),
        model=r.model,
        base_url=r.base_url or None,
        requires_key=info.requires_key,
        supports_tools=info.supports_tools,
        self_hosted=info.self_hosted,
        is_active=(name == canonical(None)),
    )


# ---------------- endpoints ----------------

@router.get("/ai", response_model=AISettingsOut)
def get_ai_settings(
    db: Session = Depends(get_db),
    actor: models.User = Depends(get_current_user),
):
    require_role(actor, Role.admin)
    return AISettingsOut(
        active_provider=canonical(None),
        providers=[_describe(db, n) for n in PROVIDERS],
    )


@router.put("/ai", response_model=AISettingsOut)
def put_ai_settings(
    body: AISettingsIn,
    db: Session = Depends(get_db),
    actor: models.User = Depends(get_current_user),
):
    require_role(actor, Role.admin)
    name = canonical(body.provider)
    info = PROVIDERS.get(name)
    if not info:
        raise HTTPException(400, f"Unknown provider {body.provider!r}")

    if body.api_key is not None:
        key = body.api_key.strip()
        if key:
            set_setting(db, _key_row(name), key, actor.id)
            setattr(provider_keys, f"{info.attr}_api_key", key)
        else:
            # Clearing the override — fall back to whatever the env provides.
            delete_setting(db, _key_row(name))
            setattr(provider_keys, f"{info.attr}_api_key",
                    getattr(ProviderKeys(), f"{info.attr}_api_key", None))
        log.info("%s API key updated via portal by %s", info.display_name, actor.email)

    if body.model is not None:
        model = body.model.strip()
        if model:
            set_setting(db, _model_row(name), model, actor.id)
            setattr(provider_keys, f"{info.attr}_model", model)
        else:
            delete_setting(db, _model_row(name))
            setattr(provider_keys, f"{info.attr}_model",
                    getattr(ProviderKeys(), f"{info.attr}_model", ""))

    if body.base_url is not None:
        base = normalise_base_url(body.base_url)
        if base:
            set_setting(db, _base_url_row(name), base, actor.id)
            setattr(provider_keys, f"{info.attr}_base_url", base)
        else:
            delete_setting(db, _base_url_row(name))
            setattr(provider_keys, f"{info.attr}_base_url",
                    getattr(ProviderKeys(), f"{info.attr}_base_url", ""))

    db.commit()
    return get_ai_settings(db=db, actor=actor)


@router.put("/ai/active", response_model=AISettingsOut)
def put_active_provider(
    body: ActiveProviderIn,
    db: Session = Depends(get_db),
    actor: models.User = Depends(get_current_user),
):
    """Choose which provider the server uses. Persisted, so it survives restart."""
    require_role(actor, Role.admin)
    name = canonical(body.provider)
    if name not in PROVIDERS:
        raise HTTPException(400, f"Unknown provider {body.provider!r}")
    if not is_configured(name):
        raise HTTPException(400, f"{PROVIDERS[name].display_name} is not configured yet")
    set_setting(db, ACTIVE_PROVIDER, name, actor.id)
    app_settings.default_ai_provider = name
    db.commit()
    log.info("active AI provider set to %s by %s", name, actor.email)
    return get_ai_settings(db=db, actor=actor)


@router.post("/ai/test")
def test_ai_key(
    body: ActiveProviderIn | None = None,
    actor: models.User = Depends(get_current_user),
):
    """Send a 1-token probe through the real provider path.

    Uses get_provider() rather than a vendor SDK directly, so this exercises
    exactly the code the app uses.
    """
    require_role(actor, Role.admin)
    name = canonical(body.provider if body else None)
    try:
        provider = get_provider(name)
        provider.chat("", [{"role": "user", "content": "ping"}], max_tokens=1)
        return {"ok": True, "provider": name, "model": provider.model}
    except Exception as e:
        return {"ok": False, "provider": name, "error": str(e)[:300]}


@router.get("/ai/status", response_model=AIStatusOut)
def get_ai_status(_: models.User = Depends(get_current_user)):
    active = canonical(None)
    info = PROVIDERS.get(active)
    r = resolve(active) if info else None
    return AIStatusOut(
        configured=is_configured(active),
        provider=active,
        display_name=info.display_name if info else active,
        model=r.model if r else "",
        any_configured=any(is_configured(n) for n in PROVIDERS),
    )


# ---------------- local model discovery ----------------

# Ports the common local runtimes listen on out of the box.
LOCAL_CANDIDATES = [
    ("Ollama", 11434),
    ("LM Studio", 1234),
    ("vLLM / llama.cpp", 8000),
    ("LocalAI / Jan", 8080),
    ("text-generation-webui", 5000),
]


class LocalCandidateOut(BaseModel):
    label: str
    base_url: str
    reachable: bool
    models: list[str] = []
    error: str | None = None


@router.get("/ai/local/discover", response_model=list[LocalCandidateOut])
def discover_local_models(actor: models.User = Depends(get_current_user)):
    """Probe the usual local endpoints and report what's actually running.

    The server normally runs in a container, where the operator's "localhost"
    is not ours — `resolve_local_url` rewrites loopback to the container host
    so this finds a model running on the Docker host, which is the case people
    actually hit.
    """
    require_role(actor, Role.admin)
    out: list[LocalCandidateOut] = []
    for label, port in LOCAL_CANDIDATES:
        raw = f"http://localhost:{port}/v1"
        url = resolve_local_url(raw)
        try:
            r = httpx.get(f"{url}/models", timeout=2.0)
            if r.status_code >= 400:
                out.append(LocalCandidateOut(
                    label=label, base_url=url, reachable=False,
                    error=f"HTTP {r.status_code}",
                ))
                continue
            data = r.json()
            ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
            out.append(LocalCandidateOut(
                label=label, base_url=url, reachable=True, models=ids,
            ))
        except Exception as e:
            out.append(LocalCandidateOut(
                label=label, base_url=url, reachable=False, error=type(e).__name__,
            ))
    return out
