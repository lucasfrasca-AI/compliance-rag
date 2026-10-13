"""Signing in (issue #146): a Firebase ID token, verified here, resolved to a
member of the one configured tenant (PHASES.md Phase 3: single tenant,
`tenant_id` fixed).

A token is accepted only if Google signed it with one of the keys published at
`FIREBASE_CERTS_URL`, it hasn't expired, it was issued for this Firebase
project (`aud`) by `https://securetoken.google.com/<project>` (`iss`), and it
names a user (`sub`). A Firebase account isn't membership: anyone can create
one in a project that allows sign-up. So the email must also be verified and
on the configured member list (#145, default 1). The tenant comes from
configuration, never from the request.

Fetching the signing keys is the one call this makes outside Australia: a GET
of Google's public certificates that carries no request data. The egress audit
(PHASES.md Phase 3) should list it.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

import httpx
from google.auth import exceptions as google_auth_exceptions
from google.auth import jwt

FIREBASE_CERTS_URL = (
    "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
)
DEFAULT_CERTS_MAX_AGE = 3600.0
"""Seconds to keep keys whose response names no `max-age`."""
CLOCK_SKEW_SECONDS = 30


@dataclass(frozen=True, slots=True)
class Member:
    """A signed-in person admitted to the tenant."""

    uid: str
    email: str


class Unauthenticated(Exception):
    """No valid token: 401."""


class NotAMember(Exception):
    """A valid token for someone not admitted to the tenant: 403."""


class VerifierUnavailable(Exception):
    """The signing keys couldn't be fetched, so no token can be checked: 503."""


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> Member: ...

    async def aclose(self) -> None: ...


Certs = tuple[Mapping[str, str], float]
"""Key id -> PEM certificate, and how many seconds they may be kept."""


class FirebaseCerts:
    """Google's published signing certificates for Firebase ID tokens."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def __call__(self) -> Certs:
        try:
            response = await self._client.get(FIREBASE_CERTS_URL)
            response.raise_for_status()
            certs: object = response.json()
        except (httpx.HTTPError, ValueError) as failed:
            raise VerifierUnavailable("couldn't fetch Firebase's signing keys") from failed
        if not isinstance(certs, dict) or not certs:
            raise VerifierUnavailable("Firebase's signing keys came back empty")
        keys = cast("dict[object, object]", certs)
        max_age = re.search(r"max-age=(\d+)", response.headers.get("cache-control", ""))
        return (
            {str(kid): str(pem) for kid, pem in keys.items()},
            float(max_age.group(1)) if max_age else DEFAULT_CERTS_MAX_AGE,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class FirebaseTokenVerifier:
    """`TokenVerifier` for Firebase ID tokens, keys cached for as long as
    Google says they may be."""

    def __init__(
        self,
        *,
        project_id: str,
        members: frozenset[str],
        fetch_certs: Callable[[], Awaitable[Certs]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not project_id:
            raise ValueError("FIREBASE_PROJECT_ID is not set: no token could be checked")
        self._project_id = project_id
        self._issuer = f"https://securetoken.google.com/{project_id}"
        self._members = frozenset(m.lower() for m in members)
        self._fetch_certs = fetch_certs
        self._clock = clock
        self._certs: Mapping[str, str] = {}
        self._fresh_until = float("-inf")
        self._lock = asyncio.Lock()

    async def _keys(self) -> Mapping[str, str]:
        async with self._lock:
            if self._clock() >= self._fresh_until:
                certs, max_age = await self._fetch_certs()
                self._certs, self._fresh_until = certs, self._clock() + max_age
            return self._certs

    async def verify(self, token: str) -> Member:
        certs = await self._keys()
        try:
            claims = cast(
                "Mapping[str, object]",
                jwt.decode(  # pyright: ignore[reportUnknownMemberType] — google-auth is untyped
                    token,
                    certs=dict(certs),
                    audience=self._project_id,
                    clock_skew_in_seconds=CLOCK_SKEW_SECONDS,
                ),
            )
        except (google_auth_exceptions.GoogleAuthError, ValueError):
            # The reason stays out of the message: it could echo the token.
            raise Unauthenticated("invalid token") from None
        if claims.get("iss") != self._issuer:
            raise Unauthenticated("token not issued for this project")
        uid = claims.get("sub")
        if not isinstance(uid, str) or not uid:
            raise Unauthenticated("token names no user")
        email = claims.get("email")
        if (
            not isinstance(email, str)
            or claims.get("email_verified") is not True
            or email.lower() not in self._members
        ):
            raise NotAMember("not a member of this tenant")
        return Member(uid=uid, email=email.lower())

    async def aclose(self) -> None:
        close = getattr(self._fetch_certs, "aclose", None)
        if close is not None:
            await close()
