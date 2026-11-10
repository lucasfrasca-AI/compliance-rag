"""Sign-in fails closed (issue #319).

The API is public and its members list is the control, not the identity
provider's console. Each refusal here goes through the real app, the real
`FirebaseTokenVerifier` and the real session codec, over a fake account
directory (or the real Identity Toolkit adapter over a mock transport for the
failures), with a controllable clock for the cookie re-check. Nothing calls
Google.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters import firebase_auth
from compliance_rag.adapters.fakes import FakeAccountDirectory
from compliance_rag.adapters.firebase_auth import (
    SESSION_RECHECK_SECONDS,
    FirebaseTokenVerifier,
    IdentityToolkitAccounts,
)
from compliance_rag.adapters.interfaces import AccountDirectory
from compliance_rag.api.app import create_app
from compliance_rag.api.main import build_app
from compliance_rag.api.session import COOKIE_NAME, SessionCodec
from tests.api_support import (
    MEMBER,
    Keys,
    api_settings,
    bearer,
    fake_api_services,
    mint,
    session_member,
    verifier_for,
)

SECRET = "s3cr3t-" + "k" * 40
ORIGIN = "https://api"
UID = "uid-123"
STRANGER = "stranger@elsewhere.example"


class World:
    """One app over a controllable clock and an account directory."""

    def __init__(
        self,
        accounts: AccountDirectory,
        *,
        verifier: FirebaseTokenVerifier | None = None,
        **kw: Any,
    ) -> None:  # Any: settings field values
        self.now = [1000.0]
        self.accounts = accounts
        self.verifier = verifier or verifier_for(Keys(), self.now, accounts=accounts, **kw)
        self.app: FastAPI = create_app(
            fake_api_services(
                settings=api_settings(session_secret=SECRET, **kw), verifier=self.verifier
            )
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN)


def enabled() -> FakeAccountDirectory:
    directory = FakeAccountDirectory()
    directory.register(UID)
    return directory


def signing_in() -> dict[str, str]:
    """`POST /session` from a browser that still holds the old cookie, so it
    carries the Origin the guard asks of it."""
    return {**bearer(mint()), "Origin": ORIGIN}


def with_cookie(value: str) -> dict[str, str]:
    return {"Cookie": f"{COOKIE_NAME}={value}", "Origin": ORIGIN}


def sealed(**member: Any) -> str:  # Any: session_member arguments
    return SessionCodec(SECRET).seal(session_member(**member), expires_at=int(time.time()) + 600)


# ---- an unknown email is refused: the members list is the control -------------------


async def test_a_fresh_account_the_provider_let_sign_up_create_is_refused_on_every_path() -> None:
    """Sign-up on, a stranger registers: a verified Google or password account
    that exists and is enabled. The members list alone refuses it, 403 as ever."""
    accounts = enabled()
    accounts.register("uid-stranger")
    world = World(accounts)
    token = mint(sub="uid-stranger", email=STRANGER)
    async with world.client() as c:
        assert (await c.get("/v1/me", headers=bearer(token))).status_code == 403
        signed_in = await c.post("/session", headers=bearer(token))
        assert signed_in.status_code == 403 and "set-cookie" not in signed_in.headers
        for provider in ("google.com", "password"):
            token = mint(
                sub="uid-stranger", email=STRANGER, firebase={"sign_in_provider": provider}
            )
            assert (await c.get("/v1/me", headers=bearer(token))).status_code == 403
        stranger_cookie = sealed(email=STRANGER, uid="uid-stranger")
        assert (await c.get("/v1/me", headers=with_cookie(stranger_cookie))).status_code == 403
        page = await c.get("/chats", headers=with_cookie(stranger_cookie))
        assert page.status_code == 403
    assert accounts.lookups == 0, "a non-member is refused before the provider is asked"


async def test_an_unknown_email_is_refused_even_when_its_account_is_unverified_or_odd() -> None:
    world = World(enabled())
    async with world.client() as c:
        cases: list[dict[str, Any]] = [  # Any: JWT claim values
            {"email": STRANGER, "email_verified": False},
            {"email": STRANGER, "firebase": {"sign_in_provider": "oidc.any-idp"}},
            {"email": STRANGER, "firebase": {"sign_in_provider": "anonymous"}},
            {"email": None},
        ]
        for claims in cases:
            assert (await c.get("/v1/me", headers=bearer(mint(**claims)))).status_code == 403


# ---- a disabled account is refused --------------------------------------------------


async def test_a_disabled_account_is_refused_at_sign_in_and_on_a_bearer_request() -> None:
    accounts = enabled()
    accounts.register(UID, disabled=True)
    world = World(accounts)
    async with world.client() as c:
        me = await c.get("/v1/me", headers=bearer(mint()))
        signed_in = await c.post("/session", headers=bearer(mint()))
    assert me.status_code == 403
    assert signed_in.status_code == 403
    assert "set-cookie" not in signed_in.headers


async def test_a_disabled_account_loses_its_cookie_session_within_the_recheck_interval() -> None:
    accounts = enabled()
    world = World(accounts)
    async with world.client() as c:
        assert (await c.post("/session", headers=bearer(mint()))).status_code == 200
        assert (await c.get("/v1/me")).status_code == 200

        accounts.register(UID, disabled=True)  # disabled in the console, now
        world.now[0] += SESSION_RECHECK_SECONDS - 1
        assert (await c.get("/v1/me")).status_code == 200  # still inside the interval

        world.now[0] += 2  # past it
        assert (await c.get("/v1/me")).status_code == 403
        page = await c.get("/chats")
        assert page.status_code == 403 and "text/html" in page.headers["content-type"]


def test_the_recheck_interval_is_five_minutes_at_most() -> None:
    assert SESSION_RECHECK_SECONDS <= 300


async def test_a_cookie_is_checked_at_most_once_an_interval_and_a_bearer_request_every_time() -> (
    None
):
    accounts = enabled()
    world = World(accounts)
    async with world.client() as c:
        await c.post("/session", headers=bearer(mint()))
        after_sign_in = accounts.lookups
        for _ in range(5):
            await c.get("/v1/me")
        assert accounts.lookups == after_sign_in + 1  # the first read; the rest inside the interval
        world.now[0] += SESSION_RECHECK_SECONDS + 1
        await c.get("/v1/me")
        await c.get("/v1/me")
        assert accounts.lookups == after_sign_in + 2
        before = accounts.lookups
        for _ in range(3):
            await c.get("/v1/me", headers=bearer(mint()))
        assert accounts.lookups == before + 3


async def test_each_session_has_its_own_recheck() -> None:
    accounts = enabled()
    world = World(accounts)
    async with world.client() as c:
        one, two = (
            sealed(),
            SessionCodec(SECRET).seal(session_member(), expires_at=int(time.time()) + 599),
        )
        await c.get("/v1/me", headers=with_cookie(one))
        first = accounts.lookups
        await c.get("/v1/me", headers=with_cookie(one))
        assert accounts.lookups == first
        await c.get("/v1/me", headers=with_cookie(two))
        assert accounts.lookups == first + 1


async def test_the_remembered_sessions_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(firebase_auth, "MAX_REMEMBERED_SESSIONS", 2)
    accounts = enabled()
    world = World(accounts)
    async with world.client() as c:
        cookies = [
            SessionCodec(SECRET).seal(session_member(), expires_at=int(time.time()) + 600 + i)
            for i in range(3)
        ]
        for value in cookies:
            await c.get("/v1/me", headers=with_cookie(value))
        assert len(world.verifier._rechecked) == 2  # pyright: ignore[reportPrivateUsage]
        before = accounts.lookups
        await c.get("/v1/me", headers=with_cookie(cookies[0]))  # forgotten: asked again
        assert accounts.lookups == before + 1


async def test_a_refusal_is_not_remembered() -> None:
    accounts = enabled()
    accounts.register(UID, disabled=True)
    world = World(accounts)
    async with world.client() as c:
        assert (await c.get("/v1/me", headers=with_cookie(sealed()))).status_code == 403
        accounts.register(UID)  # re-enabled: the very next request is checked afresh
        assert (await c.get("/v1/me", headers=with_cookie(sealed()))).status_code == 200
        accounts.register(UID, disabled=True)
        world.now[0] += SESSION_RECHECK_SECONDS + 1
        assert (await c.get("/v1/me", headers=with_cookie(sealed()))).status_code == 403


# ---- deleted, revoked ----------------------------------------------------------------


async def test_a_deleted_account_is_a_401_on_both_paths() -> None:
    accounts = enabled()
    world = World(accounts)
    async with world.client() as c:
        await c.post("/session", headers=bearer(mint()))
        del accounts.accounts[UID]
        assert (await c.get("/v1/me", headers=bearer(mint()))).status_code == 401
        assert (await c.post("/session", headers=signing_in())).status_code == 401
        world.now[0] += SESSION_RECHECK_SECONDS + 1
        assert (await c.get("/v1/me")).status_code == 401


async def test_tokens_revoked_after_the_sign_in_are_refused_on_both_paths() -> None:
    accounts = enabled()
    world = World(accounts)
    signed_in_at = int(time.time()) - 60
    async with world.client() as c:
        await c.post("/session", headers=bearer(mint(auth_time=signed_in_at)))
        accounts.register(UID, valid_since=signed_in_at + 1)  # sign-ins revoked after it
        revoked = await c.get("/v1/me", headers=bearer(mint(auth_time=signed_in_at)))
        assert revoked.status_code == 401
        world.now[0] += SESSION_RECHECK_SECONDS + 1
        assert (await c.get("/v1/me")).status_code == 401
        # A sign-in after the revocation is fine.
        fresh = mint(auth_time=signed_in_at + 30)
        assert (await c.get("/v1/me", headers=bearer(fresh))).status_code == 200


async def test_a_cookie_made_before_this_change_has_no_sign_in_time_and_is_a_401() -> None:
    import base64
    import json

    codec = SessionCodec(SECRET)
    body = {"v": 1, "uid": UID, "email": MEMBER, "exp": int(time.time()) + 600}
    payload = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
    value = f"{payload}.{codec._signature(payload)}"  # pyright: ignore[reportPrivateUsage]
    async with World(enabled()).client() as c:
        assert (await c.get("/v1/me", headers=with_cookie(value))).status_code == 401


# ---- a failed lookup refuses: 503, never an admission --------------------------------


def _timeout(_: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow")


def _identity_toolkit(handler: httpx.MockTransport) -> IdentityToolkitAccounts:
    async def token() -> str:
        return "an-access-token"

    return IdentityToolkitAccounts(
        project_id="compliance-rag-test",
        access_token=token,
        client=httpx.AsyncClient(transport=handler),
    )


BROKEN = [
    pytest.param(httpx.MockTransport(_timeout), id="timeout"),
    pytest.param(httpx.MockTransport(lambda _: httpx.Response(500)), id="5xx"),
    pytest.param(
        httpx.MockTransport(lambda _: httpx.Response(403, json={"error": {"code": 403}})),
        id="403-permission-error",
    ),
    pytest.param(httpx.MockTransport(lambda _: httpx.Response(200, text="{not json")), id="json"),
    pytest.param(
        httpx.MockTransport(lambda _: httpx.Response(200, json={"users": [{"localId": "x"}]})),
        id="unexpected-shape",
    ),
]


@pytest.mark.parametrize("transport", BROKEN)
async def test_a_failed_lookup_refuses_at_sign_in_on_a_bearer_request_and_on_a_cookie(
    transport: httpx.MockTransport,
) -> None:
    world = World(_identity_toolkit(transport))
    async with world.client() as c:
        me = await c.get("/v1/me", headers=bearer(mint()))
        signed_in = await c.post("/session", headers=bearer(mint()))
        by_cookie = await c.get("/v1/me", headers=with_cookie(sealed()))
        page = await c.get("/chats", headers=with_cookie(sealed()))
    assert (me.status_code, signed_in.status_code, by_cookie.status_code) == (503, 503, 503)
    assert "set-cookie" not in signed_in.headers
    assert page.status_code == 503  # not a 200 page, not a redirect to a sign-in that cannot work


async def test_an_adapter_that_raises_something_unforeseen_still_refuses() -> None:
    accounts = enabled()
    accounts.failure = RuntimeError("boom")
    world = World(accounts)
    async with world.client() as c:
        assert (await c.get("/v1/me", headers=bearer(mint()))).status_code == 503
        assert (await c.get("/v1/me", headers=with_cookie(sealed()))).status_code == 503


async def test_an_outage_is_noticed_when_the_interval_ends_and_not_before() -> None:
    accounts = enabled()
    world = World(accounts)
    async with world.client() as c:
        accounts.failure = RuntimeError("down")
        assert (await c.get("/v1/me", headers=with_cookie(sealed()))).status_code == 503
        accounts.failure = None
        assert (await c.get("/v1/me", headers=with_cookie(sealed()))).status_code == 200
        accounts.failure = RuntimeError("down again")
        assert (await c.get("/v1/me", headers=with_cookie(sealed()))).status_code == 200  # inside
        world.now[0] += SESSION_RECHECK_SECONDS + 1
        assert (await c.get("/v1/me", headers=with_cookie(sealed()))).status_code == 503


# ---- a missing token is refused -----------------------------------------------------

PUBLIC = {"/health", "/signin", "/session/logout"}
EMPTY_OR_MALFORMED = [
    pytest.param({}, id="nothing"),
    pytest.param({"Authorization": "Bearer "}, id="empty-bearer"),
    pytest.param({"Authorization": "Bearer"}, id="bearer-without-value"),
    pytest.param({"Authorization": "Basic dXNlcjpwYXNz"}, id="basic"),
    pytest.param(with_cookie("garbage"), id="malformed-cookie"),
    pytest.param(with_cookie(""), id="empty-cookie"),
    pytest.param(with_cookie("a.b"), id="two-segment-cookie"),
]


def _guarded_routes(app: FastAPI) -> Iterator[tuple[str, str]]:
    for path, operations in app.openapi()["paths"].items():
        if path in PUBLIC:
            continue
        for method in operations:
            yield method.upper(), path.replace("{", "").replace("}", "")


def test_the_route_enumeration_finds_the_api_and_the_pages() -> None:
    found = {path for _, path in _guarded_routes(World(enabled()).app)}
    assert len(found) >= 20
    assert {"/v1/me", "/v1/answers", "/session", "/chats", "/admin/subscriptions"} <= found


@pytest.mark.parametrize("headers", EMPTY_OR_MALFORMED)
async def test_no_usable_token_is_a_401_on_every_api_route_and_a_redirect_on_every_page(
    headers: dict[str, str],
) -> None:
    accounts = enabled()
    world = World(accounts)
    asked = 0
    async with world.client() as c:
        for method, path in _guarded_routes(world.app):
            response = await c.request(method, path, headers=headers)
            asked += 1
            # Pages send you to sign in; the htmx fragment is a 401 like the API.
            if path.startswith(("/chats", "/admin")) and not path.endswith("/turns"):
                assert (response.status_code, response.headers.get("location")) == (
                    303,
                    "/signin",
                ), (method, path)
            else:
                assert response.status_code == 401, (method, path)
    assert asked >= 20
    assert accounts.lookups == 0  # nothing was looked up for a request with no token


# ---- a misconfiguration fails closed ------------------------------------------------


@pytest.mark.parametrize("members", ["", "   ", " , ,"])
async def test_an_empty_members_list_admits_no_one_on_any_path(members: str) -> None:
    accounts = enabled()
    world = World(accounts, api_members=members, api_admins=MEMBER)
    async with world.client() as c:
        assert (await c.get("/v1/me", headers=bearer(mint()))).status_code == 403
        signed_in = await c.post("/session", headers=bearer(mint()))
        assert signed_in.status_code == 403 and "set-cookie" not in signed_in.headers
        assert (await c.get("/v1/me", headers=with_cookie(sealed()))).status_code == 403
        assert (await c.get("/chats", headers=with_cookie(sealed()))).status_code == 403
        assert (
            await c.get("/admin/subscriptions", headers=with_cookie(sealed()))
        ).status_code == 403
    assert accounts.lookups == 0


@pytest.mark.parametrize("env", ["cloud", "local", "ci"])
def test_the_api_refuses_to_start_without_a_firebase_project_in_any_environment(env: str) -> None:
    with pytest.raises(ValueError, match="FIREBASE_PROJECT_ID"):
        build_app(api_settings(env=env, firebase_project_id=""))


def test_the_account_lookup_cannot_be_built_without_a_project() -> None:
    with pytest.raises(ValueError, match="FIREBASE_PROJECT_ID"):
        IdentityToolkitAccounts(project_id="")
    with pytest.raises(ValueError, match="FIREBASE_PROJECT_ID"):
        FirebaseTokenVerifier(
            project_id="",
            members=frozenset({MEMBER}),
            sign_in_providers=frozenset({"google.com"}),
            keys=Keys(),
            accounts=FakeAccountDirectory.all_enabled(),
        )


# ---- the lookup's result reaches no log or span beyond `disabled` and the outcome ----


async def test_no_email_or_uid_reaches_a_span_or_a_log_from_the_account_check(
    span_exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    accounts = enabled()
    world = World(accounts)
    async with world.client() as c:
        await c.get("/v1/me", headers=bearer(mint()))
        accounts.register(UID, disabled=True)
        await c.get("/v1/me", headers=bearer(mint()))
        accounts.failure = RuntimeError(f"lookup of {UID} for {MEMBER} failed")
        await c.get("/v1/me", headers=bearer(mint()))
    checks = [s for s in span_exporter.get_finished_spans() if s.name == "auth.check_account"]
    assert [dict(s.attributes or {}) for s in checks] == [
        {"auth.account_disabled": False, "auth.outcome": "active"},
        {"auth.account_disabled": True, "auth.outcome": "disabled"},
        {"auth.outcome": "lookup_failed"},
    ]
    seen = " ".join(
        [r.getMessage() for r in caplog.records]
        + [str(v) for s in checks for v in (s.attributes or {}).values()]
    )
    assert UID not in seen and MEMBER not in seen
