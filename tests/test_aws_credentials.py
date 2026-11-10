"""AWS credentials with no long-lived key anywhere (#322, ADR-0056).

Cloud Run's path is proved against a fake STS and a fake token source; the
real botocore STS client is only built (and driven through botocore's own
`Stubber`), never sent anywhere. The local path is proved against a profile in
a temporary shared-credentials file, with a different key planted in the
environment to show it is not the one that is used.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import re
from collections.abc import Mapping
from typing import Any

import botocore.exceptions
import httpx
import pytest
from botocore import UNSIGNED
from botocore.stub import Stubber

from compliance_rag.adapters.aws_credentials import (
    METADATA_IDENTITY_URL,
    ROLE_SESSION_NAME,
    AwsCredentials,
    CredentialsUnavailable,
    GoogleMetadataIdToken,
    ProfileCredentials,
    WebIdentityCredentials,
    credentials_for,
    regional_sts,
)
from compliance_rag.settings import Settings

ROOT = pathlib.Path(__file__).resolve().parents[1]
ROLE = "arn:aws:iam::123456789012:role/compliance-rag-bedrock"
AUDIENCE = "sts.amazonaws.com"
START = dt.datetime(2026, 11, 10, 9, 0, tzinfo=dt.UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> dt.datetime:
        return self.now


class FakeTokens:
    def __init__(self) -> None:
        self.audiences: list[str] = []

    async def id_token(self, audience: str) -> str:
        self.audiences.append(audience)
        return f"google.id.token.{len(self.audiences)}"


class FakeSts:
    """STS in botocore's reply shape. `fail` raises it on the next call."""

    def __init__(self, clock: Clock, lifetime: dt.timedelta = dt.timedelta(hours=1)) -> None:
        self.calls: list[dict[str, Any]] = []  # Any: recorded request kwargs
        self.fail: Exception | None = None
        self.reply: Mapping[str, object] | None = None
        self._clock = clock
        self._lifetime = lifetime

    def assume_role_with_web_identity(
        self, *, RoleArn: str, RoleSessionName: str, WebIdentityToken: str, DurationSeconds: int
    ) -> Mapping[str, object]:
        self.calls.append(
            {
                "RoleArn": RoleArn,
                "RoleSessionName": RoleSessionName,
                "WebIdentityToken": WebIdentityToken,
                "DurationSeconds": DurationSeconds,
            }
        )
        if self.fail is not None:
            raise self.fail
        if self.reply is not None:
            return self.reply
        return {
            "Credentials": {
                "AccessKeyId": f"ASIAFAKE{len(self.calls)}",
                "SecretAccessKey": f"fake-secret-{len(self.calls)}",
                "SessionToken": f"fake-token-{len(self.calls)}",
                "Expiration": self._clock.now + self._lifetime,
            }
        }


def _provider(clock: Clock, sts: FakeSts, tokens: FakeTokens) -> WebIdentityCredentials:
    return WebIdentityCredentials(
        role_arn=ROLE, tokens=tokens, sts=sts, audience=AUDIENCE, now=clock
    )


# ---- Cloud Run: web-identity federation --------------------------------------------------


async def test_the_google_id_token_is_exchanged_through_sts_for_the_configured_role() -> None:
    clock, tokens = Clock(), FakeTokens()
    sts = FakeSts(clock)
    credentials = await _provider(clock, sts, tokens).credentials()
    assert tokens.audiences == [AUDIENCE]
    assert sts.calls == [
        {
            "RoleArn": ROLE,
            "RoleSessionName": ROLE_SESSION_NAME,
            "WebIdentityToken": "google.id.token.1",
            "DurationSeconds": 3600,
        }
    ]
    assert credentials.access_key_id == "ASIAFAKE1" and credentials.session_token == "fake-token-1"
    assert credentials.expires_at == START + dt.timedelta(hours=1)


