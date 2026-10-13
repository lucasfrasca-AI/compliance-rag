"""Signing in to the API (issue #146): a Firebase ID token, verified, resolved
to a member of the one configured tenant.

The real `FirebaseTokenVerifier` is tested throughout. A locally generated RSA
key and self-signed certificate stand in for Google's published keys, so no
test makes a network call; `GoogleSigningKeys` is tested against a mock
transport. `forge` builds tokens with any header, so each attack a review
named is reproduced here, not just asserted.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.firebase_auth import (
    FIREBASE_CERTS_URL,
    FirebaseTokenVerifier,
    GoogleSigningKeys,
    SigningKeys,
    SigningKeySource,
)
from compliance_rag.adapters.interfaces import VerifierUnavailable
from compliance_rag.api.app import ApiServices, create_app
from compliance_rag.api.main import build_app
from compliance_rag.settings import Settings
from compliance_rag.telemetry import span

PROJECT = "compliance-rag-test"
TENANT = "acme"
MEMBER = "lead@acme.example"
KID = "test-key-1"


def _key_and_cert() -> tuple[rsa.RSAPrivateKey, str]:
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
    return key, cert.public_bytes(serialization.Encoding.PEM).decode()


SIGNING_KEY, CERT = _key_and_cert()
OTHER_KEY, _ = _key_and_cert()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _claims(**overrides: Any) -> dict[str, Any]:  # Any: JWT claim values
    now = int(time.time())
    claims: dict[str, Any] = {  # Any: JWT claim values
        "iss": f"https://securetoken.google.com/{PROJECT}",
        "aud": PROJECT,
        "sub": "uid-123",
        "iat": now,
        "exp": now + 3600,
        "auth_time": now - 60,
        "email": MEMBER,
        "email_verified": True,
        "firebase": {"sign_in_provider": "google.com", "identities": {}},
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def forge(
    header: object,
    claims: dict[str, Any],  # Any: JWT claim values
    *,
    key: rsa.RSAPrivateKey = SIGNING_KEY,
    sign: str = "rs256",
) -> str:
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    if sign == "rs256":
        signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    elif sign == "hs256-with-the-certificate":
        signature = hmac.new(CERT.encode(), signing_input.encode(), hashlib.sha256).digest()
    else:
        signature = b""
    return f"{signing_input}.{_b64(signature)}"


def mint(*, key: rsa.RSAPrivateKey = SIGNING_KEY, **overrides: Any) -> str:  # Any: claims
    return forge({"alg": "RS256", "kid": KID, "typ": "JWT"}, _claims(**overrides), key=key)


class Keys(SigningKeySource):
    """The stand-in for Google's certificates: counts fetches, can fail."""

    def __init__(self, max_age: float = 3600.0) -> None:
        self.fetches = 0
        self.max_age = max_age
        self.failing = False

    async def fetch(self) -> SigningKeys:
        self.fetches += 1
        if self.failing:
            raise VerifierUnavailable("down")
        return SigningKeys(certificates={KID: CERT}, max_age=self.max_age)

    async def aclose(self) -> None:
        pass


