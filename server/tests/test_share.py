"""End-to-end tests for the guest share-link triage flow.

Runs the real FastAPI app against an in-memory sqlite DB. The startup
ALTERs in main.py are Postgres-flavoured, so we monkey-patch that hook
to a no-op (create_all on a fresh sqlite DB gives us the full schema
anyway).
"""
import base64
import datetime as dt
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

from app import database, models
from app.auth import hash_password
import app.main as main_mod


@pytest.fixture()
def client():
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
        email="owner@example.com",
        password_hash=hash_password("pw-owner"),
        role=models.Role.user,
    )
    other = models.User(
        email="other@example.com",
        password_hash=hash_password("pw-other"),
        role=models.Role.user,
    )
    db.add_all([owner, other])
    db.flush()

    scan = models.VulnScan(
        user_id=owner.id, product="WidgetX", scan_target="api/v1",
        state=models.ScanState.confirmed,
    )
    other_scan = models.VulnScan(
        user_id=other.id, product="OtherProd",
        state=models.ScanState.confirmed,
    )
    db.add_all([scan, other_scan])
    db.flush()

    f1 = models.Finding(
        scan_id=scan.id, user_id=owner.id, title="SQLi in /login",
        severity=models.Severity.high, status=models.FindingStatus.open,
        description="bad", proof_of_concept="' OR 1=1 --",
    )
    f2 = models.Finding(
        scan_id=scan.id, user_id=owner.id, title="XSS in /search",
        severity=models.Severity.medium, status=models.FindingStatus.open,
    )
    f_other = models.Finding(
        scan_id=other_scan.id, user_id=other.id, title="unrelated",
        severity=models.Severity.low, status=models.FindingStatus.open,
    )
    db.add_all([f1, f2, f_other])
    db.commit()

    ids = {
        "owner": owner.id, "other": other.id,
        "scan": scan.id, "other_scan": other_scan.id,
        "f1": f1.id, "f2": f2.id, "f_other": f_other.id,
    }
    db.close()

    with TestClient(main_mod.app) as c:
        c.ids = ids
        yield c


