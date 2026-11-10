"""AWS credentials for Bedrock with no long-lived key anywhere (#322, ADR-0056).

Two ways to get short-lived credentials, chosen by `Settings`:

- **On Cloud Run — web-identity federation.** The runtime service account's
  Google-signed ID token is fetched from the metadata server and exchanged
  through STS `AssumeRoleWithWebIdentity` for `AWS_ROLE_ARN`. The role's trust
  policy names the `accounts.google.com` federated principal and pins the
  service account's unique id (`accounts.google.com:aud`, which AWS fills from
  the token's `azp` claim) and the audience this module asks for
  (`accounts.google.com:oaud`). STS is called at its **regional** endpoint
  (`sts.ap-southeast-2.amazonaws.com`), unsigned: the token is the credential.
  The result is cached and renewed before it expires.
- **Locally — a named profile** (`AWS_PROFILE`, IAM Identity Center or SSO).
  botocore resolves and refreshes it; this module only asks it for the current
  values.

Neither path reads the long-lived access-key environment variables (the pair
`AWS_*_KEY_*` that AWS's tools and SDKs honour), and nothing here falls back to
botocore's default credential chain (which would). A test pins that no source
file names either variable. The profile path names its
profile explicitly, which makes botocore skip the environment provider
(`tests/test_aws_credentials.py` proves it with a key in the environment).

The generator takes a `CredentialsProvider`, so tests inject a fake; STS and
the ID-token source are small protocols for the same reason. Nothing in this
module is imported by the retrieval path.

A secret never reaches a log, a span or an exception message: `AwsCredentials`
redacts itself in `repr`, and errors here say what failed, never what the
provider returned.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import botocore.session
import httpx
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from compliance_rag.settings import Settings

METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)
"""The Cloud Run / GCE metadata server's ID-token endpoint, for the runtime
service account."""

ROLE_SESSION_NAME = "compliance-rag-api"
"""Shows in CloudTrail against every call the session makes."""

SESSION_SECONDS = 3600
"""STS's default and the role's default maximum."""

REFRESH_MARGIN = dt.timedelta(minutes=5)
"""Credentials with less than this left are renewed before they are used."""

_REFUSED_CODES = frozenset(
    {"AccessDenied", "AccessDeniedException", "InvalidIdentityToken", "IDPRejectedClaim"}
)
"""STS error codes that mean the trust policy or the token was refused, as
opposed to STS being unreachable or throttled. `ExpiredTokenException`,
`InvalidSignatureException` and the other clock or signature codes are left
out on purpose: they are this process's fault (a skewed clock), not the
account's, and surface as internal errors."""

_CLOCK_WORDS = ("expired", "not yet valid", "clock", "skew")
"""`InvalidIdentityToken` is also what STS says of a token whose times are
wrong; a message with these words is a clock problem, not a trust policy."""

RETRY_AFTER = dt.timedelta(seconds=30)
"""After a failed renewal, how long still-valid credentials are used before
STS is asked again, so a struggling STS isn't hit on every request."""

START_OF_TIME = dt.datetime.min.replace(tzinfo=dt.UTC)


class CredentialsUnavailable(RuntimeError):
    """No credentials could be had. `refused` is True when AWS or the identity
    provider said no (a trust policy that doesn't match, an SSO session that
    has lapsed): that is an access problem the operator fixes, shown to the
    member as `access`. False is anything else (a network failure, a malformed
    reply), which is an internal error."""

    def __init__(self, message: str, *, refused: bool) -> None:
        super().__init__(message)
        self.refused = refused


@dataclass(frozen=True, slots=True)
class AwsCredentials:
    """Short-lived credentials. `expires_at` is None when the source doesn't
    say (a profile: botocore renews it)."""

    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(repr=False)
    expires_at: dt.datetime | None = None

    def __repr__(self) -> str:
        return f"AwsCredentials(expires_at={self.expires_at!r}, <redacted>)"


class CredentialsProvider(Protocol):
    async def credentials(self) -> AwsCredentials:
        """Credentials good for the next request, renewed if they are about to
        lapse. Raises `CredentialsUnavailable`."""
        ...


class IdTokenSource(Protocol):
    async def id_token(self, audience: str) -> str:
        """A Google-signed ID token for the runtime service account, for
        `audience`."""
        ...


