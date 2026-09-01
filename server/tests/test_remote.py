"""Portal → remote Claude via agent.

Security invariant: ONLY the user who installed the agent (agent.user_id)
may send prompts to it. role=manager and role=admin are explicitly
forbidden — this is the user's own machine.
"""
import base64
import hashlib
import io
import os
import pathlib
import secrets
import sys
import tarfile
import zipfile

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

from app import crypto, database, models
from app.auth import hash_password
from app.config import settings
from app.routers import remote as remote_mod
import app.main as main_mod


@pytest.fixture()
def env(tmp_path):
    settings.data_dir = str(tmp_path)
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

    remote_mod._DONE.clear()

    db = TestingSession()
    owner = models.User(email="owner@example.com",
                        password_hash=hash_password("pw"), role=models.Role.user)
    other = models.User(email="other@example.com",
                        password_hash=hash_password("pw"), role=models.Role.user)
    mgr = models.User(email="mgr@example.com",
                      password_hash=hash_password("pw"), role=models.Role.manager)
    adm = models.User(email="adm@example.com",
                      password_hash=hash_password("pw"), role=models.Role.admin)
    db.add_all([owner, other, mgr, adm]); db.flush()

    agent_key = "irs_" + secrets.token_urlsafe(16)
    a = models.Agent(
        user_id=owner.id, hostname="box1",
        api_key_hash=hashlib.sha256(agent_key.encode()).hexdigest(),
    )
    db.add(a); db.commit()
    aid = a.id
    db.close()

    with TestClient(main_mod.app) as c:
        yield c, aid, agent_key