def _login(c, email, pw):
    r = c.post("/auth/token", data={"username": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_create_requires_auth(client):
    r = client.post(f"/scans/{client.ids['scan']}/share", json={})
    assert r.status_code == 401


def test_create_requires_edit_permission(client):
    hdr = _login(client, "other@example.com", "pw-other")
    r = client.post(f"/scans/{client.ids['scan']}/share", json={}, headers=hdr)
    assert r.status_code == 403


def test_full_guest_triage_flow(client):
    hdr = _login(client, "owner@example.com", "pw-owner")

    # mint
    r = client.post(
        f"/scans/{client.ids['scan']}/share",
        json={"label": "WidgetX devs", "expires_in_days": 30},
        headers=hdr,
    )
    assert r.status_code == 201, r.text
    link = r.json()
    assert link["token"] and link["url"]
    assert link["token_prefix"] == link["token"][:8]
    assert link["allow_poc"] is False
    token = link["token"]

    # list never returns plaintext
    r = client.get(f"/scans/{client.ids['scan']}/share", headers=hdr)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["token"] is None
    assert rows[0]["status"] == "active"

    # guest GET — no auth header
    r = client.get(f"/share/{token}")
    assert r.status_code == 200
    body = r.text
    assert "WidgetX" in body
    assert "SQLi in /login" in body
    # PoC hidden by default
    assert "OR 1=1" not in body
    assert "Proof-of-concept hidden" in body

    # guest sets f1 -> TP with notes
    r = client.post(
        f"/share/{token}/findings/{client.ids['f1']}",
        data={
            "status": "true_positive",
            "dev_notes": "confirmed exploitable on staging",
            "reviewer": "jdoe@widgetx.com",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    # guest sets f2 -> FP
    r = client.post(
        f"/share/{token}/findings/{client.ids['f2']}",
        data={"status": "false_positive", "dev_notes": "input is sanitised"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    # owner sees the updates via the regular API
    r = client.get(f"/scans/{client.ids['scan']}", headers=hdr)
    assert r.status_code == 200
    detail = r.json()
    by_id = {f["id"]: f for f in detail["findings_list"]}
    assert by_id[client.ids["f1"]]["status"] == "true_positive"
    assert by_id[client.ids["f1"]]["dev_notes"] == "confirmed exploitable on staging"
    assert "jdoe@widgetx.com" in by_id[client.ids["f1"]]["triaged_by"]
    assert "via share" in by_id[client.ids["f1"]]["triaged_by"]
    assert by_id[client.ids["f2"]]["status"] == "false_positive"

    # rollups recomputed
    assert detail["tp"] == 1
    assert detail["fp"] == 1
    assert detail["untriaged"] == 0
    assert detail["findings"] == 2
    assert detail["highest_severity"] == "high"


def test_guest_cannot_touch_other_scan(client):
    hdr = _login(client, "owner@example.com", "pw-owner")
    r = client.post(f"/scans/{client.ids['scan']}/share", json={}, headers=hdr)
    token = r.json()["token"]

    r = client.post(
        f"/share/{token}/findings/{client.ids['f_other']}",
        data={"status": "true_positive", "dev_notes": "x"},
    )
    assert r.status_code == 404


def test_guest_cannot_set_disallowed_status(client):
    hdr = _login(client, "owner@example.com", "pw-owner")
    r = client.post(f"/scans/{client.ids['scan']}/share", json={}, headers=hdr)
    token = r.json()["token"]

    r = client.post(
        f"/share/{token}/findings/{client.ids['f1']}",
        data={"status": "fixed", "dev_notes": ""},
    )
    assert r.status_code == 400


def test_invalid_token_renders_page(client):
    r = client.get("/share/not-a-real-token")
    assert r.status_code == 200
    assert "Link unavailable" in r.text

    r = client.post(
        "/share/not-a-real-token/findings/anything",
        data={"status": "true_positive"},
    )
    # invalid token on POST renders the same "unavailable" page
    assert r.status_code == 200
    assert "Link unavailable" in r.text


def test_revoked_link_blocks_writes(client):
    hdr = _login(client, "owner@example.com", "pw-owner")
    r = client.post(f"/scans/{client.ids['scan']}/share", json={}, headers=hdr)
    link = r.json()
    token = link["token"]

    r = client.delete(
        f"/scans/{client.ids['scan']}/share/{link['id']}", headers=hdr
    )
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"

    r = client.get(f"/share/{token}")
    assert "revoked" in r.text.lower()

    r = client.post(
        f"/share/{token}/findings/{client.ids['f1']}",
        data={"status": "true_positive"},
    )
    assert r.status_code == 200
    assert "no longer active" in r.text

    # f1 unchanged
    r = client.get(f"/scans/{client.ids['scan']}", headers=hdr)
    by_id = {f["id"]: f for f in r.json()["findings_list"]}
    assert by_id[client.ids["f1"]]["status"] == "open"


def test_expired_link_blocks_writes(client):
    hdr = _login(client, "owner@example.com", "pw-owner")
    r = client.post(f"/scans/{client.ids['scan']}/share", json={}, headers=hdr)
    link = r.json()
    token = link["token"]

    db = database.SessionLocal()
    row = db.get(models.ShareLink, link["id"])
    row.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    db.commit()
    db.close()

    r = client.get(f"/share/{token}")
    assert "expired" in r.text.lower()

    r = client.post(
        f"/share/{token}/findings/{client.ids['f1']}",
        data={"status": "true_positive"},
    )
    assert "no longer active" in r.text


def test_guest_can_assign_finding(client):
    hdr = _login(client, "owner@example.com", "pw-owner")
    r = client.post(f"/scans/{client.ids['scan']}/share", json={}, headers=hdr)
    token = r.json()["token"]

    r = client.post(
        f"/share/{token}/findings/{client.ids['f1']}",
        data={
            "status": "open",
            "assigned_to": "  alice@widgetx.com  ",
            "dev_notes": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    # value persisted (stripped) and visible to the owner via API
    r = client.get(f"/scans/{client.ids['scan']}", headers=hdr)
    by_id = {f["id"]: f for f in r.json()["findings_list"]}
    assert by_id[client.ids["f1"]]["assigned_to"] == "alice@widgetx.com"

    # share page now shows it in the overview + offers it as a filter option
    r = client.get(f"/share/{token}")
    assert "alice@widgetx.com" in r.text
    assert '<option value="alice@widgetx.com">' in r.text


def test_allow_poc_exposes_poc(client):
    hdr = _login(client, "owner@example.com", "pw-owner")
    r = client.post(
        f"/scans/{client.ids['scan']}/share",
        json={"allow_poc": True},
        headers=hdr,
    )
    token = r.json()["token"]

    r = client.get(f"/share/{token}")
    assert "OR 1=1" in r.text
    assert "Proof-of-concept hidden" not in r.text
