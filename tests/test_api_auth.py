"""Signing in to the API (issue #146): a Firebase ID token, verified, resolved
to a member of the one configured tenant.

The real `FirebaseTokenVerifier` is tested throughout. A locally generated RSA
key and self-signed certificate stand in for Google's published keys, so no
test makes a network call; `GoogleSigningKeys` is tested against a mock
transport. `forge` builds tokens with any header, so each attack a review
named is reproduced here, not just asserted.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.firebase_auth import (
    FIREBASE_CERTS_URL,
    FirebaseTokenVerifier,
    GoogleSigningKeys,
    SigningKeys,
)
from compliance_rag.adapters.interfaces import VerifierUnavailable
from compliance_rag.api.app import create_app
from compliance_rag.api.main import build_app
from compliance_rag.telemetry import span
from tests.api_support import (
    CERT,
    KID,
    MEMBER,
    OTHER_KEY,
    TENANT,
    Keys,
    api_settings,
    bearer,
    claims_for,
    fake_api_services,
    forge,
    mint,
    verifier_for,
)


def _app(
    verifier: FirebaseTokenVerifier | None = None,
    **settings: Any,  # Any: settings field values
) -> FastAPI:
    return create_app(
        fake_api_services(settings=api_settings(**settings), verifier=verifier or verifier_for())
    )


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with _client(_app()) as c:
        yield c


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
        pytest.param(forge({"alg": "none", "kid": KID}, claims_for(), sign="none"), id="alg-none"),
        pytest.param(
            forge({"alg": "HS256", "kid": KID}, claims_for(), sign="hs256-with-the-certificate"),
            id="hs256-keyed-with-the-public-certificate",
        ),
        pytest.param(forge({"alg": "ES256", "kid": KID}, claims_for()), id="es256-header"),
        pytest.param(forge({"alg": ["RS256"], "kid": KID}, claims_for()), id="alg-as-a-list"),
        pytest.param(forge({"alg": "RS256", "kid": [KID]}, claims_for()), id="kid-as-a-list"),
        pytest.param(forge({"alg": "RS256", "kid": {"a": 1}}, claims_for()), id="kid-as-an-object"),
        pytest.param(forge({"alg": "RS256"}, claims_for()), id="no-kid"),
        pytest.param(forge({"alg": "RS256", "kid": "unknown"}, claims_for()), id="unknown-kid"),
        pytest.param(forge(_NOT_AN_OBJECT, claims_for()), id="header-not-an-object"),
    ],
)
async def test_an_invalid_token_is_a_401_never_a_500(client: httpx.AsyncClient, token: str) -> None:
    response = await client.get("/v1/me", headers=bearer(token))
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
    response = await client.get("/v1/me", headers=bearer(mint(**claims)))
    assert response.status_code == 403


async def test_a_member_acts_on_the_configured_tenant_whatever_the_request_says(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/v1/me",
        params={"tenant_id": "other-tenant"},
        headers={**bearer(mint(email="SECOND@acme.example")), "X-Tenant-Id": "other-tenant"},
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
    assert (await client.get("/v1/me", headers=bearer(token))).status_code == 200


async def test_an_empty_member_list_admits_no_one() -> None:
    async with _client(_app(verifier_for(api_members=""))) as c:
        assert (await c.get("/v1/me", headers=bearer(mint()))).status_code == 403


# ---- telemetry ----------------------------------------------------------------------


async def test_every_request_is_a_span_and_the_token_reaches_no_log_or_span(
    client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
    span_exporter: InMemorySpanExporter,
) -> None:
    caplog.set_level(logging.DEBUG)
    for token in (mint(), mint(aud="another-project"), mint(email_verified=False), "a.b.c"):
        response = await client.get("/v1/me", headers=bearer(token))
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
    verifier = verifier_for(keys, now)
    await verifier.verify(mint())
    await verifier.verify(mint())
    assert keys.fetches == 1
    now[0] += 61
    await verifier.verify(mint())
    assert keys.fetches == 2


async def test_a_malformed_token_never_costs_a_fetch() -> None:
    keys = Keys()
    verifier = verifier_for(keys)
    for junk in ("x", "a.b.c", forge({"alg": "ES256", "kid": KID}, claims_for())):
        with pytest.raises(Exception, match="invalid token"):
            await verifier.verify(junk)
    assert keys.fetches == 0


async def test_an_outage_backs_off_instead_of_fetching_per_request() -> None:
    now = [1000.0]
    keys = Keys()
    keys.failing = True
    verifier = verifier_for(keys, now)
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
    verifier = verifier_for(keys, now)
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
    async with _client(_app(verifier_for(keys))) as c:
        assert (await c.get("/v1/me", headers=bearer(mint()))).status_code == 503


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
        build_app(api_settings(firebase_project_id=""))


def test_the_schema_is_served_only_when_asked_for() -> None:
    off = _app()
    assert off.openapi_url is None and off.docs_url is None
    assert _app(api_docs=True).openapi_url == "/openapi.json"