def _login(c, email):
    r = c.post("/auth/token", data={"username": email, "password": "pw"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_owner_can_send_prompt(env):
    c, aid, _ = env
    hdr = _login(c, "owner@example.com")
    r = c.post(f"/agents/{aid}/remote", json={"prompt": "hello"}, headers=hdr)
    assert r.status_code == 200, r.text
    rid = r.json()["request_id"]
    r = c.get(f"/agents/{aid}/remote/{rid}", headers=hdr)
    assert r.status_code == 200
    assert r.json()["status"] in ("pending", "running")


@pytest.mark.parametrize("who", ["other@example.com",
                                  "mgr@example.com",
                                  "adm@example.com"])
def test_non_owner_cannot_send_prompt(env, who):
    c, aid, _ = env
    hdr = _login(c, who)
    r = c.post(f"/agents/{aid}/remote", json={"prompt": "hello"}, headers=hdr)
    assert r.status_code == 403, (who, r.status_code, r.text)


def test_non_owner_cannot_read_result(env):
    c, aid, _ = env
    owner_hdr = _login(c, "owner@example.com")
    r = c.post(f"/agents/{aid}/remote", json={"prompt": "x"}, headers=owner_hdr)
    rid = r.json()["request_id"]

    adm_hdr = _login(c, "adm@example.com")
    r = c.get(f"/agents/{aid}/remote/{rid}", headers=adm_hdr)
    assert r.status_code == 403


def test_send_requires_auth(env):
    c, aid, _ = env
    r = c.post(f"/agents/{aid}/remote", json={"prompt": "x"})
    assert r.status_code == 401


def test_round_trip_via_agent_key(env):
    """Owner enqueues → agent long-polls it out → agent posts result →
    owner reads `done` with the output."""
    c, aid, key = env
    owner_hdr = _login(c, "owner@example.com")

    r = c.post(f"/agents/{aid}/remote",
               json={"prompt": "say hi", "cwd": "/tmp"}, headers=owner_hdr)
    assert r.status_code == 200
    rid = r.json()["request_id"]

    # agent picks up the job
    r = c.get("/agent/remote/poll", headers={"X-Agent-Key": key})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["request_id"] == rid
    assert job["prompt"] == "say hi"
    assert job["cwd"] == "/tmp"

    # agent posts the result
    r = c.post(f"/agent/remote/{rid}/result",
               json={"ok": True, "output": "hi there"},
               headers={"X-Agent-Key": key})
    assert r.status_code == 204

    # owner sees it
    r = c.get(f"/agents/{aid}/remote/{rid}", headers=owner_hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["output"] == "hi there"


def test_agent_poll_requires_key(env):
    c, _, _ = env
    r = c.get("/agent/remote/poll")
    assert r.status_code == 401


def test_agent_cannot_post_result_for_other_agent(env):
    """A second agent's key must not be able to fulfil a request that
    belongs to a different agent."""
    c, aid, _ = env
    owner_hdr = _login(c, "owner@example.com")
    r = c.post(f"/agents/{aid}/remote", json={"prompt": "x"}, headers=owner_hdr)
    rid = r.json()["request_id"]

    # register a second agent under owner so we have a valid-but-wrong key
    r = c.post("/agents", json={"hostname": "box2"}, headers=owner_hdr)
    assert r.status_code == 200
    other_key = r.json()["api_key"]

    r = c.post(f"/agent/remote/{rid}/result",
               json={"ok": True, "output": "spoof"},
               headers={"X-Agent-Key": other_key})
    assert r.status_code == 404


def test_history_persists_and_is_owner_only(env):
    """After sending, the prompt+result must be retrievable from the
    history endpoint — this is what the UI uses to recover after a
    reload / re-login. Admin still can't read it."""
    c, aid, key = env
    owner_hdr = _login(c, "owner@example.com")

    r = c.post(f"/agents/{aid}/remote",
               json={"prompt": "persist me", "cwd": "/tmp"}, headers=owner_hdr)
    rid = r.json()["request_id"]
    c.get("/agent/remote/poll", headers={"X-Agent-Key": key})
    c.post(f"/agent/remote/{rid}/result",
           json={"ok": True, "output": "kept"}, headers={"X-Agent-Key": key})

    # owner: history returns the full row including prompt + output
    r = c.get(f"/agents/{aid}/remote", headers=owner_hdr)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["request_id"] == rid
    assert rows[0]["prompt"] == "persist me"
    assert rows[0]["output"] == "kept"
    assert rows[0]["status"] == "done"

    # admin: 403 on history too
    adm_hdr = _login(c, "adm@example.com")
    r = c.get(f"/agents/{aid}/remote", headers=adm_hdr)
    assert r.status_code == 403


def test_chunk_appends_partial_output(env):
    c, aid, key = env
    owner_hdr = _login(c, "owner@example.com")
    r = c.post(f"/agents/{aid}/remote", json={"prompt": "x"}, headers=owner_hdr)
    rid = r.json()["request_id"]
    c.get("/agent/remote/poll", headers={"X-Agent-Key": key})

    for piece in ("[tool] Read a\n", "partial ", "text\n"):
        r = c.post(f"/agent/remote/{rid}/chunk",
                   json={"text": piece}, headers={"X-Agent-Key": key})
        assert r.status_code == 204

    r = c.get(f"/agents/{aid}/remote/{rid}", headers=owner_hdr)
    body = r.json()
    assert body["status"] == "running"
    assert body["output"] == "[tool] Read a\npartial text\n"

    c.post(f"/agent/remote/{rid}/result",
           json={"ok": True, "output": "final"}, headers={"X-Agent-Key": key})
    r = c.get(f"/agents/{aid}/remote/{rid}", headers=owner_hdr)
    assert r.json()["status"] == "done"
    assert r.json()["output"] == "final"


def test_save_as_report_creates_report_and_scan(env):
    c, aid, key = env
    owner_hdr = _login(c, "owner@example.com")

    # make a product owned by owner so we can attach to it
    r = c.post("/projects", json={"name": "WidgetX"}, headers=owner_hdr)
    assert r.status_code in (200, 201), r.text
    pid = r.json()["id"]

    r = c.post(f"/agents/{aid}/remote",
               json={"prompt": "scan it", "cwd": "/repo"}, headers=owner_hdr)
    rid = r.json()["request_id"]
    c.get("/agent/remote/poll", headers={"X-Agent-Key": key})
    c.post(f"/agent/remote/{rid}/result",
           json={"ok": True, "output": "# vuln\nfound a thing"},
           headers={"X-Agent-Key": key})

    # can't save while still running -> simulate with a 2nd pending req
    r2 = c.post(f"/agents/{aid}/remote", json={"prompt": "x"}, headers=owner_hdr)
    rid2 = r2.json()["request_id"]
    r = c.post(f"/agents/{aid}/remote/{rid2}/save",
               json={"title": "nope"}, headers=owner_hdr)
    assert r.status_code == 400

    # save the completed one
    r = c.post(f"/agents/{aid}/remote/{rid}/save",
               json={"title": "WidgetX scan", "project_id": pid},
               headers=owner_hdr)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["report_id"] and out["scan_id"]

    # admin still can't hit save on someone else's agent
    adm_hdr = _login(c, "adm@example.com")
    r = c.post(f"/agents/{aid}/remote/{rid}/save",
               json={"title": "nope"}, headers=adm_hdr)
    assert r.status_code == 403

    # the scan should be retrievable and carry title/product/cwd
    r = c.get(f"/scans/{out['scan_id']}", headers=owner_hdr)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["product"] == "WidgetX scan"
    assert s["project_id"] == pid
    assert s["scan_target"] == "/repo"
    assert s["source_report_id"] == out["report_id"]
    assert s["state"] == "draft"


def test_owner_can_delete_history_entry(env):
    c, aid, _ = env
    owner_hdr = _login(c, "owner@example.com")
    r = c.post(f"/agents/{aid}/remote", json={"prompt": "x"}, headers=owner_hdr)
    rid = r.json()["request_id"]
    r = c.delete(f"/agents/{aid}/remote/{rid}", headers=owner_hdr)
    assert r.status_code == 204
    r = c.get(f"/agents/{aid}/remote/{rid}", headers=owner_hdr)
    assert r.status_code == 404


# ---- v2: sessions ----------------------------------------------------------

def test_session_crud_owner_only(env):
    c, aid, _ = env
    owner = _login(c, "owner@example.com")
    adm = _login(c, "adm@example.com")

    r = c.post("/remote/sessions",
               json={"agent_id": aid, "title": "scan A", "cwd": "/repo"},
               headers=owner)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert r.json()["title"] == "scan A"
    assert r.json()["status"] == "idle"

    # admin cannot create on someone else's agent
    r = c.post("/remote/sessions", json={"agent_id": aid}, headers=adm)
    assert r.status_code == 403

    # admin cannot read/patch/delete owner's session
    for verb, kw in (("get", {}),
                     ("patch", {"json": {"title": "x"}}),
                     ("delete", {})):
        r = getattr(c, verb)(f"/remote/sessions/{sid}", headers=adm, **kw)
        assert r.status_code == 403, (verb, r.status_code)

    # list scoped to viewer
    r = c.get("/remote/sessions", headers=owner)
    assert [s["id"] for s in r.json()] == [sid]
    r = c.get("/remote/sessions", headers=adm)
    assert r.json() == []

    # rename + archive
    r = c.patch(f"/remote/sessions/{sid}",
                json={"title": "scan A v2", "archived": True}, headers=owner)
    assert r.json()["title"] == "scan A v2"
    assert r.json()["status"] == "archived"
    r = c.get("/remote/sessions", headers=owner)
    assert r.json() == []
    r = c.get("/remote/sessions?include_archived=true", headers=owner)
    assert len(r.json()) == 1


def test_session_conversation_round_trip(env):
    """Turn 1 captures claude_session_id; turn 2's poll must carry it as
    `resume`. Events accumulate per-turn; session flips running↔idle."""
    c, aid, key = env
    owner = _login(c, "owner@example.com")
    akey = {"X-Agent-Key": key}

    sid = c.post("/remote/sessions",
                 json={"agent_id": aid, "cwd": "/repo"}, headers=owner
                 ).json()["id"]

    # ---- turn 1 ----
    r = c.post(f"/remote/sessions/{sid}/prompt",
               json={"prompt": "find vulns"}, headers=owner)
    assert r.status_code == 200, r.text
    rid1 = r.json()["request_id"]
    assert r.json()["session_id"] == sid

    # session is now running; second prompt must 409
    assert c.get(f"/remote/sessions/{sid}", headers=owner).json()["status"] == "running"
    r = c.post(f"/remote/sessions/{sid}/prompt",
               json={"prompt": "too soon"}, headers=owner)
    assert r.status_code == 409

    job = c.get("/agent/remote/poll", headers=akey).json()
    assert job["request_id"] == rid1
    assert job["resume"] is None
    assert job["cwd"] == "/repo"

    # agent streams events
    for ev in (
        {"type": "system", "subtype": "init", "session_id": "claude-abc",
         "model": "claude-opus-4-7", "cwd": "/repo"},
        {"type": "assistant", "content": [
            {"type": "tool_use", "name": "Read", "hint": "src/auth.py"}]},
        {"type": "tool_result", "results": [
            {"type": "tool_result", "is_error": False, "preview": "def login()..."}]},
        {"type": "assistant", "content": [
            {"type": "text", "text": "Found a thing."}]},
    ):
        r = c.post(f"/agent/remote/{rid1}/chunk", json={"event": ev}, headers=akey)
        assert r.status_code == 204

    r = c.post(f"/agent/remote/{rid1}/result",
               json={"ok": True, "output": "Found a thing.",
                     "claude_session_id": "claude-abc"}, headers=akey)
    assert r.status_code == 204

    detail = c.get(f"/remote/sessions/{sid}", headers=owner).json()
    assert detail["status"] == "idle"
    assert detail["claude_session_id"] == "claude-abc"
    assert detail["title"] == "find vulns"  # auto-titled from first prompt
    assert detail["turn_count"] == 1
    t1 = detail["turns"][0]
    assert t1["status"] == "done"
    assert t1["output"] == "Found a thing."
    assert len(t1["events"]) == 4
    assert t1["events"][0]["type"] == "system"
    assert t1["events"][1]["content"][0]["name"] == "Read"

    # ---- turn 2: must resume ----
    r = c.post(f"/remote/sessions/{sid}/prompt",
               json={"prompt": "what about XSS?"}, headers=owner)
    rid2 = r.json()["request_id"]
    job = c.get("/agent/remote/poll", headers=akey).json()
    assert job["request_id"] == rid2
    assert job["resume"] == "claude-abc"

    c.post(f"/agent/remote/{rid2}/result",
           json={"ok": True, "output": "none found",
                 "claude_session_id": "claude-abc"}, headers=akey)
    detail = c.get(f"/remote/sessions/{sid}", headers=owner).json()
    assert detail["turn_count"] == 2
    assert [t["prompt"] for t in detail["turns"]] == ["find vulns", "what about XSS?"]


def test_session_model_threads_to_job(env):
    """A per-session model set in the portal rides along to the agent's claimed
    job; a session without one leaves the agent to fall back to its own default."""
    c, aid, key = env
    owner = _login(c, "owner@example.com")
    akey = {"X-Agent-Key": key}

    # with an explicit model
    s = c.post("/remote/sessions",
               json={"agent_id": aid, "model": "claude-sonnet-4-6"},
               headers=owner).json()
    assert s["model"] == "claude-sonnet-4-6"
    rid = c.post(f"/remote/sessions/{s['id']}/prompt",
                 json={"prompt": "go"}, headers=owner).json()["request_id"]
    job = c.get("/agent/remote/poll", headers=akey).json()
    assert job["request_id"] == rid
    assert job["model"] == "claude-sonnet-4-6"
    c.post(f"/agent/remote/{rid}/result",
           json={"ok": True, "output": "ok"}, headers=akey)

    # without one → null, so the agent uses its configured/CLI default
    s2 = c.post("/remote/sessions", json={"agent_id": aid}, headers=owner).json()
    assert s2["model"] is None
    rid2 = c.post(f"/remote/sessions/{s2['id']}/prompt",
                  json={"prompt": "go"}, headers=owner).json()["request_id"]
    job2 = c.get("/agent/remote/poll", headers=akey).json()
    assert job2["request_id"] == rid2
    assert job2["model"] is None


def test_active_testing_injects_append_system_prompt(env):
    """A session whose harness/project description carries the ACTIVE_TESTING
    sentinel makes the polled job carry an --append-system-prompt authorisation;
    an ordinary session does not."""
    c, aid, key = env
    owner = _login(c, "owner@example.com")
    akey = {"X-Agent-Key": key}

    db = database.SessionLocal()
    owner_id = db.query(models.User).filter_by(email="owner@example.com").one().id
    h = models.Harness(user_id=owner_id, name="probe-kit",
                       description="ACTIVE_TESTING_AUTHORIZED target=acme-gateway.lab")
    db.add(h); db.flush()
    hid = h.id
    db.commit(); db.close()

    # authorised session → job carries the system prompt
    sid = c.post("/remote/sessions",
                 json={"agent_id": aid, "harness_id": hid}, headers=owner).json()["id"]
    rid = c.post(f"/remote/sessions/{sid}/prompt",
                 json={"prompt": "audit it"}, headers=owner).json()["request_id"]
    job = c.get("/agent/remote/poll", headers=akey).json()
    assert job["request_id"] == rid
    sp = job["append_system_prompt"]
    assert sp and "authorised" in sp and "black-box" in sp and "probe-kit" in sp
    # authorised + headless → permissions are bypassed so the harness can run
    assert job["bypass_permissions"] is True
    c.post(f"/agent/remote/{rid}/result",
           json={"ok": True, "output": "ok"}, headers=akey)

    # plain session (no sentinel) → no system prompt
    sid2 = c.post("/remote/sessions", json={"agent_id": aid}, headers=owner).json()["id"]
    rid2 = c.post(f"/remote/sessions/{sid2}/prompt",
                  json={"prompt": "go"}, headers=owner).json()["request_id"]
    job2 = c.get("/agent/remote/poll", headers=akey).json()
    assert job2["request_id"] == rid2
    assert job2["append_system_prompt"] is None
    # bypass is on for every remote session (headless = no one to approve),
    # not just active-testing ones; only the system prompt is gated.
    assert job2["bypass_permissions"] is True


def test_two_sessions_claim_independently(env):
    """Two sessions with one pending turn each → two polls hand out two
    different jobs (the agent runs them concurrently)."""
    c, aid, key = env
    owner = _login(c, "owner@example.com")
    akey = {"X-Agent-Key": key}

    sids = []
    for _ in range(2):
        sid = c.post("/remote/sessions", json={"agent_id": aid},
                     headers=owner).json()["id"]
        c.post(f"/remote/sessions/{sid}/prompt",
               json={"prompt": "go"}, headers=owner)
        sids.append(sid)

    j1 = c.get("/agent/remote/poll", headers=akey).json()
    j2 = c.get("/agent/remote/poll", headers=akey).json()
    assert j1["request_id"] != j2["request_id"]
    assert {j1["request_id"], j2["request_id"]}  # both non-empty


def test_push_upgrade_flow(env):
    """Owner clicks Update → pending_upgrade set → next poll returns the
    upgrade command (and clears the flag) → agent's reported version is
    stored. Non-owners can't trigger it."""
    c, aid, key = env
    owner = _login(c, "owner@example.com")
    adm = _login(c, "adm@example.com")

    # agent reports its version via header
    r = c.get("/agent/remote/poll",
              headers={"X-Agent-Key": key, "X-Agent-Version": "0.1.0"})
    # (204 — no jobs)
    a = next(a for a in c.get("/agents", headers=owner).json() if a["id"] == aid)
    assert a["version"] == "0.1.0"
    assert a["pending_upgrade"] is False

    # admin cannot push upgrade
    r = c.post(f"/agents/{aid}/upgrade", headers=adm)
    assert r.status_code == 403

    # owner pushes upgrade
    r = c.post(f"/agents/{aid}/upgrade", headers=owner)
    assert r.status_code == 200
    assert r.json()["pending_upgrade"] is True

    # next poll hands out the command and clears the flag
    r = c.get("/agent/remote/poll",
              headers={"X-Agent-Key": key, "X-Agent-Version": "0.1.0"})
    assert r.status_code == 200
    assert r.json() == {"command": "upgrade"}

    a = next(a for a in c.get("/agents", headers=owner).json() if a["id"] == aid)
    assert a["pending_upgrade"] is False

    # after upgrade, agent reconnects with new version
    c.get("/agent/remote/poll",
          headers={"X-Agent-Key": key, "X-Agent-Version": "0.2.0"})
    a = next(a for a in c.get("/agents", headers=owner).json() if a["id"] == aid)
    assert a["version"] == "0.2.0"


def test_push_anthropic_key_flow(env):
    """Owner sets a key + expiry in the portal → next poll hands it to the
    agent → portal shows last4 / pushed_at / expires_at, never the full key.
    Non-owners are 403'd; clearing pushes an empty key."""
    c, aid, key = env
    owner = _login(c, "owner@example.com")
    adm = _login(c, "adm@example.com")

    a = next(a for a in c.get("/agents", headers=owner).json() if a["id"] == aid)
    assert a["anthropic_key_last4"] is None
    assert a["pending_key_push"] is False

    r = c.put(f"/agents/{aid}/anthropic-key",
              json={"key": "sk-ant-test-abcd1234",
                    "expires_at": "2099-01-08T00:00:00Z"},
              headers=adm)
    assert r.status_code == 403

    r = c.put(f"/agents/{aid}/anthropic-key",
              json={"key": "sk-ant-test-abcd1234",
                    "expires_at": "2099-01-08T00:00:00Z"},
              headers=owner)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["anthropic_key_last4"] == "1234"
    assert body["pending_key_push"] is True
    assert body["anthropic_key_pushed_at"] is None
    assert body["anthropic_key_expires_at"].startswith("2099-01-08")
    assert "sk-ant" not in str(body)

    r = c.get("/agent/remote/poll", headers={"X-Agent-Key": key})
    assert r.status_code == 200
    assert r.json() == {"command": "set_anthropic_key",
                        "key": "sk-ant-test-abcd1234"}

    # The poll hands over the key but does NOT clear the flag — it stays pending
    # until the agent acks, so a dropped response or a crash mid-apply re-delivers
    # instead of silently losing the key.
    a = next(a for a in c.get("/agents", headers=owner).json() if a["id"] == aid)
    assert a["pending_key_push"] is True
    assert a["anthropic_key_pushed_at"] is None

    # an un-acked poll redelivers the same (idempotent) command
    r = c.get("/agent/remote/poll", headers={"X-Agent-Key": key})
    assert r.status_code == 200
    assert r.json()["command"] == "set_anthropic_key"

    # agent acks → only now is the flag cleared and pushed_at stamped
    r = c.post("/agent/remote/key-applied", headers={"X-Agent-Key": key})
    assert r.status_code == 204
    a = next(a for a in c.get("/agents", headers=owner).json() if a["id"] == aid)
    assert a["pending_key_push"] is False
    assert a["anthropic_key_pushed_at"] is not None
    assert a["anthropic_key_last4"] == "1234"

    # subsequent poll doesn't redeliver
    r = c.get("/agent/remote/poll", headers={"X-Agent-Key": key})
    assert r.status_code == 204

    # clearing pushes an empty key so the agent wipes its local copy
    r = c.delete(f"/agents/{aid}/anthropic-key", headers=owner)
    assert r.status_code == 200
    assert r.json()["anthropic_key_last4"] is None
    r = c.get("/agent/remote/poll", headers={"X-Agent-Key": key})
    assert r.status_code == 200
    assert r.json() == {"command": "set_anthropic_key", "key": ""}
    # ack the clear too, so the flag doesn't stay stuck pending
    assert c.post("/agent/remote/key-applied",
                  headers={"X-Agent-Key": key}).status_code == 204


def test_session_harness_bundle_flow(env):
    """Create a session bound to a harness → first prompt's poll carries
    bundle_url+session_id → agent fetches the tarball (its own key only),
    finds the harness file inside → posts result with workspace path →
    session.cwd updates and pending_bundle clears."""
    c, aid, key = env
    owner = _login(c, "owner@example.com")
    akey = {"X-Agent-Key": key}

    # seed a harness with one file directly
    db = database.SessionLocal()
    owner_id = db.query(models.User).filter_by(email="owner@example.com").one().id
    h = models.Harness(user_id=owner_id, name="probe-kit")
    db.add(h); db.flush()
    raw = b"# probe\nrun me\n"
    db.add(models.HarnessFile(
        harness_id=h.id, relpath="tools/run.md",
        sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw),
        content_enc=crypto.encrypt(raw),
    ))
    db.commit()
    hid = h.id
    db.close()

    # create session with harness attached
    r = c.post("/remote/sessions",
               json={"agent_id": aid, "title": "scan", "harness_id": hid},
               headers=owner)
    assert r.status_code == 200, r.text
    s = r.json()
    sid = s["id"]
    assert s["harness_id"] == hid
    assert s["harness_name"] == "probe-kit"
    assert s["pending_bundle"] is True

    # send first prompt; poll must hand out bundle_url + session_id
    rid = c.post(f"/remote/sessions/{sid}/prompt",
                 json={"prompt": "go"}, headers=owner).json()["request_id"]
    job = c.get("/agent/remote/poll", headers=akey).json()
    assert job["request_id"] == rid
    assert job["session_id"] == sid
    assert job["bundle_url"] == f"/agent/remote/sessions/{sid}/bundle.tar.gz"

    # a different agent's key cannot fetch this session's bundle
    other_key = c.post("/agents", json={"hostname": "box2"},
                       headers=owner).json()["api_key"]
    r = c.get(job["bundle_url"], headers={"X-Agent-Key": other_key})
    assert r.status_code == 404

    # the owning agent can; tarball contains the harness file decrypted
    r = c.get(job["bundle_url"], headers=akey)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
        names = tf.getnames()
        assert names == ["CLAUDE.md", "tools/run.md"]
        assert tf.extractfile("tools/run.md").read() == raw
        assert b"authorised" in tf.extractfile("CLAUDE.md").read()

    # agent reports workspace → session picks it up and clears pending_bundle
    r = c.post(f"/agent/remote/{rid}/result",
               json={"ok": True, "output": "done",
                     "claude_session_id": "cs-1",
                     "workspace": "/home/me/.local/share/irs-agent/sessions/" + sid},
               headers=akey)
    assert r.status_code == 204

    s = c.get(f"/remote/sessions/{sid}", headers=owner).json()
    assert s["pending_bundle"] is False
    assert s["cwd"] == "/home/me/.local/share/irs-agent/sessions/" + sid
    assert s["status"] == "idle"

    # second turn: no bundle_url now that it's materialized
    rid2 = c.post(f"/remote/sessions/{sid}/prompt",
                  json={"prompt": "again"}, headers=owner).json()["request_id"]
    job2 = c.get("/agent/remote/poll", headers=akey).json()
    assert job2["request_id"] == rid2
    assert job2["bundle_url"] is None
    assert job2["cwd"] == s["cwd"]

    # patching harness_id back on re-arms the bundle
    c.post(f"/agent/remote/{rid2}/result",
           json={"ok": True, "output": ""}, headers=akey)
    r = c.patch(f"/remote/sessions/{sid}",
                json={"harness_id": hid}, headers=owner)
    assert r.json()["pending_bundle"] is True


def test_session_upload_flow(env):
    """Upload loose file + a zip → both staged (zip unpacked, traversal
    skipped), pending_bundle re-armed, bundle tarball contains them and
    overlays harness on collision. Per-file delete works; non-owner 403."""
    c, aid, key = env
    owner = _login(c, "owner@example.com")
    adm = _login(c, "adm@example.com")
    akey = {"X-Agent-Key": key}

    # harness with a file that the upload will overwrite
    db = database.SessionLocal()
    owner_id = db.query(models.User).filter_by(email="owner@example.com").one().id
    h = models.Harness(user_id=owner_id, name="kit")
    db.add(h); db.flush()
    db.add(models.HarnessFile(
        harness_id=h.id, relpath="src/app.py",
        sha256=hashlib.sha256(b"OLD").hexdigest(), size_bytes=3,
        content_enc=crypto.encrypt(b"OLD"),
    ))
    db.commit(); hid = h.id; db.close()

    sid = c.post("/remote/sessions",
                 json={"agent_id": aid, "harness_id": hid},
                 headers=owner).json()["id"]

    # build a zip: one good entry, one path-traversal that must be skipped
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("src/util.py", b"def u(): ...\n")
        zf.writestr("../evil.py", b"nope")
    zbuf.seek(0)

    files = [
        ("relpaths", (None, "src/app.py")),
        ("files", ("app.py", b"NEW", "text/x-python")),
        ("relpaths", (None, "code.zip")),
        ("files", ("code.zip", zbuf.read(), "application/zip")),
    ]
    r = c.post(f"/remote/sessions/{sid}/files", files=files, headers=owner)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["upload_count"] == 2
    assert body["pending_bundle"] is True

    listing = c.get(f"/remote/sessions/{sid}/files", headers=owner).json()
    paths = sorted(u["relpath"] for u in listing)
    assert paths == ["src/app.py", "src/util.py"]
    assert "../evil.py" not in paths and "evil.py" not in paths

    # non-owner cannot list/upload
    assert c.get(f"/remote/sessions/{sid}/files", headers=adm).status_code == 403
    r = c.post(f"/remote/sessions/{sid}/files",
               files=[("files", ("x.txt", b"x"))], headers=adm)
    assert r.status_code == 403

    # bundle: harness src/app.py first, then upload src/app.py overlays it
    rid = c.post(f"/remote/sessions/{sid}/prompt",
                 json={"prompt": "go"}, headers=owner).json()["request_id"]
    job = c.get("/agent/remote/poll", headers=akey).json()
    assert job["bundle_url"] == f"/agent/remote/sessions/{sid}/bundle.tar.gz"
    r = c.get(job["bundle_url"], headers=akey)
    contents = {}
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
        for m in tf.getmembers():
            contents[m.name] = tf.extractfile(m).read()
    assert set(contents) == {"CLAUDE.md", "src/app.py", "src/util.py"}
    assert contents["src/app.py"] == b"NEW"
    assert contents["src/util.py"] == b"def u(): ...\n"

    c.post(f"/agent/remote/{rid}/result",
           json={"ok": True, "output": "", "workspace": "/ws"}, headers=akey)
    s = c.get(f"/remote/sessions/{sid}", headers=owner).json()
    assert s["pending_bundle"] is False

    # re-upload same relpath replaces (count stays 2); then delete one
    r = c.post(f"/remote/sessions/{sid}/files",
               files=[("relpaths", (None, "src/app.py")),
                      ("files", ("app.py", b"NEWER"))],
               headers=owner)
    assert r.json()["upload_count"] == 2
    assert r.json()["pending_bundle"] is True

    fid = next(u["id"] for u in
               c.get(f"/remote/sessions/{sid}/files", headers=owner).json()
               if u["relpath"] == "src/util.py")
    r = c.delete(f"/remote/sessions/{sid}/files/{fid}", headers=owner)
    assert r.status_code == 204
    paths = [u["relpath"] for u in
             c.get(f"/remote/sessions/{sid}/files", headers=owner).json()]
    assert paths == ["src/app.py"]

    # clear all
    r = c.delete(f"/remote/sessions/{sid}/files", headers=owner)
    assert r.status_code == 204
    assert c.get(f"/remote/sessions/{sid}/files", headers=owner).json() == []


def test_product_file_library(env, tmp_path):
    """Uploads to a product-linked session land in the shared product
    library: another member can start their own session on that product
    and their agent's bundle contains the files. Per-file delete via the
    other member's session removes it for everyone; the on-disk blob is
    GC'd only when no row in either table references it."""
    c, aid, key = env
    A = _login(c, "owner@example.com")
    B = _login(c, "other@example.com")

    # B installs their own agent
    r = c.post("/agents", json={"hostname": "boxB"}, headers=B)
    bid, bkey = r.json()["id"], r.json()["api_key"]

    # A creates a product and adds B as a member
    pid = c.post("/projects", json={"name": "acme-gateway"}, headers=A).json()["id"]
    r = c.post(f"/projects/{pid}/members",
               json={"email": "other@example.com"}, headers=A)
    assert r.status_code == 200, r.text

    # A: session on product, upload src/main.py
    sa = c.post("/remote/sessions",
                json={"agent_id": aid, "project_id": pid}, headers=A).json()["id"]
    r = c.post(f"/remote/sessions/{sa}/files", headers=A,
               data={"relpaths": ["src/main.py"]},
               files=[("files", ("main.py", b"print(1)\n", "text/x-python"))])
    assert r.status_code == 200, r.text
    assert r.json()["upload_count"] == 1

    da = c.get(f"/remote/sessions/{sa}", headers=A).json()
    assert da["uploads"][0]["source"] == "project"
    assert da["uploads"][0]["uploaded_by_email"] == "owner@example.com"
    fid = da["uploads"][0]["id"]

    # B: own session on the same product — sees A's file without uploading
    sb = c.post("/remote/sessions",
                json={"agent_id": bid, "project_id": pid}, headers=B).json()
    assert sb["upload_count"] == 1
    assert sb["pending_bundle"] is True
    sb = sb["id"]
    db_ = c.get(f"/remote/sessions/{sb}", headers=B).json()
    assert {u["relpath"] for u in db_["uploads"]} == {"src/main.py"}

    # product detail surfaces the shared library size
    pd = c.get(f"/projects/{pid}", headers=B).json()
    assert pd["file_count"] == 1 and pd["file_bytes"] == len(b"print(1)\n")
    # /projects/{id}/files lists it with uploader; non-members are blocked
    pf = c.get(f"/projects/{pid}/files", headers=B).json()
    assert pf["count"] == 1
    assert pf["files"][0]["relpath"] == "src/main.py"
    assert pf["files"][0]["uploaded_by_email"] == "owner@example.com"
    C = _login(c, "mgr@example.com")  # manager but not a member → no view
    assert c.get(f"/projects/{pid}/files", headers=C).status_code == 403
    assert c.delete(f"/projects/{pid}/files", headers=B).status_code == 403  # owner-only
    # second file → delete via the product endpoint (member B), GC + bump
    c.post(f"/remote/sessions/{sa}/files", headers=A,
           data={"relpaths": ["src/util.py"]},
           files=[("files", ("util.py", b"x=1\n", "text/x-python"))])
    pf = c.get(f"/projects/{pid}/files", headers=A).json()
    assert pf["count"] == 2
    util_id = next(f["id"] for f in pf["files"] if f["relpath"] == "src/util.py")
    n_blobs = len(list((tmp_path / "session_uploads").rglob("*.bin")))
    r = c.delete(f"/projects/{pid}/files/{util_id}", headers=B)
    assert r.status_code == 204, r.text
    assert c.get(f"/projects/{pid}", headers=A).json()["file_count"] == 1
    assert c.get(f"/remote/sessions/{sa}", headers=A).json()["pending_bundle"]
    assert len(list((tmp_path / "session_uploads").rglob("*.bin"))) == n_blobs - 1

    # B's agent bundle contains the shared file
    r = c.post(f"/remote/sessions/{sb}/prompt",
               json={"prompt": "go"}, headers=B)
    assert r.status_code == 200
    job = c.get("/agent/remote/poll", headers={"X-Agent-Key": bkey}).json()
    assert job["bundle_url"]
    r = c.get(job["bundle_url"], headers={"X-Agent-Key": bkey})
    assert r.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
        members = {m.name: tf.extractfile(m).read() for m in tf if m.isreg()}
    assert set(members) == {"CLAUDE.md", "src/main.py"}
    assert members["src/main.py"] == b"print(1)\n"
    # auto-context names the product and grants scope
    ctx = members["CLAUDE.md"].decode()
    assert "acme-gateway" in ctx and "authorised" in ctx

    # A's agent must not be able to fetch B's bundle
    r = c.get(job["bundle_url"], headers={"X-Agent-Key": key})
    assert r.status_code == 404

    # gc refcount: A also uploads the SAME bytes to a session with NO
    # product → session_uploads row, same content-addressed blob.
    spriv = c.post("/remote/sessions",
                   json={"agent_id": aid}, headers=A).json()["id"]
    c.post(f"/remote/sessions/{spriv}/files", headers=A,
           data={"relpaths": ["x.py"]},
           files=[("files", ("x.py", b"print(1)\n", "text/x-python"))])
    blobs = list((tmp_path / "session_uploads").rglob("*.bin"))
    assert len(blobs) == 1

    # B deletes the product file via their own session → gone for both,
    # but blob survives because the private session still references it.
    r = c.delete(f"/remote/sessions/{sb}/files/{fid}", headers=B)
    assert r.status_code == 204, r.text
    assert c.get(f"/remote/sessions/{sa}", headers=A).json()["upload_count"] == 0
    assert c.get(f"/remote/sessions/{sb}", headers=B).json()["upload_count"] == 0
    assert blobs[0].exists()

    # both product-linked sessions were re-armed for sync
    assert c.get(f"/remote/sessions/{sa}", headers=A).json()["pending_bundle"]

    # delete the private upload too → blob is finally GC'd
    fid2 = c.get(f"/remote/sessions/{spriv}", headers=A).json()["uploads"][0]["id"]
    c.delete(f"/remote/sessions/{spriv}/files/{fid2}", headers=A)
    assert not blobs[0].exists()


def test_session_project_link(env):
    c, aid, _ = env
    owner = _login(c, "owner@example.com")
    pid = c.post("/projects", json={"name": "WidgetX"}, headers=owner).json()["id"]
    r = c.post("/remote/sessions",
               json={"agent_id": aid, "project_id": pid}, headers=owner)
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] == pid
    assert r.json()["project_name"] == "WidgetX"
    sid = r.json()["id"]
    # non-member product is rejected
    other = _login(c, "other@example.com")
    r = c.patch(f"/remote/sessions/{sid}", json={"project_id": pid}, headers=other)
    assert r.status_code == 403


def test_legacy_text_chunk_still_works(env):
    """Old agents send {text: ...}; new server must still accept it."""
    c, aid, key = env
    owner = _login(c, "owner@example.com")
    r = c.post(f"/agents/{aid}/remote", json={"prompt": "x"}, headers=owner)
    rid = r.json()["request_id"]
    c.get("/agent/remote/poll", headers={"X-Agent-Key": key})
    r = c.post(f"/agent/remote/{rid}/chunk",
               json={"text": "hello\n"}, headers={"X-Agent-Key": key})
    assert r.status_code == 204
    body = c.get(f"/agents/{aid}/remote/{rid}", headers=owner).json()
    assert body["output"] == "hello\n"
    assert body["events"] == []


def test_crypto_stream_roundtrip():
    """encrypt_stream/decrypt_iter must round-trip a multi-chunk buffer
    and DecryptReader must satisfy sized reads across chunk boundaries."""
    plain = os.urandom(crypto.STREAM_CHUNK + 1234)
    enc = io.BytesIO()
    sha, size = crypto.encrypt_stream(io.BytesIO(plain), enc)
    assert size == len(plain)
    assert sha == hashlib.sha256(plain).hexdigest()
    enc.seek(0)
    assert b"".join(crypto.decrypt_iter(io.BytesIO(enc.getvalue()))) == plain
    enc.seek(0)
    r = crypto.DecryptReader(enc)
    out = bytearray()
    while True:
        chunk = r.read(7919)
        if not chunk:
            break
        out += chunk
    assert bytes(out) == plain
