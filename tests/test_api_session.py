"""A signed-in session for server-rendered pages (issue #280).

`POST /session` swaps a verified Firebase ID token for a signed cookie, and
`current_member` accepts that cookie as it accepts the Bearer header. Tokens are
forged with the stand-in keys of `api_support`; cookies are sealed by the real
`SessionCodec` (a forged one under another secret, an expired one under a past
expiry), and requests go through the real app, so the Origin guard, the verifier
and the routes are all the real ones. `httpx` stores `Secure` cookies only for an
`https` base URL, which is the right test of the `Secure` flag.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import AsyncIterator
from html.parser import HTMLParser
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.interfaces import Member
from compliance_rag.api.app import create_app
from compliance_rag.api.session import COOKIE_NAME, MAX_SESSION_SECONDS, SessionCodec
from compliance_rag.api.sign_in import CONTENT_SECURITY_POLICY, STATIC_DIR
from compliance_rag.settings import MIN_SESSION_SECRET_CHARS, Settings
from tests.api_support import (
    MEMBER,
    PROJECT,
    TENANT,
    api_settings,
    bearer,
    fake_api_services,
    mint,
)

SECRET = "s3cr3t-" + "k" * 40
OTHER_SECRET = "0ther-" + "z" * 40
ORIGIN = "https://api"
WEB_KEY = "AIza-web-key-not-a-secret"
VENDORED = STATIC_DIR / "vendor" / "firebase"


def _app(**settings: Any):  # Any: settings field values
    values: dict[str, Any] = {  # Any: settings field values
        "session_secret": SECRET,
        "firebase_web_api_key": WEB_KEY,
        **settings,
    }
    return create_app(fake_api_services(settings=api_settings(**values)))


def _client(app: Any = None) -> httpx.AsyncClient:  # Any: the ASGI app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app or _app()), base_url=ORIGIN)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with _client() as c:
        yield c


async def sign_in(client: httpx.AsyncClient, **claims: Any) -> httpx.Response:  # Any: claims
    return await client.post("/session", headers=bearer(mint(**claims)))


def sealed(
    secret: str = SECRET,
    *,
    email: str = MEMBER,
    expires_in: float = 600,
) -> str:
    return SessionCodec(secret).seal(
        Member(uid="uid-123", email=email), expires_at=int(time.time() + expires_in)
    )


def with_cookie(value: str) -> dict[str, str]:
    return {"Cookie": f"{COOKIE_NAME}={value}"}


# ---- a valid token gives a cookie, and the cookie signs in ------------------------------


async def test_a_valid_token_gives_a_cookie_that_authenticates_the_member(
    client: httpx.AsyncClient,
) -> None:
    response = await sign_in(client)
    assert response.status_code == 200
    assert COOKIE_NAME in client.cookies

    me = await client.get("/v1/me")  # the cookie alone, no Authorization header
    assert me.status_code == 200
    assert me.json() == {"tenant_id": TENANT, "uid": "uid-123", "email": MEMBER}


async def test_the_cookie_is_httponly_secure_samesite_strict_and_holds_only_uid_and_email(
    client: httpx.AsyncClient,
) -> None:
    response = await sign_in(client)
    header = response.headers["set-cookie"]
    flags = {part.strip().lower() for part in header.split(";")[1:]}
    assert {"httponly", "secure", "samesite=strict", "path=/"} <= flags
    assert not any(f.startswith("domain") for f in flags)  # __Host- forbids it
    assert response.headers["cache-control"] == "no-store"

    value = client.cookies[COOKIE_NAME]
    member = SessionCodec(SECRET).open(value)
    assert (member.uid, member.email) == ("uid-123", MEMBER)
    import base64
    import json

    payload = json.loads(base64.urlsafe_b64decode(value.split(".")[0] + "=="))
    assert set(payload) == {"v", "uid", "email", "exp"}


async def test_the_cookie_expires_no_later_than_the_token(client: httpx.AsyncClient) -> None:
    now = int(time.time())
    response = await sign_in(client, iat=now, auth_time=now, exp=now + 300)
    max_age = int(re.search(r"Max-Age=(\d+)", response.headers["set-cookie"]).group(1))  # type: ignore[union-attr]
    assert 0 < max_age <= 300
    member = SessionCodec(SECRET).open(client.cookies[COOKIE_NAME])
    assert member.expires_at is not None and member.expires_at <= now + 300


async def test_a_long_lived_token_still_gets_a_session_of_at_most_an_hour(
    client: httpx.AsyncClient,
) -> None:
    now = int(time.time())
    response = await sign_in(client, exp=now + 86400)
    max_age = int(re.search(r"Max-Age=(\d+)", response.headers["set-cookie"]).group(1))  # type: ignore[union-attr]
    assert max_age <= MAX_SESSION_SECONDS


async def test_a_bearer_header_is_never_overridden_by_a_cookie() -> None:
    async with _client() as c:
        response = await c.get(
            "/v1/me", headers={**bearer(mint(email="second@acme.example")), **with_cookie(sealed())}
        )
        assert response.json()["email"] == "second@acme.example"
        bad = await c.get("/v1/me", headers={**bearer("a.b.c"), **with_cookie(sealed())})
        assert bad.status_code == 401


# ---- forged, expired or tampered: 401 ----------------------------------------------------


def _tampered() -> str:
    payload, _, signature = sealed().partition(".")
    other = sealed(email="someone@elsewhere.example").split(".")[0]
    return f"{other}.{signature}" if payload != other else f"{payload}x.{signature}"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(sealed(OTHER_SECRET), id="signed-under-another-secret"),
        pytest.param(sealed(expires_in=-5), id="expired"),
        pytest.param(_tampered(), id="payload-swapped-under-the-old-signature"),
        pytest.param(sealed().rsplit(".", 1)[0], id="signature-removed"),
        pytest.param(sealed()[:-4], id="signature-truncated"),
        pytest.param(sealed().split(".")[0] + ".", id="empty-signature"),
        pytest.param("garbage", id="not-a-session"),
        pytest.param("a.b", id="two-junk-segments"),
        pytest.param("x" * 5000, id="oversized"),
        pytest.param("", id="empty"),
    ],
)
async def test_a_forged_expired_or_tampered_cookie_is_a_401(
    client: httpx.AsyncClient, value: str
) -> None:
    response = await client.get("/v1/me", headers=with_cookie(value))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_a_cookie_with_a_valid_signature_but_a_malformed_payload_is_a_401(
    client: httpx.AsyncClient,
) -> None:
    codec = SessionCodec(SECRET)
    import base64

    for body in (b"[]", b'{"v":1,"uid":1,"email":"a@b","exp":9999999999}', b'{"v":2}', b"\xff"):
        payload = base64.urlsafe_b64encode(body).rstrip(b"=").decode()
        value = f"{payload}.{codec._signature(payload)}"  # pyright: ignore[reportPrivateUsage]
        assert (await client.get("/v1/me", headers=with_cookie(value))).status_code == 401


async def test_a_member_removed_from_the_tenant_loses_their_session_at_once() -> None:
    async with _client(_app(api_members="someone.else@acme.example")) as c:
        response = await c.get("/v1/me", headers=with_cookie(sealed()))
        assert response.status_code == 403


async def test_a_session_secret_that_is_not_set_turns_cookie_sessions_off() -> None:
    async with _client(_app(session_secret="")) as c:
        assert (await sign_in(c)).status_code == 503
        assert COOKIE_NAME not in c.cookies
        assert (await c.get("/v1/me", headers=with_cookie(sealed()))).status_code == 401
        assert (await c.get("/v1/me", headers=bearer(mint()))).status_code == 200


async def test_sign_in_stays_required_on_every_route(client: httpx.AsyncClient) -> None:
    for method, path in (
        ("GET", "/v1/me"),
        ("GET", "/v1/subscriptions"),
        ("GET", "/v1/chats"),
        ("POST", "/session"),
    ):
        assert (await client.request(method, path)).status_code == 401


async def test_the_cookie_cannot_start_another_session(client: httpx.AsyncClient) -> None:
    response = await client.post("/session", headers={**with_cookie(sealed()), "Origin": ORIGIN})
    assert response.status_code == 401
    assert "set-cookie" not in response.headers


# ---- a non-member gets no cookie ---------------------------------------------------------


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"email": "someone@elsewhere.example"}, id="not-on-the-list"),
        pytest.param({"email_verified": False}, id="unverified-email"),
        pytest.param({"firebase": {"sign_in_provider": "oidc.any-idp"}}, id="untrusted-provider"),
    ],
)
async def test_a_non_members_token_gets_no_cookie(
    client: httpx.AsyncClient,
    claims: dict[str, Any],  # Any: JWT claim values
) -> None:
    response = await sign_in(client, **claims)
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert COOKIE_NAME not in client.cookies


@pytest.mark.parametrize("token", ["a.b.c", mint(aud="another-project"), mint(exp=1, iat=0)])
async def test_an_invalid_token_gets_no_cookie(client: httpx.AsyncClient, token: str) -> None:
    response = await client.post("/session", headers=bearer(token))
    assert response.status_code == 401 and "set-cookie" not in response.headers


# ---- CSRF: the Origin check --------------------------------------------------------------


async def test_a_cross_origin_post_with_the_cookie_is_refused(client: httpx.AsyncClient) -> None:
    await sign_in(client)
    for headers in (
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://api"},  # right host, wrong scheme
        {"Origin": "https://api.evil.example"},
        {},  # no Origin at all
    ):
        refused = await client.put("/v1/subscriptions/privacy-act-1988", headers=headers)
        assert refused.status_code == 403, headers
        assert refused.json() == {"detail": "cross-origin request refused"}
    assert (await client.get("/v1/subscriptions")).json() == {"instruments": []}  # nothing ran


async def test_every_state_changing_method_needs_the_origin_not_just_post(
    client: httpx.AsyncClient,
) -> None:
    await sign_in(client)
    evil = {"Origin": "https://evil.example"}
    assert (await client.delete("/v1/subscriptions/x", headers=evil)).status_code == 403
    assert (await client.post("/v1/answers", headers=evil, json={})).status_code == 403
    assert (await client.post("/session/logout", headers=evil)).status_code == 403


async def test_a_same_origin_post_with_the_cookie_goes_through(client: httpx.AsyncClient) -> None:
    await sign_in(client)
    ok = await client.put("/v1/subscriptions/privacy-act-1988", headers={"Origin": ORIGIN})
    assert ok.status_code == 204
    assert (await client.get("/v1/subscriptions")).json() == {"instruments": ["privacy-act-1988"]}


async def test_a_configured_origin_may_post_with_the_cookie() -> None:
    other = "https://app.acme.example"
    async with _client(_app(api_allowed_origins=f"{other}/, https://x.example")) as c:
        await sign_in(c)
        assert (await c.post("/session/logout", headers={"Origin": "https://y"})).status_code == 403
        assert (await c.post("/session/logout", headers={"Origin": other})).status_code == 204


async def test_loopback_http_is_a_same_origin_for_local_development() -> None:
    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080"
    ) as c:
        ok = await c.post(
            "/session/logout",
            headers={**with_cookie(sealed()), "Origin": "http://localhost:8080"},
        )
        assert ok.status_code == 204


async def test_the_origin_check_leaves_reads_and_bearer_clients_alone(
    client: httpx.AsyncClient,
) -> None:
    await sign_in(client)
    assert (
        await client.get("/v1/me", headers={"Origin": "https://evil.example"})
    ).status_code == 200
    async with _client() as bare:  # a script: Bearer, no cookie, no Origin
        put = await bare.put("/v1/subscriptions/privacy-act-1988", headers=bearer(mint()))
        assert put.status_code == 204


async def test_logout_clears_the_cookie_and_the_session_ends(client: httpx.AsyncClient) -> None:
    await sign_in(client)
    response = await client.post("/session/logout", headers={"Origin": ORIGIN})
    assert response.status_code == 204
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert COOKIE_NAME not in client.cookies
    assert (await client.get("/v1/me")).status_code == 401


# ---- the secret reaches no log, span, repr or error --------------------------------------


async def test_the_secret_and_the_cookie_reach_no_log_or_span(
    caplog: pytest.LogCaptureFixture, span_exporter: InMemorySpanExporter
) -> None:
    caplog.set_level(logging.DEBUG)
    token = mint()
    async with _client() as c:
        await c.post("/session", headers=bearer(token))
        value = c.cookies[COOKIE_NAME]
        await c.get("/v1/me")
        await c.get("/v1/me", headers=with_cookie(sealed(OTHER_SECRET)))
        await c.post("/session/logout", headers={"Origin": "https://evil.example"})
        await c.get("/signin")
    needles = (SECRET, value, token)
    for record in caplog.records:
        text = record.getMessage() + str(record.args) + str(record.__dict__.get("exc_text"))
        assert all(n not in text for n in needles)
    spans = span_exporter.get_finished_spans()
    assert spans
    for recorded in spans:
        assert all(n not in str(v) for v in (recorded.attributes or {}).values() for n in needles)
        assert all(n not in str(e.attributes) for e in recorded.events for n in needles)


def test_the_secret_is_redacted_in_settings_and_in_a_refusal() -> None:
    settings = api_settings(session_secret=SECRET)
    assert SECRET not in repr(settings) and SECRET not in str(settings.session_secret)
    assert SECRET not in repr(settings.model_dump())

    short = "short-secret-value"
    assert len(short) < MIN_SESSION_SECRET_CHARS
    with pytest.raises(ValueError, match="at least") as refused:
        Settings(_env_file=None, session_secret=short)  # pyright: ignore[reportCallIssue]
    assert short not in str(refused.value)


# ---- the sign-in page and the vendored SDK -----------------------------------------------


class _Refs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.inline_scripts = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        for name in ("src", "href", "action"):
            if values.get(name):
                self.urls.append(values[name] or "")
        if tag == "script" and not values.get("src"):
            self.inline_scripts += 1
        if "style" in values:
            self.inline_scripts += 1


async def test_the_sign_in_page_is_one_origin_with_a_strict_csp(client: httpx.AsyncClient) -> None:
    response = await client.get("/signin")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    assert csp == CONTENT_SECURITY_POLICY
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "default-src 'none'" in csp and "script-src 'self'" in csp
    refs = _Refs()
    refs.feed(response.text)
    assert refs.urls and all(u.startswith("/") and not u.startswith("//") for u in refs.urls)
    assert refs.inline_scripts == 0
    assert f'data-api-key="{WEB_KEY}"' in response.text
    assert f'data-project-id="{PROJECT}"' in response.text
    assert f'data-auth-domain="{PROJECT}.firebaseapp.com"' in response.text


async def test_the_sign_in_page_needs_no_sign_in_but_is_off_when_unconfigured() -> None:
    async with _client(_app(firebase_web_api_key="")) as c:
        assert (await c.get("/signin")).status_code == 503


async def test_the_pages_script_loads_nothing_from_another_origin(
    client: httpx.AsyncClient,
) -> None:
    script = (await client.get("/static/signin.js")).text
    imports = re.findall(r'from\s+"([^"]+)"', script)
    assert imports and all(i.startswith("./vendor/firebase/") for i in imports)
    assert "http://" not in script and "https://" not in script.replace("https://www", "")


async def test_the_vendored_sdk_is_served_by_the_api_and_matches_its_recorded_hashes(
    client: httpx.AsyncClient,
) -> None:
    readme = (VENDORED / "README.md").read_text()
    assert re.search(r"\*\*version \d+\.\d+\.\d+\*\*", readme)
    for name in ("firebase-app.js", "firebase-auth.js"):
        response = await client.get(f"/static/vendor/firebase/{name}")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
        assert response.headers["x-content-type-options"] == "nosniff"
        digest = hashlib.sha256(response.content).hexdigest()
        row = next(line for line in readme.splitlines() if line.startswith(f"| `{name}`"))
        recorded = re.findall(r"`([0-9a-f]{64})`", row)
        assert len(recorded) == 2
        assert digest == recorded[1], f"{name} changed without its hash in the README"
        # Its imports are local. (The URL stays as a label string inside the SDK.)
        assert not re.search(rb"""(?:from|import)\s*["']https?:""", response.content)
    # The package version in the README is the version the SDK reports itself as.
    version = re.search(r"\*\*version (\d+\.\d+\.\d+)\*\*", readme)
    assert version is not None
    assert version.group(1).encode() in (VENDORED / "firebase-app.js").read_bytes()


async def test_only_the_auth_sdk_is_vendored() -> None:
    assert {p.name for p in VENDORED.iterdir()} == {
        "README.md",
        "firebase-app.js",
        "firebase-auth.js",
    }
