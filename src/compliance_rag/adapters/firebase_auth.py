"""`TokenVerifier` for Firebase ID tokens (#146): who signed in, admitted to
the one configured tenant only if they're a member (PHASES.md Phase 3: single
tenant, `tenant_id` fixed).

A token is accepted only if all of these hold:
- **Structure, before any network call.** Its header names `RS256` and a
  string `kid`.
- **Signature.** Google signed it with the key for that `kid`.
- **Time.** It isn't expired, wasn't issued in the future, and signed someone
  in no later than now.
- **Audience and issuer.** `aud` is this Firebase project and `iss` is
  `https://securetoken.google.com/<project>`.
- **Subject.** It names a user: `sub` of 1-128 characters.
- **No Identity Platform tenant**, which shares the issuer and audience.

Then membership, because a Firebase account isn't membership: anyone can
create one in a project that allows sign-up. The sign-in provider must be one
whose "verified email" is proven (#145, default: Google and email/password,
not arbitrary OIDC/SAML providers that can assert any email). The email must
be verified and on the member list.

The signing keys are Google's published certificates, cached for as long as
Google says. When a fetch fails the verifier backs off rather than refetching
per request. It keeps using the last good keys for up to an hour, which can
reject a token signed by a brand-new key but never accepts one without a valid
signature. Fetching the keys is the API's one call outside Australia: a GET of
public certificates that carries no request data. The egress audit (PHASES.md
Phase 3) should list it.

Tests use this real verifier with locally generated keys standing in for
Google's. There is no fake in `fakes.py`: a fake verifier would be a way in.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast

import httpx
from google.auth import exceptions as google_auth_exceptions
from google.auth import jwt

from compliance_rag.adapters.interfaces import (
    Member,
    NotAMember,
    TokenVerifier,
    Unauthenticated,
    VerifierUnavailable,
)
from compliance_rag.telemetry import span

FIREBASE_CERTS_URL = (
    "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
)
DEFAULT_MAX_AGE_SECONDS = 3600.0
"""How long to keep keys whose response names no `max-age`."""
FETCH_BACKOFF_SECONDS = 30.0
"""After a failed fetch, how long before trying again."""
STALE_GRACE_SECONDS = 3600.0
"""How long past their `max-age` the last good keys stand in while fetches fail."""
CLOCK_SKEW_SECONDS = 30
MAX_UID_LENGTH = 128


@dataclass(frozen=True, slots=True)
class SigningKeys:
    certificates: Mapping[str, str]
    """Key id -> PEM certificate."""
    max_age: float
    """Seconds they may be kept."""


class SigningKeySource(ABC):
    @abstractmethod
    async def fetch(self) -> SigningKeys:
        """The current keys, or `VerifierUnavailable`."""

    @abstractmethod
    async def aclose(self) -> None: ...


class GoogleSigningKeys(SigningKeySource):
    """Google's published certificates for Firebase ID tokens."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def fetch(self) -> SigningKeys:
        with span("auth.fetch_signing_keys", **{"http.url": FIREBASE_CERTS_URL}) as current:
            try:
                response = await self._client.get(FIREBASE_CERTS_URL)
                response.raise_for_status()
                body: object = response.json()
            except (httpx.HTTPError, ValueError) as failed:
                current.set_attribute("auth.keys_fetched", False)
                raise VerifierUnavailable("couldn't fetch Firebase's signing keys") from failed
            if not isinstance(body, dict) or not body:
                current.set_attribute("auth.keys_fetched", False)
                raise VerifierUnavailable("Firebase's signing keys came back empty")
            keys = cast("dict[object, object]", body)
            max_age = re.search(r"max-age=(\d+)", response.headers.get("cache-control", ""))
            current.set_attribute("auth.keys_fetched", True)
            return SigningKeys(
                certificates={str(kid): str(pem) for kid, pem in keys.items()},
                max_age=float(max_age.group(1)) if max_age else DEFAULT_MAX_AGE_SECONDS,
            )

    async def aclose(self) -> None:
        await self._client.aclose()