class StsLike(Protocol):
    """The one STS call this module makes, in botocore's own shape."""

    def assume_role_with_web_identity(
        self, *, RoleArn: str, RoleSessionName: str, WebIdentityToken: str, DurationSeconds: int
    ) -> Mapping[str, object]: ...


class GoogleMetadataIdToken:
    """The runtime service account's ID token from the metadata server, which
    exists on Cloud Run and nowhere else. The token's `azp` and `sub` are the
    service account's unique id; its `aud` is `audience`."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def id_token(self, audience: str) -> str:
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(5.0))
        try:
            response = await client.get(
                METADATA_IDENTITY_URL,
                params={"audience": audience},
                headers={"Metadata-Flavor": "Google"},
            )
            response.raise_for_status()
        except httpx.HTTPError as failed:
            raise CredentialsUnavailable(
                "the metadata server gave no ID token (is this Cloud Run?)", refused=False
            ) from failed
        finally:
            if self._client is None:
                await client.aclose()
        token = response.text.strip()
        if not token:
            raise CredentialsUnavailable("the metadata server's ID token was empty", refused=False)
        return token


def regional_sts(region: str) -> StsLike:
    """botocore's STS client at the **regional** endpoint, unsigned:
    `AssumeRoleWithWebIdentity` is authorised by the token alone, so no
    credential is resolved for it (and so the environment's keys, if any,
    are never looked at)."""
    session = botocore.session.Session()
    return cast(
        "StsLike",
        session.create_client(  # pyright: ignore[reportUnknownMemberType]
            "sts",
            region_name=region,
            endpoint_url=f"https://sts.{region}.amazonaws.com",
            config=Config(
                signature_version=UNSIGNED,
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=10,
            ),
        ),
    )


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _credentials_of(response: Mapping[str, object]) -> AwsCredentials:
    """STS's reply, checked. Anything but the documented shape is an internal
    error, never a guess."""
    raw = response.get("Credentials")
    if not isinstance(raw, Mapping):
        raise CredentialsUnavailable("STS's reply held no credentials", refused=False)
    fields = cast("Mapping[str, object]", raw)
    key, secret = fields.get("AccessKeyId"), fields.get("SecretAccessKey")
    token, expiry = fields.get("SessionToken"), fields.get("Expiration")
    if not (
        isinstance(key, str)
        and isinstance(secret, str)
        and isinstance(token, str)
        and isinstance(expiry, dt.datetime)
    ):
        raise CredentialsUnavailable("STS's credentials were malformed", refused=False)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=dt.UTC)
    return AwsCredentials(key, secret, token, expiry)


class WebIdentityCredentials:
    """Cloud Run: a Google ID token for `role_arn`, renewed with a margin."""

    def __init__(
        self,
        *,
        role_arn: str,
        tokens: IdTokenSource,
        sts: StsLike,
        audience: str,
        now: Callable[[], dt.datetime] = _utcnow,
        refresh_margin: dt.timedelta = REFRESH_MARGIN,
        session_seconds: int = SESSION_SECONDS,
    ) -> None:
        if not role_arn:
            raise ValueError("no AWS role configured (AWS_ROLE_ARN)")
        self._role_arn = role_arn
        self._tokens = tokens
        self._sts = sts
        self._audience = audience
        self._now = now
        self._margin = refresh_margin
        self._seconds = session_seconds
        self._current: AwsCredentials | None = None
        self._next_attempt = START_OF_TIME
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        current = self._current
        return (
            current is not None
            and current.expires_at is not None
            and current.expires_at - self._margin > self._now()
        )

    def _still_valid(self) -> bool:
        current = self._current
        return (
            current is not None
            and current.expires_at is not None
            and current.expires_at > self._now()
        )

    async def credentials(self) -> AwsCredentials:
        if self._current is not None and self._fresh():
            return self._current
        async with self._lock:
            if self._current is not None and self._fresh():  # another request renewed
                return self._current
            if (
                self._current is not None
                and self._still_valid()
                and self._now() < self._next_attempt
            ):
                return self._current  # a renewal failed lately: ask again soon
            try:
                self._current = await self._exchange()
            except CredentialsUnavailable:
                # A renewal that fails inside the margin costs nothing while
                # the credentials in hand are still good: keep using them, and
                # fail only once they have actually expired.
                if self._current is not None and self._still_valid():
                    self._next_attempt = self._now() + RETRY_AFTER
                    return self._current
                raise
            return self._current

    async def _exchange(self) -> AwsCredentials:
        token = await self._tokens.id_token(self._audience)
        try:
            response = await asyncio.to_thread(
                self._sts.assume_role_with_web_identity,
                RoleArn=self._role_arn,
                RoleSessionName=ROLE_SESSION_NAME,
                WebIdentityToken=token,
                DurationSeconds=self._seconds,
            )
        except ClientError as refused:
            detail = cast("Mapping[str, object]", refused.response).get("Error")
            fields: Mapping[str, object] = {}
            if isinstance(detail, Mapping):
                fields = cast("Mapping[str, object]", detail)
            code = str(fields.get("Code", ""))
            said = str(fields.get("Message", "")).lower()
            clock = code == "InvalidIdentityToken" and any(w in said for w in _CLOCK_WORDS)
            raise CredentialsUnavailable(
                f"STS refused the web-identity exchange ({code or 'no code'})",
                refused=code in _REFUSED_CODES and not clock,
            ) from refused
        except BotoCoreError as failed:
            raise CredentialsUnavailable(
                "STS could not be reached for the web-identity exchange", refused=False
            ) from failed
        return _credentials_of(response)


class ProfileCredentials:
    """Locally: a named profile, resolved and refreshed by botocore. The
    profile is named explicitly, which is what stops botocore reading the
    environment's keys."""

    def __init__(self, profile: str) -> None:
        if not profile:
            raise ValueError("no AWS profile configured (AWS_PROFILE)")
        self._profile = profile
        self._session: Any = botocore.session.Session(profile=profile)  # Any: botocore is untyped

    def _resolve(self) -> AwsCredentials:
        try:
            resolved = self._session.get_credentials()
            if resolved is None:
                raise CredentialsUnavailable(
                    f"AWS profile {self._profile!r} resolves no credentials: run "
                    f"`aws sso login --profile {self._profile}`",
                    refused=True,
                )
            frozen = resolved.get_frozen_credentials()
        except BotoCoreError as failed:
            raise CredentialsUnavailable(
                f"AWS profile {self._profile!r} could not be used ({type(failed).__name__}): "
                f"run `aws sso login --profile {self._profile}`",
                refused=True,
            ) from failed
        if not frozen.token:
            # Long-lived keys have no session token. A profile may hold only
            # short-lived credentials (SSO, an assumed role); anything else is a
            # key in a file, which this application never accepts.
            raise CredentialsUnavailable(
                f"AWS profile {self._profile!r} resolves to long-lived access keys (no session "
                "token): use an IAM Identity Center (SSO) profile",
                refused=True,
            )
        return AwsCredentials(
            cast("str", frozen.access_key),
            cast("str", frozen.secret_key),
            cast("str", frozen.token),
        )

    async def credentials(self) -> AwsCredentials:
        return await asyncio.to_thread(self._resolve)


def credentials_for(settings: Settings) -> CredentialsProvider:
    """The provider `Settings` names: `AWS_ROLE_ARN` (Cloud Run, web identity)
    or `AWS_PROFILE` (local). Neither, or both, is refused rather than guessed
    at: with neither, botocore's default chain would pick up whatever keys the
    environment holds."""
    if settings.aws_role_arn and settings.aws_profile:
        raise ValueError(
            "set AWS_ROLE_ARN (Cloud Run) or AWS_PROFILE (local), not both: which one "
            "signs the request must not depend on guesswork"
        )
    if settings.aws_role_arn:
        return WebIdentityCredentials(
            role_arn=settings.aws_role_arn,
            tokens=GoogleMetadataIdToken(),
            sts=regional_sts(settings.bedrock_region),
            audience=settings.aws_web_identity_audience,
        )
    if settings.aws_profile:
        return ProfileCredentials(settings.aws_profile)
    raise ValueError(
        "GENERATION_BACKEND=bedrock needs AWS_ROLE_ARN (Cloud Run) or AWS_PROFILE (local); "
        "there is no default, because the default credential chain reads long-lived keys"
    )
