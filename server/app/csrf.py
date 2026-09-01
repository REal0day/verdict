"""CSRF protection for the cookie-authenticated `/ui/*` surface.

The JSON API is safe without this: it authenticates with an
`Authorization: Bearer` header, which an attacker's page cannot set on a
cross-origin request. The server-rendered `/ui/*` forms are different — they
ride on the `irs_token` cookie, which is ambient authority the browser
attaches automatically.

`samesite=lax` on that cookie already blocks cross-site POSTs in current
browsers, but it is one flag away from being the only thing standing between
a forged form and a state change, and it does nothing for older browsers or
for state-changing GETs. This adds a real second factor: a double-submit
token that must appear in both the `irs_csrf` cookie and the request itself.

The cookie is httponly — the token reaches forms because the server renders
it into a hidden field from `request.state.csrf_token`, so no JavaScript ever
needs to read it.
"""
from __future__ import annotations

import secrets
from urllib.parse import parse_qsl

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import PlainTextResponse

COOKIE_NAME = "irs_csrf"
FORM_FIELD = "csrf_token"
HEADER_NAME = "x-csrf-token"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
# Only the cookie-authenticated server-rendered surface needs this.
PROTECTED_PREFIXES = ("/ui/",)
# Bodies are small form posts; refuse to buffer anything unreasonable.
MAX_BUFFERED_BODY = 1024 * 1024


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


class CSRFMiddleware:
    """Double-submit-cookie CSRF guard, as pure ASGI.

    Written at the ASGI layer rather than as BaseHTTPMiddleware because
    validating a form field means reading the request body, and the body has
    to be replayed afterwards or the route handler receives an empty stream.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = Headers(scope=scope)
        cookie_token = Request(scope).cookies.get(COOKIE_NAME)

        # Every response gets a token to render into forms; mint one on first
        # contact so the login form itself is already protected.
        issue = cookie_token or new_csrf_token()
        scope.setdefault("state", {})["csrf_token"] = issue

        if self._needs_check(scope, headers):
            submitted = headers.get(HEADER_NAME)
            if submitted is None:
                body, receive = await self._buffer_body(receive)
                submitted = self._field_from_body(body, headers.get("content-type", ""))
            if not _token_ok(cookie_token, submitted):
                resp = PlainTextResponse(
                    "CSRF token missing or invalid. Reload the page and try again.",
                    status_code=403,
                )
                return await resp(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start" and cookie_token is None:
                MutableHeaders(scope=message).append(
                    "set-cookie",
                    f"{COOKIE_NAME}={issue}; Path=/; HttpOnly; SameSite=Lax",
                )
            await send(message)

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    def _needs_check(scope, headers: Headers) -> bool:
        if scope["method"] in SAFE_METHODS:
            return False
        if not any(scope["path"].startswith(p) for p in PROTECTED_PREFIXES):
            return False
        # A Bearer request proves the caller could read a header we issued;
        # it is not riding on an ambient cookie, so it cannot be forged.
        return not headers.get("authorization", "").lower().startswith("bearer ")

    @staticmethod
    async def _buffer_body(receive):
        """Drain the request body, then hand back a receive() that replays it."""
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body", b""))
            size += len(chunks[-1])
            if size > MAX_BUFFERED_BODY or not message.get("more_body", False):
                break
        body = b"".join(chunks)

        sent = False

        async def replay():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return body, replay

    @staticmethod
    def _field_from_body(body: bytes, content_type: str) -> str | None:
        if "application/x-www-form-urlencoded" not in content_type:
            return None
        try:
            decoded = body.decode("utf-8")
        except UnicodeDecodeError:
            return None
        for key, value in parse_qsl(decoded, keep_blank_values=True):
            if key == FORM_FIELD:
                return value
        return None


def _token_ok(cookie_token: str | None, submitted: str | None) -> bool:
    if not cookie_token or not submitted:
        return False
    return secrets.compare_digest(cookie_token, submitted)