def _key_id(token: str) -> str:
    """The token's `kid`, from a header that names RS256: checked before any
    network call, so a malformed token costs nothing."""
    parts = token.split(".")
    if len(parts) != 3:
        raise Unauthenticated("invalid token")
    try:
        header: object = json.loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise Unauthenticated("invalid token") from None
    if not isinstance(header, dict):
        raise Unauthenticated("invalid token")
    fields = cast("dict[str, object]", header)
    kid = fields.get("kid")
    if fields.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
        raise Unauthenticated("invalid token")
    return kid


class FirebaseTokenVerifier(TokenVerifier):
    def __init__(
        self,
        *,
        project_id: str,
        members: frozenset[str],
        sign_in_providers: frozenset[str],
        keys: SigningKeySource,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not project_id:
            raise ValueError("FIREBASE_PROJECT_ID is not set: no token could be checked")
        self._project_id = project_id
        self._issuer = f"https://securetoken.google.com/{project_id}"
        self._members = frozenset(m.strip().lower() for m in members)
        self._providers = sign_in_providers
        self._source = keys
        self._clock = clock
        self._certificates: Mapping[str, str] = {}
        self._fresh_until = float("-inf")
        self._retry_after = float("-inf")
        self._lock = asyncio.Lock()

    async def _keys(self) -> Mapping[str, str]:
        async with self._lock:
            now = self._clock()
            if now < self._fresh_until:
                return self._certificates
            usable_stale = (
                bool(self._certificates) and now < self._fresh_until + STALE_GRACE_SECONDS
            )
            if now < self._retry_after:
                if usable_stale:
                    return self._certificates
                raise VerifierUnavailable("signing keys unavailable; retrying shortly")
            try:
                fetched = await self._source.fetch()
            except VerifierUnavailable:
                self._retry_after = now + FETCH_BACKOFF_SECONDS
                if usable_stale:
                    return self._certificates
                raise
            self._certificates = fetched.certificates
            self._fresh_until = now + fetched.max_age
            return self._certificates

    async def verify(self, token: str) -> Member:
        kid = _key_id(token)
        certificate = (await self._keys()).get(kid)
        if certificate is None:
            raise Unauthenticated("invalid token")
        try:
            claims = cast(
                "Mapping[str, object]",
                jwt.decode(  # pyright: ignore[reportUnknownMemberType] — google-auth is untyped
                    token,
                    certs={kid: certificate},
                    audience=self._project_id,
                    clock_skew_in_seconds=CLOCK_SKEW_SECONDS,
                ),
            )
        except (google_auth_exceptions.GoogleAuthError, ValueError, TypeError, KeyError):
            # The reason stays out of the message: it could echo the token.
            raise Unauthenticated("invalid token") from None
        if claims.get("iss") != self._issuer:
            raise Unauthenticated("token not issued for this project")
        uid = claims.get("sub")
        if not isinstance(uid, str) or not 0 < len(uid) <= MAX_UID_LENGTH:
            raise Unauthenticated("token names no user")
        auth_time = claims.get("auth_time")
        if not isinstance(auth_time, int | float) or auth_time > time.time() + CLOCK_SKEW_SECONDS:
            raise Unauthenticated("invalid sign-in time")
        firebase = claims.get("firebase")
        details = cast("dict[str, object]", firebase) if isinstance(firebase, dict) else {}
        if "tenant" in details:
            raise Unauthenticated("token is for an Identity Platform tenant")
        email = claims.get("email")
        if (
            details.get("sign_in_provider") not in self._providers
            or not isinstance(email, str)
            or claims.get("email_verified") is not True
            or email.lower() not in self._members
        ):
            raise NotAMember("not a member of this tenant")
        return Member(uid=uid, email=email.lower())

    async def aclose(self) -> None:
        await self._source.aclose()
