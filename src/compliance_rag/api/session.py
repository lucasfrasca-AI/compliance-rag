"""A signed-in session for server-rendered pages (#280, ADR-0053 decision 19).

A page can't hold a Bearer token the way a script does, so a member signs in
with Firebase (the vendored web SDK, `static/`), the page hands the ID token to
`POST /session`, and the API swaps it for a cookie:

- **signed**: `payload.signature`, HMAC-SHA256 under `Settings.session_secret`.
  The payload holds the uid, the email, an expiry and the token's `auth_time`
  (what an account's revoked-since time is compared with, #319), nothing else;
- **`HttpOnly`, `Secure`, `SameSite=Strict`**, named `__Host-session` so a
  browser also refuses one set with a `Domain` or from a sibling host;
- **expiring no later than the token** it was made from, and in any case within
  an hour.

`deps.current_member` accepts the cookie as well as the Bearer header, with the
same outcomes. Membership is checked again when a cookie is read, so removing a
member from `API_MEMBERS` ends their sessions at once rather than at expiry. The
account is looked up again too, at most every `SESSION_RECHECK_SECONDS` per
session (`TokenVerifier.recheck`), so a disabled account loses its sessions
within that interval (#319).

Every state-changing request that carries the cookie must also pass an Origin
check (`OriginGuard`), on top of `SameSite=Strict`. Sign-in is always required;
there is no anonymous path.

**The cookie is stateless.** Nothing about a session is stored on the server, so
`POST /session/logout` clears the browser's copy and nothing else: a copy of the
cookie taken before, if stolen, still signs in until its expiry (at most an
hour, and never past the token's). Removing a member from `API_MEMBERS` ends
their sessions at once, since membership is checked on every read; rotating
`SESSION_SECRET` ends everyone's.

The secret is never logged, never put on a span and never in an error message.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import cast
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from compliance_rag.adapters.interfaces import Member, Unauthenticated
from compliance_rag.settings import MIN_SESSION_SECRET_CHARS

COOKIE_NAME = "__Host-session"
MAX_SESSION_SECONDS = 3600
"""A session never outlives this, whatever its token's expiry (Firebase ID
tokens last an hour, so the token is the usual limit)."""

_VERSION = 1
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "[::1]"})
_MAX_COOKIE_CHARS = 1024


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SessionCodec:
    """Makes and reads session cookie values under one secret."""

    def __init__(self, secret: str, *, clock: Callable[[], float] = time.time) -> None:
        # Fail closed here too, not only in Settings: a codec built from a
        # short or empty key would sign cookies anyone could forge.
        if len(secret) < MIN_SESSION_SECRET_CHARS:
            raise ValueError(
                f"the session secret must be at least {MIN_SESSION_SECRET_CHARS} characters"
            )
        self._key = secret.encode()
        self._clock = clock

    def _signature(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode(), hashlib.sha256).digest())

    def seal(self, member: Member, *, expires_at: int) -> str:
        """The cookie value for `member`, valid until `expires_at` (Unix seconds)."""
        body = json.dumps(
            {
                "v": _VERSION,
                "uid": member.uid,
                "email": member.email,
                "exp": expires_at,
                "auth_time": member.auth_time,
            },
            separators=(",", ":"),
        )
        payload = _b64(body.encode())
        return f"{payload}.{self._signature(payload)}"

    def max_age(self, expires_at: int) -> int:
        """Seconds from now, by this codec's clock, until `expires_at`: the
        cookie's `Max-Age`. Not positive means already over."""
        return expires_at - int(self._clock())

    def expiry_for(self, member: Member) -> int:
        """When a session made now from `member`'s token ends: with the token,
        and within `MAX_SESSION_SECONDS`."""
        limit = int(self._clock()) + MAX_SESSION_SECONDS
        return min(limit, member.expires_at) if member.expires_at is not None else limit

    def open(self, value: str) -> Member:
        """The member a cookie value signs in, or `Unauthenticated`. The reason
        never echoes the value."""
        if len(value) > _MAX_COOKIE_CHARS:
            raise Unauthenticated("invalid session")
        payload, _, signature = value.partition(".")
        if not hmac.compare_digest(signature.encode(), self._signature(payload).encode()):
            raise Unauthenticated("invalid session")
        try:
            claims = cast("object", json.loads(_unb64(payload)))
        except (ValueError, binascii.Error):
            raise Unauthenticated("invalid session") from None
        if not isinstance(claims, dict):
            raise Unauthenticated("invalid session")
        fields = cast("dict[str, object]", claims)
        uid, email, expires = fields.get("uid"), fields.get("email"), fields.get("exp")
        signed_in = fields.get("auth_time")
        if (
            fields.get("v") != _VERSION
            or not isinstance(uid, str)
            or not uid
            or not isinstance(email, str)
            or not email
            or not isinstance(expires, int)
            or isinstance(expires, bool)
            # The sign-in the account's revocation is checked against (#319). A
            # cookie without it can't be checked, so it isn't a session.
            or not isinstance(signed_in, int)
            or isinstance(signed_in, bool)
        ):
            raise Unauthenticated("invalid session")
        if expires <= self._clock():
            raise Unauthenticated("session expired")
        return Member(uid=uid, email=email, expires_at=expires, auth_time=signed_in)


def codec_for(secret: str) -> SessionCodec | None:
    """The codec for `Settings.session_secret`; none when it is unset, which
    turns cookie sessions off."""
    return SessionCodec(secret) if secret else None


# ---- CSRF ---------------------------------------------------------------------------


def origin_allowed(headers: Headers, extra: frozenset[str]) -> bool:
    """Whether the request's `Origin` is this service's own (the `Host` it was
    sent to, over https, or http on a loopback host) or a configured one. No
    Origin, or `null`, is refused."""
    origin = headers.get("origin")
    if not origin:
        return False
    origin = origin.rstrip("/").lower()
    if origin in extra:
        return True
    parts = urlsplit(origin)
    host = headers.get("host", "").lower()
    if not parts.hostname or not host or parts.netloc != host:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOOPBACK)


class OriginGuard:
    """Refuses (403) a state-changing request that carries the session cookie
    unless its Origin is allowed (`origin_allowed`). Pure ASGI, so no route can
    forget it and nothing is read from the body."""

    def __init__(self, app: ASGIApp, *, extra_origins: frozenset[str] = frozenset()) -> None:
        self.app = app
        self._extra = extra_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and str(scope["method"]) not in _SAFE_METHODS:
            headers = Headers(scope=scope)
            carries_cookie = COOKIE_NAME in cookie_parser(headers.get("cookie", ""))
            if carries_cookie and not origin_allowed(headers, self._extra):
                refusal = JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
                await refusal(scope, receive, send)
                return
        await self.app(scope, receive, send)