async def test_credentials_are_reused_until_they_are_about_to_lapse_then_renewed() -> None:
    clock, tokens = Clock(), FakeTokens()
    sts = FakeSts(clock)
    provider = _provider(clock, sts, tokens)
    first = await provider.credentials()
    clock.now = START + dt.timedelta(minutes=54)  # six minutes left: still good
    assert await provider.credentials() == first and len(sts.calls) == 1
    clock.now = START + dt.timedelta(minutes=56)  # four minutes left: inside the margin
    renewed = await provider.credentials()
    assert renewed != first and renewed.access_key_id == "ASIAFAKE2" and len(sts.calls) == 2
    assert tokens.audiences == [AUDIENCE, AUDIENCE]  # a fresh ID token each time


async def test_concurrent_requests_share_one_exchange() -> None:
    clock, tokens = Clock(), FakeTokens()
    sts = FakeSts(clock)
    provider = _provider(clock, sts, tokens)
    results = await asyncio.gather(*(provider.credentials() for _ in range(8)))
    assert len(sts.calls) == 1 and len(set(results)) == 1


@pytest.mark.parametrize("code", ["AccessDenied", "InvalidIdentityToken", "ExpiredTokenException"])
async def test_sts_refusing_the_token_or_the_trust_policy_is_a_refusal(code: str) -> None:
    clock = Clock()
    sts = FakeSts(clock)
    sts.fail = botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": "Not authorized: arn:aws:iam::1:role/x"}},
        "AssumeRoleWithWebIdentity",
    )
    with pytest.raises(CredentialsUnavailable) as raised:
        await _provider(clock, sts, FakeTokens()).credentials()
    assert raised.value.refused is True
    assert "arn:aws" not in str(raised.value) and "Not authorized" not in str(raised.value)


async def test_throttling_malformed_replies_and_unreachable_sts_are_not_refusals() -> None:
    clock = Clock()
    sts = FakeSts(clock)
    sts.fail = botocore.exceptions.ClientError(
        {"Error": {"Code": "Throttling", "Message": "slow down"}}, "AssumeRoleWithWebIdentity"
    )
    with pytest.raises(CredentialsUnavailable) as throttled:
        await _provider(clock, sts, FakeTokens()).credentials()
    sts.fail = botocore.exceptions.EndpointConnectionError(endpoint_url="https://sts.example")
    with pytest.raises(CredentialsUnavailable) as unreachable:
        await _provider(clock, sts, FakeTokens()).credentials()
    sts.fail, sts.reply = None, {"Credentials": {"AccessKeyId": "x"}}
    with pytest.raises(CredentialsUnavailable) as malformed:
        await _provider(clock, sts, FakeTokens()).credentials()
    assert [e.value.refused for e in (throttled, unreachable, malformed)] == [False] * 3


def test_credentials_never_show_a_secret_in_their_repr() -> None:
    shown = repr(AwsCredentials("ASIAFAKE", "fake-secret", "fake-token", START))
    assert "ASIAFAKE" not in shown and "fake-secret" not in shown and "fake-token" not in shown


async def test_the_id_token_comes_from_the_metadata_server_for_the_configured_audience() -> None:
    seen: list[httpx.Request] = []

    def metadata(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="eyJ.fake.jwt\n")

    client = httpx.AsyncClient(transport=httpx.MockTransport(metadata))
    token = await GoogleMetadataIdToken(client).id_token(AUDIENCE)
    (request,) = seen
    assert token == "eyJ.fake.jwt"
    assert str(request.url).startswith(METADATA_IDENTITY_URL)
    assert request.url.params["audience"] == AUDIENCE
    assert request.headers["Metadata-Flavor"] == "Google"


