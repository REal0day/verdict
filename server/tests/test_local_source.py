"""Local source under SOURCE_ROOT (option 1) + upload fallback (option 3).

Pins the confinement (no ../ or symlink escape) and the resolve/browse
behaviour, plus that local-path validation flows through the cases API.
"""
import base64, os, pathlib, secrets, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("IRS_ENCRYPTION_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
os.environ.setdefault("IRS_SECRET_KEY", "test-secret")
os.environ["IRS_DATABASE_URL"] = "sqlite://"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app import database, models, localsource
from app.auth import hash_password, create_access_token
from app.config import settings
import app.main as main_mod


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """A populated SOURCE_ROOT: two products + a nested dir."""
    (tmp_path / "product-a" / "src").mkdir(parents=True)
    (tmp_path / "product-b").mkdir()
    (tmp_path / "product-a" / "src" / "main.py").write_text("x = 1\n")
    monkeypatch.setattr(settings, "source_mount", str(tmp_path))
    return tmp_path


# ---------------- helper unit tests ----------------

def test_available_reflects_a_populated_root(root):
    assert localsource.available() is True


def test_unset_root_is_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "source_mount", "")
    assert localsource.available() is False
    assert localsource.browse()["available"] is False


def test_empty_root_reads_as_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "source_mount", str(tmp_path))  # empty dir
    assert localsource.available() is False


def test_resolve_a_valid_subdir(root):
    p = localsource.resolve("product-a/src")
    assert p == (root / "product-a" / "src").resolve()


def test_resolve_rejects_a_missing_path(root):
    with pytest.raises(localsource.SourceError, match="not found"):
        localsource.resolve("product-z")


def test_resolve_rejects_a_file(root):
    with pytest.raises(localsource.SourceError, match="directory"):
        localsource.resolve("product-a/src/main.py")


def test_resolve_confines_against_dotdot_escape(root):
    with pytest.raises(localsource.SourceError, match="escapes"):
        localsource.resolve("../../etc")


def test_resolve_confines_against_symlink_escape(root, tmp_path):
    outside = tmp_path.parent / "outside_secret"
    outside.mkdir(exist_ok=True)
    (root / "sneaky").symlink_to(outside)
    with pytest.raises(localsource.SourceError, match="escapes"):
        localsource.resolve("sneaky")


def test_browse_lists_products_and_hides_dotfiles(root):
    (root / ".hidden").mkdir()
    out = localsource.browse()
    assert set(out["dirs"]) == {"product-a", "product-b"}
    assert ".hidden" not in out["dirs"]


def test_browse_descends(root):
    assert localsource.browse("product-a")["dirs"] == ["src"]


# ---------------- through the API ----------------

@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    TS = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    database.engine = engine; database.SessionLocal = TS
    main_mod.engine = engine; main_mod._ensure_onboarding_column = lambda: None
    database.Base.metadata.create_all(bind=engine)
    s = TS()
    u = models.User(email="o@example.com", password_hash=hash_password("pw"),
                    role=models.Role.user)
    s.add(u); s.flush()
    proj = models.Project(name="P", created_by=u.id)
    s.add(proj); s.commit()
    uid, pid = u.id, proj.id; s.close()

    def _db():
        d = TS()
        try:
            yield d
        finally:
            d.close()
    main_mod.app.dependency_overrides[database.get_db] = _db
    with TestClient(main_mod.app) as c:
        c.h = {"Authorization": f"Bearer {create_access_token(uid)}"}
        c.pid = pid
        yield c
    main_mod.app.dependency_overrides.clear()


def _case(c):
    return c.post("/cases", json={"title": "T", "project_id": c.pid}, headers=c.h).json()


def test_browse_endpoint(client, root):
    r = client.get("/cases/source/browse", headers=client.h)
    assert r.status_code == 200
    assert r.json()["available"] and "product-a" in r.json()["dirs"]


def test_set_valid_local_path_is_ready(client, root):
    case = _case(client)
    r = client.put(f"/cases/{case['id']}/source", headers=client.h,
                   json={"kind": "local_path", "local_path": "product-a/src"})
    assert r.status_code == 200
    assert r.json()["source"]["status"] == "ready"


def test_set_escaping_local_path_is_400(client, root):
    case = _case(client)
    r = client.put(f"/cases/{case['id']}/source", headers=client.h,
                   json={"kind": "local_path", "local_path": "../../etc"})
    assert r.status_code == 400 and "escape" in r.text.lower()


def test_local_path_without_a_root_is_400(client, monkeypatch):
    monkeypatch.setattr(settings, "source_mount", "")
    case = _case(client)
    r = client.put(f"/cases/{case['id']}/source", headers=client.h,
                   json={"kind": "local_path", "local_path": "anything"})
    assert r.status_code == 400 and "source root" in r.text.lower()


def test_upload_fallback_pending_then_ready(client):
    """Option 3: upload mode is ready once the project has files."""
    case = _case(client)
    r = client.put(f"/cases/{case['id']}/source", headers=client.h,
                   json={"kind": "upload"})
    assert r.json()["source"]["status"] == "pending"   # no files yet

    db = database.SessionLocal()
    db.add(models.ProjectFile(project_id=client.pid, relpath="a.c",
                              sha256="0"*64, size_bytes=1, storage_key="k"))
    db.commit(); db.close()
    r = client.put(f"/cases/{case['id']}/source", headers=client.h,
                   json={"kind": "upload"})
    assert r.json()["source"]["status"] == "ready"