def _settings(**overrides: Any) -> Settings:  # Any: settings field values
    values: dict[str, Any] = {  # Any: settings field values
        "tenant_id": TENANT,
        "firebase_project_id": PROJECT,
        "api_members": f"{MEMBER}, Second@Acme.example",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg


def _verifier(
    keys: Keys | None = None,
    now: list[float] | None = None,
    **settings: Any,  # Any: settings field values
) -> FirebaseTokenVerifier:
    configured = _settings(**settings)
    clock = now if now is not None else [time.monotonic()]
    return FirebaseTokenVerifier(
        project_id=configured.firebase_project_id,
        members=configured.members,
        sign_in_providers=configured.sign_in_providers,
        keys=keys or Keys(),
        clock=lambda: clock[0],
    )


def _app(
    verifier: FirebaseTokenVerifier | None = None,
    **settings: Any,  # Any: settings field values
) -> FastAPI:
    return create_app(ApiServices(settings=_settings(**settings), verifier=verifier or _verifier()))


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with _client(_app()) as c:
        yield c


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- the routes ---------------------------------------------------------------------


async def test_health_needs_no_token_and_says_nothing_else(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


async def test_no_bearer_token_is_a_401_challenge(client: httpx.AsyncClient) -> None:
    for headers in ({}, {"Authorization": "Basic dXNlcjpwYXNz"}):
        response = await client.get("/v1/me", headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


NOW = int(time.time())
_NOT_AN_OBJECT: object = ["RS256"]


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("not-a-jwt", id="malformed"),
        pytest.param("a.b.c", id="three-junk-segments"),
        pytest.param(mint(exp=NOW - 3600, iat=NOW - 7200), id="expired"),
        pytest.param(mint(iat=NOW + 3600), id="issued-in-the-future"),
        pytest.param(mint(aud="another-project"), id="wrong-audience"),
        pytest.param(mint(iss="https://securetoken.google.com/another-project"), id="wrong-issuer"),
        pytest.param(mint(key=OTHER_KEY), id="bad-signature"),
        pytest.param(mint(sub=""), id="no-user"),
        pytest.param(mint(sub="u" * 129), id="user-id-too-long"),
        pytest.param(mint(auth_time=None), id="no-sign-in-time"),
        pytest.param(mint(auth_time=NOW + 3600), id="sign-in-time-in-the-future"),
        pytest.param(
            mint(firebase={"sign_in_provider": "google.com", "tenant": "t-1"}),
            id="identity-platform-tenant",
        ),
        pytest.param(forge({"alg": "none", "kid": KID}, _claims(), sign="none"), id="alg-none"),
        pytest.param(
            forge({"alg": "HS256", "kid": KID}, _claims(), sign="hs256-with-the-certificate"),
            id="hs256-keyed-with-the-public-certificate",
        ),
        pytest.param(forge({"alg": "ES256", "kid": KID}, _claims()), id="es256-header"),
        pytest.param(forge({"alg": ["RS256"], "kid": KID}, _claims()), id="alg-as-a-list"),
        pytest.param(forge({"alg": "RS256", "kid": [KID]}, _claims()), id="kid-as-a-list"),
        pytest.param(forge({"alg": "RS256", "kid": {"a": 1}}, _claims()), id="kid-as-an-object"),
        pytest.param(forge({"alg": "RS256"}, _claims()), id="no-kid"),
        pytest.param(forge({"alg": "RS256", "kid": "unknown"}, _claims()), id="unknown-kid"),
        pytest.param(forge(_NOT_AN_OBJECT, _claims()), id="header-not-an-object"),
    ],
)
async def test_an_invalid_token_is_a_401_never_a_500(client: httpx.AsyncClient, token: str) -> None:
    response = await client.get("/v1/me", headers=_bearer(token))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"email_verified": False}, id="unverified-email"),
        pytest.param({"email_verified": "true"}, id="verified-as-a-string"),
        pytest.param({"email": "someone@elsewhere.example"}, id="not-on-the-list"),
        pytest.param({"email": None}, id="no-email"),
        pytest.param(
            {"firebase": {"sign_in_provider": "oidc.any-idp"}}, id="untrusted-sign-in-provider"
        ),
        pytest.param({"firebase": None}, id="no-sign-in-provider"),
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


async def test_an_email_password_sign_in_is_trusted_by_default(
    client: httpx.AsyncClient,
) -> None:
    token = mint(firebase={"sign_in_provider": "password"})
    assert (await client.get("/v1/me", headers=_bearer(token))).status_code == 200


async def test_an_empty_member_list_admits_no_one() -> None:
    async with _client(_app(_verifier(api_members=""))) as c:
        assert (await c.get("/v1/me", headers=_bearer(mint()))).status_code == 403


# ---- telemetry ----------------------------------------------------------------------


async def test_every_request_is_a_span_and_the_token_reaches_no_log_or_span(
    client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
    span_exporter: InMemorySpanExporter,
) -> None:
    caplog.set_level(logging.DEBUG)
    for token in (mint(), mint(aud="another-project"), mint(email_verified=False), "a.b.c"):
        response = await client.get("/v1/me", headers=_bearer(token))
        trace_id = response.headers["x-trace-id"]
        assert len(trace_id) == 32 and trace_id != "0" * 32
        assert all(token not in r.getMessage() for r in caplog.records)
        for recorded in span_exporter.get_finished_spans():
            assert all(token not in str(v) for v in (recorded.attributes or {}).values())
    statuses = [
        (s.attributes or {}).get("http.status_code")
        for s in span_exporter.get_finished_spans()
        if s.name == "api.request"
    ]
    assert statuses == [200, 401, 403, 401]


