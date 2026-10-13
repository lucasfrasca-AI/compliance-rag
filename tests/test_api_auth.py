"""Signing in to the API (issue #146): a Firebase ID token, verified, resolved
to a member of the one configured tenant.

The real `FirebaseTokenVerifier` is tested. A locally generated RSA key and
self-signed certificate stand in for Google's published keys, so no test
makes a network call; `FirebaseCerts` is tested against a mock transport.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from google.auth import crypt, jwt
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.api.app import ApiServices, create_app
from compliance_rag.api.auth import (
    FIREBASE_CERTS_URL,
    Certs,
    FirebaseCerts,
    FirebaseTokenVerifier,
    VerifierUnavailable,
)
from compliance_rag.api.main import build_app
from compliance_rag.settings import Settings

PROJECT = "compliance-rag-test"
TENANT = "acme"
MEMBER = "lead@acme.example"
KID = "test-key-1"


def _key_and_cert() -> tuple[bytes, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test signer")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem_key, cert.public_bytes(serialization.Encoding.PEM).decode()


SIGNING_KEY, CERT = _key_and_cert()
OTHER_KEY, _ = _key_and_cert()


def mint(*, key: bytes = SIGNING_KEY, **overrides: Any) -> str:  # Any: JWT claim values
    now = int(time.time())
    claims: dict[str, Any] = {  # Any: JWT claim values
        "iss": f"https://securetoken.google.com/{PROJECT}",
        "aud": PROJECT,
        "sub": "uid-123",
        "iat": now,
        "exp": now + 3600,
        "email": MEMBER,
        "email_verified": True,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    signer = crypt.RSASigner.from_string(key, KID)  # pyright: ignore[reportUnknownMemberType] — google-auth is untyped
    return jwt.encode(signer, claims).decode()  # pyright: ignore[reportUnknownMemberType] — google-auth is untyped


class CountingCerts:
    def __init__(self, max_age: float = 3600.0) -> None:
        self.calls = 0
        self.max_age = max_age

    async def __call__(self) -> Certs:
        self.calls += 1
        return {KID: CERT}, self.max_age


def _settings(**overrides: Any) -> Settings:  # Any: settings field values
    values: dict[str, Any] = {  # Any: settings field values
        "tenant_id": TENANT,
        "firebase_project_id": PROJECT,
        "api_members": f"{MEMBER}, Second@Acme.example",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg


def _verifier(
    certs: CountingCerts | None = None,
    **kwargs: Any,  # Any: FirebaseTokenVerifier keyword overrides
) -> FirebaseTokenVerifier:
    settings = _settings()
    return FirebaseTokenVerifier(
        project_id=settings.firebase_project_id,
        members=settings.members,
        fetch_certs=certs or CountingCerts(),
        **kwargs,
    )


def _app(verifier: FirebaseTokenVerifier | None = None) -> FastAPI:
    return create_app(ApiServices(settings=_settings(), verifier=verifier or _verifier()))


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://api"
    ) as c:
        yield c


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_health_needs_no_token_and_says_nothing_else(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


async def test_no_token_is_a_401_challenge(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("not-a-jwt", id="malformed"),
        pytest.param(mint(exp=int(time.time()) - 3600, iat=int(time.time()) - 7200), id="expired"),
        pytest.param(mint(aud="another-project"), id="wrong-audience"),
        pytest.param(mint(iss="https://securetoken.google.com/another-project"), id="wrong-issuer"),
        pytest.param(mint(key=OTHER_KEY), id="bad-signature"),
        pytest.param(mint(sub=""), id="no-user"),
    ],
)
async def test_an_invalid_token_is_a_401(client: httpx.AsyncClient, token: str) -> None:
    response = await client.get("/v1/me", headers=_bearer(token))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"email_verified": False}, id="unverified-email"),
        pytest.param({"email": "someone@elsewhere.example"}, id="not-on-the-list"),
        pytest.param({"email": None}, id="no-email"),
    ],
)
async def test_a_valid_token_for_a_non_member_is_a_403(
    client: httpx.AsyncClient,
    claims: dict[str, Any],  # Any: JWT claim values
) -> None:
    response = await client.get("/v1/me", headers=_bearer(mint(**claims)))
    assert response.status_code == 403


async def test_a_member_acts_on_the_configured_tenant_whatever_the_request_says(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/v1/me",
        params={"tenant_id": "other-tenant"},
        headers={**_bearer(mint(email="SECOND@acme.example")), "X-Tenant-Id": "other-tenant"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": TENANT,
        "uid": "uid-123",
        "email": "second@acme.example",
    }
    assert len(response.headers["x-trace-id"]) == 32


async def test_an_empty_member_list_admits_no_one() -> None:
    verifier = FirebaseTokenVerifier(
        project_id=PROJECT, members=_settings(api_members="").members, fetch_certs=CountingCerts()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(verifier)), base_url="http://api"
    ) as c:
        assert (await c.get("/v1/me", headers=_bearer(mint()))).status_code == 403


async def test_the_token_never_reaches_a_log_or_a_span(
    client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
    span_exporter: InMemorySpanExporter,
) -> None:
    caplog.set_level(logging.DEBUG)
    for token in (mint(), mint(aud="another-project"), mint(email_verified=False)):
        await client.get("/v1/me", headers=_bearer(token))
        assert all(token not in r.getMessage() for r in caplog.records)
        for recorded in span_exporter.get_finished_spans():
            assert all(token not in str(v) for v in (recorded.attributes or {}).values())
    assert any(s.name == "api.request" for s in span_exporter.get_finished_spans())


async def test_keys_are_fetched_once_and_again_when_they_expire() -> None:
    now = [1000.0]
    certs = CountingCerts(max_age=60)
    verifier = _verifier(certs, clock=lambda: now[0])
    await verifier.verify(mint())
    await verifier.verify(mint())
    assert certs.calls == 1
    now[0] += 61
    await verifier.verify(mint())
    assert certs.calls == 2


async def test_unfetchable_keys_are_a_503_not_a_401() -> None:
    async def failing() -> Certs:
        raise VerifierUnavailable("down")

    verifier = FirebaseTokenVerifier(project_id=PROJECT, members=frozenset(), fetch_certs=failing)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(verifier)), base_url="http://api"
    ) as c:
        assert (await c.get("/v1/me", headers=_bearer(mint()))).status_code == 503


async def test_firebase_certs_reads_the_keys_and_their_max_age() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == FIREBASE_CERTS_URL
        return httpx.Response(
            200,
            json={KID: CERT},
            headers={"Cache-Control": "public, max-age=19271, must-revalidate"},
        )

    fetch = FirebaseCerts(httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    assert await fetch() == ({KID: CERT}, 19271.0)
    await fetch.aclose()


async def test_firebase_certs_failing_is_verifier_unavailable() -> None:
    fetch = FirebaseCerts(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    )
    with pytest.raises(VerifierUnavailable):
        await fetch()
    await fetch.aclose()


def test_the_api_refuses_to_start_without_a_firebase_project() -> None:
    with pytest.raises(ValueError, match="FIREBASE_PROJECT_ID"):
        build_app(_settings(firebase_project_id=""))
    with pytest.raises(ValueError, match="FIREBASE_PROJECT_ID"):
        create_app(
            ApiServices(
                settings=_settings(env="cloud", firebase_project_id=""), verifier=_verifier()
            )
        )


def test_the_schema_is_published_only_locally() -> None:
    assert _app().openapi_url == "/openapi.json"
    cloud = create_app(ApiServices(settings=_settings(env="cloud"), verifier=_verifier()))
    assert cloud.openapi_url is None and cloud.docs_url is None
