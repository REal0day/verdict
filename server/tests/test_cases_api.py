"""Cases API (foundation): create, source, stage queue, model resolution, RBAC.

No execution — stage runs stay pending. Verifies the surface an analyst and
the (future) executor talk to.
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
from app.config import provider_keys
import app.main as main_mod


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    TS = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    database.engine = engine
    database.SessionLocal = TS
    main_mod.engine = engine
    main_mod._ensure_onboarding_column = lambda: None
    database.Base.metadata.create_all(bind=engine)

    s = TS()
    owner = models.User(email="owner@example.com", password_hash=hash_password("pw"),
                        role=models.Role.user)
    other = models.User(email="other@example.com", password_hash=hash_password("pw"),
                        role=models.Role.user)
    s.add_all([owner, other]); s.flush()
    proj = models.Project(name="WidgetX", created_by=owner.id)
    s.add(proj); s.commit()
    ids = (owner.id, other.id, proj.id); s.close()

    def _get_db():
        d = TS()
        try:
            yield d
        finally:
            d.close()

    main_mod.app.dependency_overrides[database.get_db] = _get_db
    monkeypatch.setattr(provider_keys, "anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr(provider_keys, "openai_api_key", "sk-openai")
    with TestClient(main_mod.app) as c:
        c.owner = {"Authorization": f"Bearer {create_access_token(ids[0])}"}
        c.other = {"Authorization": f"Bearer {create_access_token(ids[1])}"}
        c.pid = ids[2]
        yield c
    main_mod.app.dependency_overrides.clear()


def _mk_case(c, **kw):
    body = {"title": "RCE in upload", "project_id": c.pid,
            "report_text": "attacker uploads a .php and gets RCE"}
    body.update(kw)
    r = c.post("/cases", json=body, headers=c.owner)
    assert r.status_code == 201, r.text
    return r.json()


def test_create_case_makes_a_backing_scan_and_encrypts_the_report(client):
    case = _mk_case(client)
    assert case["scan_id"], "a backing scan should be created"
    assert case["status"] == "intake"
    assert case["report_text"] == "attacker uploads a .php and gets RCE"
    assert case["finding_count"] == 0


def test_report_text_is_not_stored_in_the_clear(client):
    case = _mk_case(client)
    row = database.SessionLocal().get(models.Case, case["id"])
    assert row.report_enc is not None
    assert b"attacker uploads" not in row.report_enc


def test_findings_flow_through_the_backing_scan(client):
    case = _mk_case(client)
    db = database.SessionLocal()
    for t in ("A", "B", "C"):
        db.add(models.Finding(scan_id=case["scan_id"],
                              user_id=db.get(models.Case, case["id"]).user_id, title=t))
    db.commit(); db.close()
    detail = client.get(f"/cases/{case['id']}", headers=client.owner).json()
    assert detail["finding_count"] == 3
    assert {f["title"] for f in detail["findings"]} == {"A", "B", "C"}


def test_local_source_needs_a_root_remote_is_pending(client):
    case = _mk_case(client)
    # No SOURCE_ROOT configured here -> local_path is rejected with a 400
    # (validation lives in test_local_source.py). Remote is accepted-but-pending.
    r = client.put(f"/cases/{case['id']}/source", headers=client.owner,
                   json={"kind": "local_path", "local_path": "widgetx"})
    assert r.status_code == 400 and "source root" in r.text.lower()
    r = client.put(f"/cases/{case['id']}/source", headers=client.owner,
                   json={"kind": "remote_git", "remote_url": "https://x/y.git", "ref": "main"})
    src = r.json()["source"]
    assert src["status"] == "pending" and src["kind"] == "remote_git"


def test_queue_a_finding_stage_and_resolve_its_model(client):
    case = _mk_case(client, ai_provider="openai")   # case-wide default
    db = database.SessionLocal()
    f = models.Finding(scan_id=case["scan_id"],
                       user_id=db.get(models.Case, case["id"]).user_id, title="A")
    db.add(f); db.commit(); fid = f.id; db.close()

    r = client.post(f"/cases/{case['id']}/stages", headers=client.owner,
                    json={"stage": "source", "finding_id": fid})
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["status"] == "pending"
    # no stage pin -> inherits the case default
    assert run["resolved"]["provider"] == "openai"
    assert run["resolved"]["source"] == "case"
    # queuing work activates the case
    assert client.get(f"/cases/{case['id']}", headers=client.owner).json()["status"] == "active"


def test_stage_pin_overrides_case_default(client):
    case = _mk_case(client, ai_provider="openai")
    db = database.SessionLocal()
    f = models.Finding(scan_id=case["scan_id"],
                       user_id=db.get(models.Case, case["id"]).user_id, title="A")
    db.add(f); db.commit(); fid = f.id; db.close()
    r = client.post(f"/cases/{case['id']}/stages", headers=client.owner,
                    json={"stage": "poc", "finding_id": fid,
                          "ai_provider": "anthropic", "ai_model": "claude-haiku-4-5"})
    res = r.json()["resolved"]
    assert (res["provider"], res["model"], res["source"]) == ("anthropic", "claude-haiku-4-5", "stage")


def test_finding_stage_requires_a_finding(client):
    case = _mk_case(client)
    r = client.post(f"/cases/{case['id']}/stages", headers=client.owner,
                    json={"stage": "source"})
    assert r.status_code == 400 and "finding_id" in r.text


def test_report_stage_is_case_level(client):
    case = _mk_case(client)
    r = client.post(f"/cases/{case['id']}/stages", headers=client.owner,
                    json={"stage": "report"})
    assert r.status_code == 201
    assert r.json()["finding_id"] is None


def test_a_stage_run_from_another_cases_finding_is_rejected(client):
    case1 = _mk_case(client)
    case2 = _mk_case(client)
    db = database.SessionLocal()
    f = models.Finding(scan_id=case2["scan_id"],
                       user_id=db.get(models.Case, case2["id"]).user_id, title="X")
    db.add(f); db.commit(); fid = f.id; db.close()
    r = client.post(f"/cases/{case1['id']}/stages", headers=client.owner,
                    json={"stage": "source", "finding_id": fid})
    assert r.status_code == 400


def test_invalid_stage_name_is_rejected(client):
    case = _mk_case(client)
    r = client.post(f"/cases/{case['id']}/stages", headers=client.owner,
                    json={"stage": "not-a-stage"})
    assert r.status_code == 400 and "expected one of" in r.text


def test_rbac_other_user_cannot_see_or_edit(client):
    case = _mk_case(client)
    assert client.get(f"/cases/{case['id']}", headers=client.other).status_code == 403
    assert client.patch(f"/cases/{case['id']}", headers=client.other,
                        json={"title": "hijack"}).status_code == 403
    assert client.post(f"/cases/{case['id']}/stages", headers=client.other,
                       json={"stage": "report"}).status_code == 403


def test_list_is_scoped_to_the_viewer(client):
    _mk_case(client); _mk_case(client)
    assert len(client.get("/cases", headers=client.owner).json()) == 2
    assert client.get("/cases", headers=client.other).json() == []


def test_delete_leaves_the_scan_intact(client):
    case = _mk_case(client)
    sid = case["scan_id"]
    assert client.delete(f"/cases/{case['id']}", headers=client.owner).status_code == 204
    assert client.get(f"/cases/{case['id']}", headers=client.owner).status_code == 404
    assert database.SessionLocal().get(models.VulnScan, sid) is not None


def test_update_case_status_and_model(client):
    case = _mk_case(client)
    r = client.patch(f"/cases/{case['id']}", headers=client.owner,
                     json={"status": "blocked", "ai_provider": "openai", "ai_model": "gpt-4o"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "blocked"
    assert body["ai_provider"] == "openai" and body["ai_model"] == "gpt-4o"