async def test_a_streamed_response_is_inside_its_request_span(
    span_exporter: InMemorySpanExporter,
) -> None:
    """#149's SSE stream does its work while the body is sent, so the request
    span must still be open then."""
    app = _app()

    async def body() -> AsyncIterator[bytes]:
        # Async, as #149's stream will be: it runs in the request's own task.
        with span("streaming.work"):
            yield b"data: 1\n\n"

    async def probe() -> StreamingResponse:
        return StreamingResponse(body(), media_type="text/event-stream")

    app.add_api_route("/stream-probe", probe)
    async with _client(app) as c:
        assert (await c.get("/stream-probe")).text == "data: 1\n\n"
    spans = {s.name: s for s in span_exporter.get_finished_spans()}
    work, request = spans["streaming.work"], spans["api.request"]
    assert work.parent is not None and request.context is not None
    assert work.parent.span_id == request.context.span_id


# ---- the signing keys ---------------------------------------------------------------


async def test_keys_are_fetched_once_and_again_when_they_expire() -> None:
    now = [1000.0]
    keys = Keys(max_age=60)
    verifier = _verifier(keys, now)
    await verifier.verify(mint())
    await verifier.verify(mint())
    assert keys.fetches == 1
    now[0] += 61
    await verifier.verify(mint())
    assert keys.fetches == 2


async def test_a_malformed_token_never_costs_a_fetch() -> None:
    keys = Keys()
    verifier = _verifier(keys)
    for junk in ("x", "a.b.c", forge({"alg": "ES256", "kid": KID}, _claims())):
        with pytest.raises(Exception, match="invalid token"):
            await verifier.verify(junk)
    assert keys.fetches == 0


async def test_an_outage_backs_off_instead_of_fetching_per_request() -> None:
    now = [1000.0]
    keys = Keys()
    keys.failing = True
    verifier = _verifier(keys, now)
    for _ in range(20):
        with pytest.raises(VerifierUnavailable):
            await verifier.verify(mint())
    assert keys.fetches == 1
    now[0] += 31
    keys.failing = False
    assert (await verifier.verify(mint())).email == MEMBER
    assert keys.fetches == 2


async def test_the_last_good_keys_stand_in_during_an_outage_for_an_hour() -> None:
    now = [1000.0]
    keys = Keys(max_age=60)
    verifier = _verifier(keys, now)
    await verifier.verify(mint())
    keys.failing = True
    now[0] += 61 + 1800  # stale, inside the grace
    assert (await verifier.verify(mint())).email == MEMBER
    now[0] += 3600  # beyond it
    with pytest.raises(VerifierUnavailable):
        await verifier.verify(mint())


async def test_unfetchable_keys_are_a_503_not_a_401() -> None:
    keys = Keys()
    keys.failing = True
    async with _client(_app(_verifier(keys))) as c:
        assert (await c.get("/v1/me", headers=_bearer(mint()))).status_code == 503


async def test_google_signing_keys_reads_the_certificates_and_their_max_age() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == FIREBASE_CERTS_URL
        return httpx.Response(
            200,
            json={KID: CERT},
            headers={"Cache-Control": "public, max-age=19271, must-revalidate"},
        )

    source = GoogleSigningKeys(httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    assert await source.fetch() == SigningKeys(certificates={KID: CERT}, max_age=19271.0)
    await source.aclose()


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(500), id="server-error"),
        pytest.param(httpx.Response(200, json={}), id="empty"),
        pytest.param(httpx.Response(200, text="not json"), id="not-json"),
    ],
)
async def test_google_signing_keys_failing_is_verifier_unavailable(
    response: httpx.Response,
) -> None:
    source = GoogleSigningKeys(httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)))
    with pytest.raises(VerifierUnavailable):
        await source.fetch()
    await source.aclose()


# ---- startup ------------------------------------------------------------------------


def test_the_api_refuses_to_start_without_a_firebase_project() -> None:
    with pytest.raises(ValueError, match="FIREBASE_PROJECT_ID"):
        build_app(_settings(firebase_project_id=""))


def test_the_schema_is_served_only_when_asked_for() -> None:
    off = _app()
    assert off.openapi_url is None and off.docs_url is None
    assert _app(api_docs=True).openapi_url == "/openapi.json"
