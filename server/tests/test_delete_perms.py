"""Delete-permission matrix: creator / role=manager / role=admin can delete;
plain users cannot delete things they don't own. Covers products (projects),
scans, reports, harnesses, agents.
"""
import base64
import os
import pathlib
import secrets
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault(
    "IRS_ENCRYPTION_KEY",
    base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
)
os.environ.setdefault("IRS_SECRET_KEY", "test-secret")
os.environ["IRS_DATABASE_URL"] = "sqlite://"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app import database, models, crypto
from app.auth import hash_password
import app.main as main_mod


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    database.engine = engine
    database.SessionLocal = TestingSession
    main_mod.engine = engine
    main_mod._ensure_onboarding_column = lambda: None

    database.Base.metadata.create_all(bind=engine)

    db = TestingSession()
    owner = models.User(
        email="owner@example.com", password_hash=hash_password("pw"),
        role=models.Role.user,
    )
    other = models.User(
        email="other@example.com", password_hash=hash_password("pw"),
        role=models.Role.user,
    )
    mgr = models.User(
        email="mgr@example.com", password_hash=hash_password("pw"),
        role=models.Role.manager,
    )
    adm = models.User(
        email="adm@example.com", password_hash=hash_password("pw"),
        role=models.Role.admin,
    )
    db.add_all([owner, other, mgr, adm])
    db.flush()
    ids = {"owner": owner.id, "other": other.id, "mgr": mgr.id, "adm": adm.id}
    db.commit()
    db.close()

    with TestClient(main_mod.app) as c:
        yield c, TestingSession, ids


def _login(c, email):
    r = c.post("/auth/token", data={"username": email, "password": "pw"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _mk_project(db, user_id, name="P"):
    p = models.Project(name=name, created_by=user_id)
    db.add(p); db.commit(); return p.id


def _mk_scan(db, user_id):
    s = models.VulnScan(user_id=user_id, product="X",
                        state=models.ScanState.confirmed)
    db.add(s); db.commit(); return s.id


def _mk_report(db, user_id):
    r = models.Report(
        user_id=user_id, filename="r.md", sha256=secrets.token_hex(32),
        size_bytes=3, content_enc=crypto.encrypt(b"# h"),
    )
    db.add(r); db.commit(); return r.id


def _mk_harness(db, user_id):
    h = models.Harness(user_id=user_id, name="H")
    db.add(h); db.commit(); return h.id


def _mk_agent(db, user_id):
    a = models.Agent(user_id=user_id, hostname="box", api_key_hash="x")
    db.add(a); db.commit(); return a.id


CASES = [
    ("project", _mk_project, "/projects/{}"),
    ("scan",    _mk_scan,    "/scans/{}"),
    ("report",  _mk_report,  "/reports/{}"),
    ("harness", _mk_harness, "/harnesses/{}"),
    ("agent",   _mk_agent,   "/agents/{}"),
]


@pytest.mark.parametrize("label,mk,path", CASES, ids=[c[0] for c in CASES])
def test_owner_can_delete(env, label, mk, path):
    c, S, ids = env
    db = S(); rid = mk(db, ids["owner"]); db.close()
    hdr = _login(c, "owner@example.com")
    r = c.delete(path.format(rid), headers=hdr)
    assert r.status_code == 204, (label, r.status_code, r.text)


@pytest.mark.parametrize("label,mk,path", CASES, ids=[c[0] for c in CASES])
def test_other_user_cannot_delete(env, label, mk, path):
    c, S, ids = env
    db = S(); rid = mk(db, ids["owner"]); db.close()
    hdr = _login(c, "other@example.com")
    r = c.delete(path.format(rid), headers=hdr)
    assert r.status_code == 403, (label, r.status_code, r.text)


@pytest.mark.parametrize("label,mk,path", CASES, ids=[c[0] for c in CASES])
def test_manager_can_delete_anything(env, label, mk, path):
    c, S, ids = env
    db = S(); rid = mk(db, ids["owner"]); db.close()
    hdr = _login(c, "mgr@example.com")
    r = c.delete(path.format(rid), headers=hdr)
    assert r.status_code == 204, (label, r.status_code, r.text)


@pytest.mark.parametrize("label,mk,path", CASES, ids=[c[0] for c in CASES])
def test_admin_can_delete_anything(env, label, mk, path):
    c, S, ids = env
    db = S(); rid = mk(db, ids["owner"]); db.close()
    hdr = _login(c, "adm@example.com")
    r = c.delete(path.format(rid), headers=hdr)
    assert r.status_code == 204, (label, r.status_code, r.text)


def test_delete_requires_auth(env):
    c, S, ids = env
    db = S(); rid = _mk_report(db, ids["owner"]); db.close()
    r = c.delete(f"/reports/{rid}")
    assert r.status_code == 401


def test_delete_report_detaches_scan_source(env):
    c, S, ids = env
    db = S()
    rid = _mk_report(db, ids["owner"])
    s = models.VulnScan(user_id=ids["owner"], product="X",
                        state=models.ScanState.confirmed,
                        source_report_id=rid)
    db.add(s); db.commit(); sid = s.id; db.close()

    hdr = _login(c, "owner@example.com")
    r = c.delete(f"/reports/{rid}", headers=hdr)
    assert r.status_code == 204

    db = S()
    assert db.get(models.Report, rid) is None
    assert db.get(models.VulnScan, sid).source_report_id is None
    db.close()


def test_delete_agent_detaches_reports(env):
    c, S, ids = env
    db = S()
    aid = _mk_agent(db, ids["owner"])
    rpt = models.Report(
        user_id=ids["owner"], agent_id=aid, filename="r.md",
        sha256=secrets.token_hex(32), size_bytes=1,
        content_enc=crypto.encrypt(b"x"),
    )
    db.add(rpt); db.commit(); rid = rpt.id; db.close()

    hdr = _login(c, "owner@example.com")
    r = c.delete(f"/agents/{aid}", headers=hdr)
    assert r.status_code == 204

    db = S()
    assert db.get(models.Agent, aid) is None
    assert db.get(models.Report, rid).agent_id is None
    db.close()
