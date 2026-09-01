"""Per-project / per-team model pinning (Phase 3).

One deployment can route one product's work to a local model and another's to
a hosted one. Resolution is most-specific-first: project, then the user's
team, then the server default.
"""
import base64, os, pathlib, secrets, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("IRS_ENCRYPTION_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
os.environ.setdefault("IRS_SECRET_KEY", "test-secret")
os.environ["IRS_DATABASE_URL"] = "sqlite://"

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app import database, models
from app.auth import hash_password
from app.ai import scope
from app.config import provider_keys


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    database.Base.metadata.create_all(bind=engine)
    s = Session()

    team = models.Team(name="core")
    s.add(team); s.flush()
    user = models.User(email="u@example.com", password_hash=hash_password("pw"),
                       role=models.Role.user, team_id=team.id)
    s.add(user); s.flush()
    proj = models.Project(name="p", created_by=user.id)
    s.add(proj); s.commit()

    # Both providers configured, so pins are considered valid.
    monkeypatch.setattr(provider_keys, "openai_api_key", "sk-test")
    monkeypatch.setattr(provider_keys, "anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr(provider_keys, "local_ai_base_url", "http://gpu.lan:11434/v1")
    monkeypatch.setattr(provider_keys, "local_ai_model", "llama3.1:8b")

    s.team, s.user, s.proj = team, user, proj
    yield s
    s.close()


def test_no_pins_uses_the_server_default(db):
    c = scope.resolve(db, project_id=db.proj.id, user_id=db.user.id)
    assert c.is_default and c.source == "default"


def test_team_pin_applies_to_its_members(db):
    db.team.ai_provider = "openai"
    db.team.ai_model = "gpt-4o-mini"
    db.commit()
    c = scope.resolve(db, project_id=db.proj.id, user_id=db.user.id)
    assert (c.provider, c.model, c.source) == ("openai", "gpt-4o-mini", "team")


def test_project_pin_beats_the_team_pin(db):
    db.team.ai_provider = "openai"
    db.proj.ai_provider = "local"
    db.proj.ai_model = "llama3.1:8b"
    db.commit()
    c = scope.resolve(db, project_id=db.proj.id, user_id=db.user.id)
    assert (c.provider, c.source) == ("local", "project")


def test_a_model_alone_pins_the_model_on_the_active_provider(db):
    db.proj.ai_model = "gpt-4o-mini"
    db.commit()
    c = scope.resolve(db, project_id=db.proj.id, user_id=db.user.id)
    assert c.provider is None and c.model == "gpt-4o-mini"


def test_unknown_pinned_provider_falls_back_instead_of_failing(db):
    """A typo must not take background summarisation down."""
    db.proj.ai_provider = "not-a-provider"
    db.commit()
    c = scope.resolve(db, project_id=db.proj.id, user_id=db.user.id)
    assert c.provider is None


def test_unconfigured_pinned_provider_falls_back(db, monkeypatch):
    """Pinning a provider then clearing its key must not break the pipeline."""
    monkeypatch.setattr(provider_keys, "openai_api_key", None)
    db.proj.ai_provider = "openai"
    db.commit()
    c = scope.resolve(db, project_id=db.proj.id, user_id=db.user.id)
    assert c.provider is None


def test_alias_is_canonicalised(db):
    db.proj.ai_provider = "claude"
    db.commit()
    assert scope.resolve(db, project_id=db.proj.id).provider == "anthropic"


def test_a_user_with_no_team_gets_the_default(db):
    db.user.team_id = None
    db.commit()
    assert scope.resolve(db, user_id=db.user.id).is_default
