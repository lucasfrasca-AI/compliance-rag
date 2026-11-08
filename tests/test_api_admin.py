"""Roles: admins are configuration (issue #294, ADR-0053 U6).

`API_ADMINS` names the members who may also write subscriptions and record
profile evidence. Everything runs through the real app, verifier and session
codec over fake stores; the role is read from configuration on every request,
so it is in neither the token nor the cookie.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from compliance_rag.adapters.fakes import FakeLedgerStore
from compliance_rag.adapters.interfaces import Member, Profile
from compliance_rag.api.app import create_app
from compliance_rag.api.deps import ApiServices
from compliance_rag.api.session import COOKIE_NAME, SessionCodec
from compliance_rag.settings import Settings
from tests.api_support import (
    MEMBER,
    SECOND,
    TENANT,
    api_settings,
    bearer,
    fake_api_services,
    mint,
)

SECRET = "s3cr3t-" + "k" * 40
ORIGIN = "https://api"
OUTSIDER = "admin@elsewhere.example"
"""Listed as an admin below, never as a member."""
LAW = "privacy-act-1988"

FACT: Mapping[str, object] = {
    "field": "legal_form",
    "value": "company",
    "period": {"start": "2020-01-01"},
    "source": "the compliance lead",
}

WRITES = [
    pytest.param("PUT", f"/v1/subscriptions/{LAW}", None, id="subscribe"),
    pytest.param("DELETE", f"/v1/subscriptions/{LAW}", None, id="unsubscribe"),
    pytest.param("POST", "/v1/profile/evidence", FACT, id="record-evidence"),
]
READS = [
    pytest.param("/v1/profile", id="profile"),
    pytest.param("/v1/subscriptions", id="subscriptions"),
    pytest.param("/v1/coverage", id="coverage"),
    pytest.param("/v1/me", id="me"),
]


def _settings(**overrides: Any) -> Settings:  # Any: settings field values
    return api_settings(session_secret=SECRET, **overrides)


async def _world(
    settings: Settings | None = None,
) -> tuple[FastAPI, httpx.AsyncClient, FakeLedgerStore]:
    ledger = FakeLedgerStore()
    await ledger.put_profile(Profile(TENANT))
    app = create_app(fake_api_services(settings=settings or _settings(), ledger=ledger))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)
    return app, client, ledger


def _cookie(email: str) -> dict[str, str]:
    value = SessionCodec(SECRET).seal(
        Member(uid="uid-1", email=email), expires_at=int(time.time()) + 600
    )
    return {"Cookie": f"{COOKIE_NAME}={value}", "Origin": ORIGIN}


def with_settings(app: FastAPI, **updates: str) -> None:
    """Run the app's next requests on other configuration, as a redeploy with a
    different `API_MEMBERS` or `API_ADMINS` would leave them."""
    services: ApiServices = app.state.services
    app.state.services = replace(services, settings=services.settings.model_copy(update=updates))


# ---- who is an admin -------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
async def test_a_member_who_is_not_an_admin_gets_403_on_the_admin_routes(
    method: str, path: str, body: Mapping[str, object] | None
) -> None:
    _, client, ledger = await _world()
    await ledger.subscribe(tenant_id=TENANT, instrument_id=LAW, subscribed_by="setup")
    async with client:
        response = await client.request(method, path, json=body, headers=bearer(mint(email=SECOND)))
    assert response.status_code == 403
    assert "admin" in response.json()["detail"]
    # Nothing ran: the seeded subscription survives, and no evidence was recorded.
    assert await ledger.subscribed_instruments(TENANT) == frozenset({LAW})
    assert await ledger.evidence(TENANT) == ()


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
async def test_an_admin_succeeds_on_the_admin_routes(
    method: str, path: str, body: Mapping[str, object] | None
) -> None:
    _, client, _ = await _world()
    async with client:
        response = await client.request(method, path, json=body, headers=bearer(mint()))
    assert response.status_code in (201, 204), response.text


async def test_an_admin_is_matched_case_insensitively_like_a_member() -> None:
    _, client, _ = await _world(_settings(api_admins=" LEAD@Acme.Example , x@y.example"))
    async with client:
        response = await client.put(f"/v1/subscriptions/{LAW}", headers=bearer(mint()))
    assert response.status_code == 204


@pytest.mark.parametrize("read", READS)
async def test_a_member_who_is_not_an_admin_still_reads(read: str) -> None:
    _, client, _ = await _world()
    async with client:
        response = await client.get(read, headers=bearer(mint(email=SECOND)))
    assert response.status_code == 200


async def test_an_address_in_the_admins_but_not_the_members_is_refused() -> None:
    settings = _settings(api_admins=f"{MEMBER}, {OUTSIDER}")
    _, client, ledger = await _world(settings)
    async with client:
        # A token: the verifier admits members only.
        token = await client.put(f"/v1/subscriptions/{LAW}", headers=bearer(mint(email=OUTSIDER)))
        # A cookie sealed for them (as if issued while they were still a member).
        cookie = await client.put(f"/v1/subscriptions/{LAW}", headers=_cookie(OUTSIDER))
    assert (token.status_code, cookie.status_code) == (403, 403)
    assert await ledger.subscribed_instruments(TENANT) == frozenset()


async def test_an_admin_dropped_from_the_members_is_refused_on_a_valid_cookie() -> None:
    app, client, _ = await _world()
    async with client:
        before = await client.put(f"/v1/subscriptions/{LAW}", headers=_cookie(MEMBER))
        with_settings(app, api_members=SECOND)  # MEMBER is still listed in API_ADMINS
        after = await client.put(f"/v1/subscriptions/{LAW}", headers=_cookie(MEMBER))
    assert (before.status_code, after.status_code) == (204, 403)


async def test_an_admin_dropped_from_the_members_is_refused_on_a_valid_bearer_token() -> None:
    app, client, ledger = await _world()
    token = bearer(mint())  # signed while MEMBER was a member and an admin
    async with client:
        before = await client.put(f"/v1/subscriptions/{LAW}", headers=token)
        with_settings(app, api_members=SECOND)  # MEMBER is still listed in API_ADMINS
        after = await client.delete(f"/v1/subscriptions/{LAW}", headers=token)
    assert (before.status_code, after.status_code) == (204, 403)
    assert await ledger.subscribed_instruments(TENANT) == frozenset({LAW})


async def test_no_admins_configured_means_nobody_may_write() -> None:
    _, client, ledger = await _world(_settings(api_admins=""))
    async with client:
        sub = await client.put(f"/v1/subscriptions/{LAW}", headers=bearer(mint()))
        evidence = await client.post("/v1/profile/evidence", json=FACT, headers=bearer(mint()))
    assert (sub.status_code, evidence.status_code) == (403, 403)
    assert await ledger.evidence(TENANT) == ()


# ---- the role is read on every request -------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
async def test_removing_an_admin_takes_effect_on_the_next_bearer_request(
    method: str, path: str, body: Mapping[str, object] | None
) -> None:
    app, client, _ = await _world()
    token = bearer(mint())  # one token, issued while MEMBER was an admin
    async with client:
        before = await client.request(method, path, json=body, headers=token)
        with_settings(app, api_admins="someone.else@acme.example")
        after = await client.request(method, path, json=body, headers=token)
    assert before.status_code in (201, 204)
    assert after.status_code == 403


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
async def test_removing_an_admin_takes_effect_on_the_next_cookie_request(
    method: str, path: str, body: Mapping[str, object] | None
) -> None:
    app, client, _ = await _world()
    cookie = _cookie(MEMBER)  # one cookie, sealed while MEMBER was an admin
    async with client:
        before = await client.request(method, path, json=body, headers=cookie)
        with_settings(app, api_admins="someone.else@acme.example")
        after = await client.request(method, path, json=body, headers=cookie)
        still_reads = await client.get("/v1/profile", headers=cookie)
    assert before.status_code in (201, 204)
    assert after.status_code == 403
    assert still_reads.status_code == 200  # still a member


async def test_adding_an_admin_takes_effect_on_the_next_request_too() -> None:
    app, client, _ = await _world()
    token = bearer(mint(email=SECOND))
    async with client:
        before = await client.put(f"/v1/subscriptions/{LAW}", headers=token)
        with_settings(app, api_admins=f"{MEMBER}, {SECOND}")
        after = await client.put(f"/v1/subscriptions/{LAW}", headers=token)
    assert (before.status_code, after.status_code) == (403, 204)


# ---- sign-in is still required ---------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
async def test_an_admin_route_without_sign_in_is_a_401_not_a_403(
    method: str, path: str, body: Mapping[str, object] | None
) -> None:
    _, client, _ = await _world()
    async with client:
        response = await client.request(method, path, json=body)
    assert response.status_code == 401
