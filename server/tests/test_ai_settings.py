"""Multi-provider AI settings, end to end against the real app.

Includes the regression for /settings/ai/status reporting `configured` from
the Anthropic key alone — which made an OpenAI-only or local-model
deployment show a false "not configured" warning and disable Send.
"""
import base64, os, pathlib, secrets, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("IRS_ENCRYPTION_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
os.environ.setdefault("IRS_SECRET_KEY", "test-secret")
os.environ["IRS_DATABASE_URL"] = "sqlite://"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app import database, models
from app.auth import hash_password, create_access_token
from app.config import provider_keys, settings as app_settings
import app.main as main_mod


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    database.engine = engine
    database.SessionLocal = TestingSession
    main_mod.engine = engine
    main_mod._ensure_onboarding_column = lambda: None
    database.Base.metadata.create_all(bind=engine)

    db = TestingSession()
    admin = models.User(email="admin@example.com",
                        password_hash=hash_password("pw"), role=models.Role.admin)
    plain = models.User(email="user@example.com",
                        password_hash=hash_password("pw"), role=models.Role.user)
    db.add_all([admin, plain]); db.commit()
    ids = (admin.id, plain.id); db.close()

    def _get_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    main_mod.app.dependency_overrides[database.get_db] = _get_db

    # Start from a known-empty provider config.
    for attr in ("anthropic_api_key", "openai_api_key", "gemini_api_key", "xai_api_key",
                 "local_ai_api_key"):
        monkeypatch.setattr(provider_keys, attr, None)
    monkeypatch.setattr(provider_keys, "local_ai_base_url", "")
    monkeypatch.setattr(provider_keys, "local_ai_model", "")
    monkeypatch.setattr(app_settings, "default_ai_provider", "anthropic")

    with TestClient(main_mod.app) as c:
        c.admin = {"Authorization": f"Bearer {create_access_token(ids[0])}"}
        c.plain = {"Authorization": f"Bearer {create_access_token(ids[1])}"}
        yield c
    main_mod.app.dependency_overrides.clear()


def test_settings_lists_every_provider(client):
    r = client.get("/settings/ai", headers=client.admin)
    assert r.status_code == 200
    names = {p["name"] for p in r.json()["providers"]}
    assert {"anthropic", "openai", "gemini", "grok", "local"} <= names


def test_settings_is_admin_only(client):
    assert client.get("/settings/ai", headers=client.plain).status_code == 403


def test_status_is_readable_by_any_signed_in_user(client):
    assert client.get("/settings/ai/status", headers=client.plain).status_code == 200


def test_status_does_not_leak_the_key_hint(client, monkeypatch):
    monkeypatch.setattr(provider_keys, "anthropic_api_key", "sk-ant-secret-tail")
    body = client.get("/settings/ai/status", headers=client.plain).json()
    assert "hint" not in body and "secret" not in str(body)


def test_openai_only_deployment_is_reported_configured(client, monkeypatch):
    """Regression: status used to read the Anthropic key regardless of provider."""
    monkeypatch.setattr(provider_keys, "openai_api_key", "sk-openai")
    monkeypatch.setattr(app_settings, "default_ai_provider", "openai")
    body = client.get("/settings/ai/status", headers=client.plain).json()
    assert body["provider"] == "openai"
    assert body["configured"] is True
    assert body["any_configured"] is True


def test_status_flags_an_unconfigured_active_provider(client, monkeypatch):
    monkeypatch.setattr(provider_keys, "openai_api_key", "sk-openai")
    monkeypatch.setattr(app_settings, "default_ai_provider", "anthropic")
    body = client.get("/settings/ai/status", headers=client.plain).json()
    assert body["configured"] is False, "active provider has no key"
    assert body["any_configured"] is True, "but another provider does"


def test_saving_a_key_takes_effect_immediately(client):
    r = client.put("/settings/ai", headers=client.admin,
                   json={"provider": "openai", "api_key": "sk-live-1234"})
    assert r.status_code == 200
    openai = next(p for p in r.json()["providers"] if p["name"] == "openai")
    assert openai["configured"] and openai["source"] == "db"
    assert openai["hint"] == "…1234", "hint should be a masked tail, not the key"
    assert provider_keys.openai_api_key == "sk-live-1234"


def test_clearing_a_key_reverts_to_the_environment(client):
    client.put("/settings/ai", headers=client.admin,
               json={"provider": "openai", "api_key": "sk-live"})
    r = client.put("/settings/ai", headers=client.admin,
                   json={"provider": "openai", "api_key": ""})
    openai = next(p for p in r.json()["providers"] if p["name"] == "openai")
    assert openai["source"] in ("env", "none")


def test_local_endpoint_is_configured_by_url_and_model(client):
    r = client.put("/settings/ai", headers=client.admin,
                   json={"provider": "local", "base_url": "http://gpu-box.lan:11434",
                         "model": "llama3.1:8b"})
    local = next(p for p in r.json()["providers"] if p["name"] == "local")
    assert local["configured"] is True
    assert local["base_url"] == "http://gpu-box.lan:11434/v1", "trailing /v1 added"
    assert local["model"] == "llama3.1:8b"
    assert local["requires_key"] is False


def test_switching_the_active_provider_persists(client):
    client.put("/settings/ai", headers=client.admin,
               json={"provider": "local", "base_url": "http://gpu-box.lan:11434",
                     "model": "llama3.1:8b"})
    r = client.put("/settings/ai/active", headers=client.admin, json={"provider": "local"})
    assert r.status_code == 200
    assert r.json()["active_provider"] == "local"
    assert client.get("/settings/ai/status", headers=client.plain).json()["provider"] == "local"


def test_cannot_activate_an_unconfigured_provider(client):
    r = client.put("/settings/ai/active", headers=client.admin, json={"provider": "openai"})
    assert r.status_code == 400


def test_settings_survive_a_restart(client):
    """load_ai_settings must rehydrate every provider, not just Anthropic."""
    client.put("/settings/ai", headers=client.admin,
               json={"provider": "local", "base_url": "http://gpu-box.lan:11434",
                     "model": "llama3.1:8b"})
    client.put("/settings/ai/active", headers=client.admin, json={"provider": "local"})

    # Wipe the live singleton as a process restart would.
    provider_keys.local_ai_base_url = ""
    provider_keys.local_ai_model = ""
    app_settings.default_ai_provider = "anthropic"

    from app.routers.settings import load_ai_settings
    db = database.SessionLocal()
    try:
        load_ai_settings(db)
    finally:
        db.close()

    assert provider_keys.local_ai_base_url == "http://gpu-box.lan:11434/v1"
    assert provider_keys.local_ai_model == "llama3.1:8b"
    assert app_settings.default_ai_provider == "local"


def test_local_discovery_is_admin_only(client):
    assert client.get("/settings/ai/local/discover", headers=client.plain).status_code == 403


def test_local_discovery_reports_candidates(client):
    r = client.get("/settings/ai/local/discover", headers=client.admin)
    assert r.status_code == 200
    rows = r.json()
    assert any("11434" in row["base_url"] for row in rows), "Ollama's port should be probed"
    assert all({"label", "base_url", "reachable"} <= set(row) for row in rows)
