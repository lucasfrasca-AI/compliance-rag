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

Then the account itself (#319). A signature only says Google signed the token
up to an hour ago, so the verifier also asks the Identity Toolkit API, as the
runtime identity (`IdentityToolkitAccounts`), whether the account still exists,
is enabled, and hasn't had its tokens revoked since the sign-in (`validSince`
later than the token's `auth_time`). It does this on every Bearer request. A
session cookie is checked the same way no less often than every
`SESSION_RECHECK_SECONDS` (`recheck`), so a disabled account loses its session
within that interval rather than at the cookie's expiry. A lookup that fails in
any way refuses (503, `VerifierUnavailable`): it never admits. What the lookup
returned never reaches a log or a span beyond `disabled` and the outcome.

The identity provider is not the control; `API_MEMBERS` is. Membership is
checked before the lookup, so an account that sign-up created is refused (403)
without a call, whatever the console says about sign-up.

Tests use this real verifier with locally generated keys standing in for
Google's. There is no fake verifier in `fakes.py`: it would be a way in.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

import httpx
from google.auth import exceptions as google_auth_exceptions
from google.auth import jwt

from compliance_rag.adapters.interfaces import (
    AccountDirectory,
    AccountStatus,
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
SESSION_RECHECK_SECONDS = 300.0
"""How long a session's account check stands (#319): a disabled account loses
a cookie session within this long. Every Bearer request checks afresh."""
MAX_REMEMBERED_SESSIONS = 4096
"""How many sessions' last checks one process keeps. Past it the oldest are
forgotten, which only means they are checked again."""
IDENTITY_TOOLKIT_LOOKUP_URL = (
    "https://identitytoolkit.googleapis.com/v1/projects/{project_id}/accounts:lookup"
)
IDENTITY_TOOLKIT_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
"""Either this or `.../auth/identitytoolkit` authorises `accounts:lookup`; the
first is what Application Default Credentials grant a Cloud Run identity."""
LOOKUP_TIMEOUT_SECONDS = 5.0


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


AccessTokenSource = Callable[[], Awaitable[str]]
"""An OAuth2 access token for the runtime identity. Never a user's token, never
a key file: Application Default Credentials (`adc_access_token`)."""


class _Credentials(Protocol):
    """The part of google-auth's credentials this reads (it ships untyped)."""

    valid: bool
    token: str | None

    def refresh(self, request: object) -> None: ...


def adc_access_token(scope: str = IDENTITY_TOOLKIT_SCOPE) -> AccessTokenSource:
    """Access tokens from Application Default Credentials (on Cloud Run, the
    service's own identity from the metadata server). google-auth is blocking,
    so the refresh runs in a thread, and one lock keeps a burst of requests to
    one refresh. No service-account key is read or created."""
    import google.auth
    import google.auth.transport.requests

    lock = asyncio.Lock()
    held: list[_Credentials] = []

    def refreshed() -> str:
        if not held:
            found = cast(
                "tuple[_Credentials, str | None]",
                google.auth.default(scopes=[scope]),  # pyright: ignore[reportUnknownMemberType]
            )
            held.append(found[0])
        credentials = held[0]
        if not credentials.valid:
            credentials.refresh(google.auth.transport.requests.Request())
        if not isinstance(credentials.token, str) or not credentials.token:
            raise RuntimeError("no access token")
        return credentials.token

    async def token() -> str:
        async with lock:
            return await asyncio.to_thread(refreshed)

    return token


class IdentityToolkitAccounts(AccountDirectory):
    """`AccountDirectory` over the Identity Toolkit API (#319): `POST
    /v1/projects/{project}/accounts:lookup` with `{"localId": [uid]}`,
    authorised by the runtime identity's access token (no API key). That identity
    needs `firebaseauth.users.get`, which `roles/firebaseauth.viewer` holds.

    Reads `users[0].localId`, `.disabled` and `.validSince` and nothing else of
    the response, which also carries the account's email and providers. Any
    failure, from a timeout to a body it doesn't understand, is
    `VerifierUnavailable`. Telemetry gets `auth.account_disabled` and the
    outcome, never an identifier.
    """

    def __init__(
        self,
        *,
        project_id: str,
        access_token: AccessTokenSource | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not project_id:
            raise ValueError("FIREBASE_PROJECT_ID is not set: no account could be looked up")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project_id) is None:
            raise ValueError("FIREBASE_PROJECT_ID is not a project id")
        self._url = IDENTITY_TOOLKIT_LOOKUP_URL.format(project_id=project_id)
        self._access_token = access_token or adc_access_token()
        self._client = client or httpx.AsyncClient(timeout=LOOKUP_TIMEOUT_SECONDS)

    async def lookup(self, uid: str) -> AccountStatus | None:
        with span("auth.lookup_account") as current:
            try:
                status = await self._lookup(uid)
            except VerifierUnavailable:
                current.set_attribute("auth.lookup_outcome", "failed")
                raise
            except Exception as failed:  # the token source, or anything unforeseen
                current.set_attribute("auth.lookup_outcome", "failed")
                raise VerifierUnavailable("couldn't look up the account") from failed
            current.set_attribute("auth.lookup_outcome", "found" if status else "not_found")
            if status is not None:
                current.set_attribute("auth.account_disabled", status.disabled)
            return status

    async def _lookup(self, uid: str) -> AccountStatus | None:
        unavailable = VerifierUnavailable("couldn't look up the account")
        token = await self._access_token()
        try:
            response = await self._client.post(
                self._url,
                json={"localId": [uid]},
                headers={"Authorization": f"Bearer {token}"},
            )
            body: object = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError) as failed:
            raise unavailable from failed
        if not isinstance(body, dict):
            raise unavailable
        fields = cast("dict[str, object]", body)
        users = fields.get("users")
        if users is None or users == []:
            return None  # the API leaves `users` out for an account that isn't there
        if not isinstance(users, list) or len(cast("list[object]", users)) != 1:
            raise unavailable
        user = cast("list[object]", users)[0]
        if not isinstance(user, dict):
            raise unavailable
        account = cast("dict[str, object]", user)
        disabled = account.get("disabled", False)  # JSON leaves out a false boolean
        valid_since = account.get("validSince", "0")  # an int64, so a string of seconds
        if account.get("localId") != uid or not isinstance(disabled, bool):
            raise unavailable
        if not isinstance(valid_since, str) or re.fullmatch(r"[0-9]{1,18}", valid_since) is None:
            raise unavailable
        return AccountStatus(disabled=disabled, valid_since=int(valid_since))

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


def is_member(email: str, members: Iterable[str]) -> bool:
    """Whether `email` is on the member list, compared case-insensitively. The
    one membership rule: the verifier applies it to a token's email, and the
    session cookie's reader to the cookie's (#280), so the two can't drift."""
    return email.lower() in {m.strip().lower() for m in members}


class FirebaseTokenVerifier(TokenVerifier):
    def __init__(
        self,
        *,
        project_id: str,
        members: frozenset[str],
        sign_in_providers: frozenset[str],
        keys: SigningKeySource,
        accounts: AccountDirectory,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not project_id:
            raise ValueError("FIREBASE_PROJECT_ID is not set: no token could be checked")
        self._accounts = accounts
        self._rechecked: dict[str, float] = {}
        """Session (a hash of its cookie) -> when its account was last checked."""
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
            or not is_member(email, self._members)
        ):
            raise NotAMember("not a member of this tenant")
        await self._require_active_account(uid, auth_time)
        expires = claims.get("exp")
        return Member(
            uid=uid,
            email=email.lower(),
            expires_at=int(expires) if isinstance(expires, int | float) else None,
            auth_time=int(auth_time),
        )

    async def _require_active_account(self, uid: str, auth_time: float) -> None:
        """The account exists, is enabled, and its tokens haven't been revoked
        since `auth_time`; otherwise a refusal. A lookup that fails in any way is
        `VerifierUnavailable`, never an admission (#319)."""
        with span("auth.check_account") as current:
            try:
                status = await self._accounts.lookup(uid)
            except VerifierUnavailable:
                current.set_attribute("auth.outcome", "lookup_failed")
                raise
            except Exception as failed:  # an adapter that let something through
                current.set_attribute("auth.outcome", "lookup_failed")
                raise VerifierUnavailable("couldn't look up the account") from failed
            if status is None:
                current.set_attribute("auth.outcome", "not_found")
                raise Unauthenticated("account not found")
            current.set_attribute("auth.account_disabled", status.disabled)
            if status.disabled:
                current.set_attribute("auth.outcome", "disabled")
                raise NotAMember("this account is disabled")
            if auth_time < status.valid_since:
                current.set_attribute("auth.outcome", "revoked")
                raise Unauthenticated("sign-in revoked")
            current.set_attribute("auth.outcome", "active")

    async def recheck(self, member: Member, *, session: str) -> None:
        """A session's account, looked up again unless it was within
        `SESSION_RECHECK_SECONDS`. Only a pass is remembered: a refusal or a
        failed lookup is looked up afresh by the next request."""
        if member.auth_time is None:
            raise Unauthenticated("invalid session")  # fail closed: nothing to check against
        now = self._clock()
        key = hashlib.sha256(session.encode()).hexdigest()
        checked = self._rechecked.pop(key, None)
        if checked is not None and 0 <= now - checked < SESSION_RECHECK_SECONDS:
            self._rechecked[key] = checked
            return
        await self._require_active_account(member.uid, member.auth_time)
        self._rechecked[key] = now
        while len(self._rechecked) > MAX_REMEMBERED_SESSIONS:
            del self._rechecked[next(iter(self._rechecked))]

    async def aclose(self) -> None:
        try:
            await self._source.aclose()
        finally:
            await self._accounts.aclose()