async def test_no_metadata_server_is_an_internal_fault_not_a_refusal() -> None:
    def down(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = httpx.AsyncClient(transport=httpx.MockTransport(down))
    with pytest.raises(CredentialsUnavailable) as raised:
        await GoogleMetadataIdToken(client).id_token(AUDIENCE)
    assert raised.value.refused is False


# ---- the real STS client, built but never sent -------------------------------------------


def test_sts_is_called_at_sydneys_regional_endpoint_unsigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsigned: the token is the credential, so no key is resolved for it, and
    a key planted in the environment is never looked at."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ENVKEY-MUST-NOT-BE-USED")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "env-secret-must-not-be-used")
    client: Any = regional_sts("ap-southeast-2")  # Any: botocore client
    assert client.meta.endpoint_url == "https://sts.ap-southeast-2.amazonaws.com"
    assert client.meta.region_name == "ap-southeast-2"
    assert client.meta.config.signature_version is UNSIGNED
    with Stubber(client) as stubbed:
        stubbed.add_response(  # pyright: ignore[reportUnknownMemberType]
            "assume_role_with_web_identity",
            {
                "Credentials": {
                    "AccessKeyId": "ASIAFAKEFAKEFAKE",
                    "SecretAccessKey": "fake-secret",
                    "SessionToken": "fake-token",
                    "Expiration": START,
                }
            },
            {
                "RoleArn": ROLE,
                "RoleSessionName": ROLE_SESSION_NAME,
                "WebIdentityToken": "google.id.token.1",
                "DurationSeconds": 3600,
            },
        )
        provider = WebIdentityCredentials(
            role_arn=ROLE,
            tokens=FakeTokens(),
            sts=client,
            audience=AUDIENCE,
            now=lambda: START - dt.timedelta(hours=1),
        )
        credentials = asyncio.run(provider.credentials())
    assert credentials.access_key_id == "ASIAFAKEFAKEFAKE"
    assert credentials.expires_at == START


# ---- locally: a named profile ------------------------------------------------------------


@pytest.fixture
def profile_files(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "credentials").write_text(
        "[local]\naws_access_key_id = PROFILEKEY\naws_secret_access_key = profile-secret\n"
        "aws_session_token = profile-token\n"
    )
    (tmp_path / "config").write_text("[profile local]\nregion = ap-southeast-2\n")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))


@pytest.mark.usefixtures("profile_files")
async def test_a_named_profile_is_used_and_the_environments_keys_are_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ENVKEY-MUST-NOT-BE-USED")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "env-secret-must-not-be-used")
    credentials = await ProfileCredentials("local").credentials()
    assert credentials.access_key_id == "PROFILEKEY"
    assert credentials.secret_access_key == "profile-secret"
    assert credentials.session_token == "profile-token"


@pytest.mark.usefixtures("profile_files")
async def test_a_profile_that_does_not_exist_is_an_access_problem_with_the_fix_named() -> None:
    with pytest.raises(CredentialsUnavailable) as raised:
        await ProfileCredentials("no-such-profile").credentials()
    assert raised.value.refused is True and "aws sso login" in str(raised.value)


# ---- which provider Settings names -------------------------------------------------------


def _settings(**overrides: str) -> Settings:
    return Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg


@pytest.mark.usefixtures("profile_files")
def test_settings_pick_web_identity_on_cloud_run_and_a_profile_locally() -> None:
    assert isinstance(credentials_for(_settings(aws_role_arn=ROLE)), WebIdentityCredentials)
    assert isinstance(credentials_for(_settings(aws_profile="local")), ProfileCredentials)


def test_neither_source_or_both_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="AWS_ROLE_ARN"):
        credentials_for(_settings())
    with pytest.raises(ValueError, match="not both"):
        credentials_for(_settings(aws_role_arn=ROLE, aws_profile="local"))


# ---- no long-lived keys ------------------------------------------------------------------


def test_no_code_path_reads_the_long_lived_access_key_variables() -> None:
    """Pinned over every source file, scripts included: neither name appears
    anywhere in code. The credential chain that would read them is never used
    either: a profile is named explicitly, and STS is called unsigned."""
    names = re.compile(r"AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|aws_access_key_id", re.I)
    offenders = [
        str(path.relative_to(ROOT))
        for folder in ("src", "scripts")
        for path in (ROOT / folder).rglob("*.py")
        if names.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
