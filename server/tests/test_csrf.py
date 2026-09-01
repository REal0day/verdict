"""CSRF protection on the cookie-authenticated /ui/* surface.

The JSON API authenticates with a Bearer header and is deliberately exempt —
an attacker's page cannot set that header cross-origin. These tests pin both
halves of that contract.
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

from app import database, models
from app.auth import hash_password, create_access_token
from app.csrf import COOKIE_NAME, FORM_FIELD
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
    user = models.User(
        email="owner@example.com",
        password_hash=hash_password("pw-owner"),
        role=models.Role.admin,
    )
    db.add(user)
    db.commit()
    uid = user.id
    db.close()

    def _get_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    main_mod.app.dependency_overrides[database.get_db] = _get_db
    with TestClient(main_mod.app) as c:
        c.user_id = uid
        yield c
    main_mod.app.dependency_overrides.clear()


def _prime(c):
    """Do a safe request so the server issues a CSRF cookie."""
    c.get("/ui/scans")
    return c.cookies.get(COOKIE_NAME)


def test_safe_request_issues_a_csrf_cookie(client):
    assert _prime(client), "no CSRF cookie was issued on a GET"


def test_post_without_token_is_rejected(client):
    _prime(client)
    client.cookies.set("irs_token", create_access_token(client.user_id))
    r = client.post("/ui/projects/new", data={"name": "x"}, follow_redirects=False)
    assert r.status_code == 403
    assert "CSRF" in r.text


def test_post_with_wrong_token_is_rejected(client):
    _prime(client)
    client.cookies.set("irs_token", create_access_token(client.user_id))
    r = client.post(
        "/ui/projects/new",
        data={"name": "x", FORM_FIELD: "not-the-right-token"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_post_with_matching_token_passes_the_guard(client):
    tok = _prime(client)
    client.cookies.set("irs_token", create_access_token(client.user_id))
    r = client.post(
        "/ui/projects/new",
        data={"name": "csrf-ok", FORM_FIELD: tok},
        follow_redirects=False,
    )
    assert r.status_code != 403


def test_token_may_also_arrive_as_a_header(client):
    tok = _prime(client)
    client.cookies.set("irs_token", create_access_token(client.user_id))
    r = client.post(
        "/ui/projects/new",
        data={"name": "hdr"},
        headers={"X-CSRF-Token": tok},
        follow_redirects=False,
    )
    assert r.status_code != 403


def test_bearer_authenticated_requests_are_exempt(client):
    """The JSON API can't be forged cross-origin, so it must not be blocked."""
    _prime(client)
    token = create_access_token(client.user_id)
    r = client.post(
        "/projects",
        json={"name": "via-api"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code != 403


def test_body_is_still_readable_by_the_handler(client):
    """The guard buffers the body to read the token; the route must still see it."""
    tok = _prime(client)
    client.cookies.set("irs_token", create_access_token(client.user_id))
    client.post(
        "/ui/projects/new",
        data={"name": "body-replay-check", FORM_FIELD: tok},
        follow_redirects=False,
    )
    token = create_access_token(client.user_id)
    names = [
        p["name"]
        for p in client.get(
            "/projects", headers={"Authorization": f"Bearer {token}"}
        ).json()
    ]
    assert "body-replay-check" in names, "form field was lost when the body was buffered"


def test_logout_is_not_reachable_by_get(client):
    """A state-changing GET is CSRF-able via plain top-level navigation."""
    r = client.get("/ui/logout", follow_redirects=False)
    assert r.status_code in (404, 405)


def test_logout_via_post_requires_a_token(client):
    _prime(client)
    client.cookies.set("irs_token", create_access_token(client.user_id))
    assert client.post("/ui/logout", follow_redirects=False).status_code == 403


def test_logout_via_post_with_token_succeeds(client):
    tok = _prime(client)
    client.cookies.set("irs_token", create_access_token(client.user_id))
    r = client.post("/ui/logout", data={FORM_FIELD: tok}, follow_redirects=False)
    assert r.status_code == 303


def test_get_requests_are_never_blocked(client):
    _prime(client)
    assert client.get("/ui/scans").status_code != 403
