"""The admin subscriptions panel (issue #302, ADR-0053 I5 and U6).

`/admin/subscriptions` over the library and the tenant's subscriptions, driven
end to end against `create_app` with fakes: an admin signs in (the session
cookie, as the browser holds it), reads the page, and ticks or unticks an
instrument the way htmx does (a form post carrying `HX-Request`) or the way a
browser without script does (a plain form post, which redirects back).

What is checked, in the acceptance criteria's order:

- an admin sees the library with the tenant's ticks, and changes them;
- unsubscribing leaves the clause store and the retrieval store byte-unchanged
  (and so every other tenant's subscriptions: the row is the only thing that
  moves);
- a member sees no link to the panel, and gets 403 on the page and on every
  write, whose effect on the ledger is nil.

Then the rules every page keeps: the one CSP, no inline script, handler or style,
markup in a title reaching the browser as text, a cross-origin post refused.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

import httpx
import pytest

from compliance_rag.adapters.fakes import (
    FakeClauseStore,
    FakeGenerator,
    FakeGraphStore,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.interfaces import DocumentClass, LawScope, Profile
from compliance_rag.api.app import create_app
from compliance_rag.api.sign_in import CONTENT_SECURITY_POLICY, STATIC_DIR
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.corpus.models import PUBLIC_TENANT_ID, Scope
from compliance_rag.eval.golden_corpus import golden_chunks
from compliance_rag.eval.harness import golden_clause_store, golden_ledger
from compliance_rag.graph.mappings import MappingSet
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import Settings
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier
from tests.api_support import MEMBER, SECOND, TENANT, api_settings, bearer, fake_api_services, mint

SECRET = "s3cr3t-" + "k" * 40
ORIGIN = "https://api"
PRIVACY = "privacy-act-1988"
SPAM = "spam-act-2003"
SPAM_TITLE = "Spam Act 2003"
STALE = "repealed-act-1999"
PAGE = "/admin/subscriptions"
HTMX = {"HX-Request": "true"}


# ---- the world ---------------------------------------------------------------------------


@dataclass
class World:
    app: httpx.AsyncClient
    ledger: FakeLedgerStore
    clauses: FakeClauseStore
    retrieval: FakeRetrievalStore


async def _library_ledger(spam_title: str = SPAM_TITLE) -> FakeLedgerStore:
    """The golden ledger (the Privacy Act, in force), a second library
    instrument, Acme subscribed to the Privacy Act alone, and another tenant
    subscribed to the Spam Act."""
    ledger = await golden_ledger()
    await ledger.record_version(
        tenant_id=PUBLIC_TENANT_ID,
        instrument_id=SPAM,
        version_id="C2024C00001",
        valid_from=dt.date(2020, 1, 1),
        supersedes=None,
        document_class=DocumentClass.LIBRARY_LAW,
        title=spam_title,
    )
    for tenant in (TENANT, "other-tenant"):
        await ledger.put_profile(Profile(tenant))
    await ledger.subscribe(tenant_id=TENANT, instrument_id=PRIVACY, subscribed_by="setup")
    await ledger.subscribe(tenant_id="other-tenant", instrument_id=SPAM, subscribed_by="setup")
    return ledger


async def _world(
    ledger: FakeLedgerStore | None = None, settings: Settings | None = None
) -> AsyncIterator[World]:
    ledger = ledger or await _library_ledger()
    settings = settings or api_settings(session_secret=SECRET)
    clauses = await golden_clause_store()
    retrieval = FakeRetrievalStore(golden_chunks(), ledger=ledger)
    services = fake_api_services(
        settings=settings,
        ledger=ledger,
        clauses=clauses,
        graph=FakeGraphStore(MappingSet(files=()), clause_validity={}),
        pipeline=AssessmentPipeline(
            RetrievalPipeline(retrieval, FakeReranker()),
            FakeGenerator(),
            Verifier(clauses, ledger, CanonicalCitationVerifier()),
            ledger,
            tenant_text=TenantTextGate.from_settings(settings),
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN},
    ) as client:
        yield World(client, ledger, clauses, retrieval)


async def _signed_in(world: World, email: str) -> World:
    response = await world.app.post("/session", headers=bearer(mint(email=email)))
    assert response.status_code == 200
    return world


@pytest.fixture
async def admin() -> AsyncIterator[World]:
    async for world in _world():
        yield await _signed_in(world, MEMBER)


@pytest.fixture
async def member() -> AsyncIterator[World]:
    """Listed in `API_MEMBERS`, not in `API_ADMINS`."""
    async for world in _world():
        yield await _signed_in(world, SECOND)


# ---- reading the page --------------------------------------------------------------------


class Rows(HTMLParser):
    """The panel's rows: instrument id to (its words, its button's label)."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str]]] = []
        self.text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, {k: v or "" for k, v in attrs}))

    def handle_data(self, data: str) -> None:
        self.text += data

    def where(self, tag: str, **attrs: str) -> list[dict[str, str]]:
        return [
            a for t, a in self.elements if t == tag and all(a.get(k) == v for k, v in attrs.items())
        ]


def parsed(page: str) -> Rows:
    rows = Rows()
    rows.feed(page)
    return rows


def rows_of(page: str) -> dict[str, str]:
    """Instrument id to its state, as the page draws it."""
    return {
        a["data-instrument"]: a["data-state"]
        for _, a in parsed(page).elements
        if "data-instrument" in a
    }


def words(page: str) -> str:
    import html

    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def snapshot(store: FakeClauseStore | FakeRetrievalStore) -> dict[str, Any]:  # Any: store state
    """Everything a fake store holds, except the ledger it reads from."""
    return copy.deepcopy({k: v for k, v in vars(store).items() if k != "_ledger"})


async def subscriptions(ledger: FakeLedgerStore, tenant: str = TENANT) -> list[str]:
    return sorted(await ledger.subscribed_instruments(tenant))


# ---- an admin sees and changes subscriptions ---------------------------------------------


async def test_an_admin_sees_the_library_with_the_tenants_ticks(admin: World) -> None:
    response = await admin.app.get(PAGE)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert rows_of(response.text) == {PRIVACY: "subscribed", SPAM: "unsubscribed"}
    text = words(response.text)
    assert "Privacy Act 1988" in text and SPAM_TITLE in text
    # another tenant's tick is not this tenant's
    assert await subscriptions(admin.ledger, "other-tenant") == [SPAM]


async def test_an_admin_subscribes_and_the_server_draws_the_new_state(admin: World) -> None:
    response = await admin.app.post(f"{PAGE}/{SPAM}/subscribe", headers=HTMX)
    assert response.status_code == 200
    assert rows_of(response.text) == {PRIVACY: "subscribed", SPAM: "subscribed"}
    assert await subscriptions(admin.ledger) == [PRIVACY, SPAM]
    # the same adapter operation the API route calls: in the admin's name, with the request's trace
    assert admin.ledger._subscriptions[(TENANT, SPAM)] == MEMBER  # pyright: ignore[reportPrivateUsage]
    assert admin.ledger.subscription_traces[(TENANT, SPAM)] == response.headers["x-trace-id"]
    # and the API reads it back
    assert (await admin.app.get("/v1/subscriptions")).json() == {"instruments": [PRIVACY, SPAM]}


async def test_an_admin_unsubscribes_and_the_server_draws_the_new_state(admin: World) -> None:
    response = await admin.app.post(f"{PAGE}/{PRIVACY}/unsubscribe", headers=HTMX)
    assert response.status_code == 200
    assert rows_of(response.text) == {PRIVACY: "unsubscribed", SPAM: "unsubscribed"}
    assert await subscriptions(admin.ledger) == []
    assert (await admin.app.get("/v1/subscriptions")).json() == {"instruments": []}


async def test_the_fragment_is_the_panel_alone_and_the_page_is_the_panel_in_the_shell(
    admin: World,
) -> None:
    fragment = (await admin.app.post(f"{PAGE}/{SPAM}/subscribe", headers=HTMX)).text
    page = (await admin.app.get(PAGE)).text
    assert "<html" not in fragment and "<body" not in fragment
    assert parsed(fragment).where("div", id="subscriptions")
    assert parsed(page).where("div", id="subscriptions") and "<html" in page


async def test_without_script_a_write_redirects_back_to_the_page(admin: World) -> None:
    response = await admin.app.post(f"{PAGE}/{SPAM}/subscribe")
    assert response.status_code == 303 and response.headers["location"] == PAGE
    assert await subscriptions(admin.ledger) == [PRIVACY, SPAM]
    response = await admin.app.post(f"{PAGE}/{SPAM}/unsubscribe")
    assert response.status_code == 303 and response.headers["location"] == PAGE
    assert await subscriptions(admin.ledger) == [PRIVACY]


async def test_every_button_is_a_post_form_with_its_own_stable_id(admin: World) -> None:
    page = parsed((await admin.app.get(PAGE)).text)
    forms = {a["action"]: a for a in page.where("form")}
    assert set(forms) == {f"{PAGE}/{PRIVACY}/unsubscribe", f"{PAGE}/{SPAM}/subscribe"}
    for attrs in forms.values():
        assert attrs["method"] == "post"
        assert attrs["hx-post"] == attrs["action"]
        assert attrs["hx-target"] == "#subscriptions" and attrs["hx-swap"] == "outerHTML"
    # htmx puts focus back on the element with the id it had before the swap
    assert page.where("button", id=f"toggle-{PRIVACY}") and page.where(
        "button", id=f"toggle-{SPAM}"
    )


async def test_subscribing_twice_and_unsubscribing_what_was_never_ticked_are_harmless(
    admin: World,
) -> None:
    await admin.app.post(f"{PAGE}/{SPAM}/subscribe", headers=HTMX)
    again = await admin.app.post(f"{PAGE}/{SPAM}/subscribe", headers=HTMX)
    assert again.status_code == 200 and await subscriptions(admin.ledger) == [PRIVACY, SPAM]
    await admin.app.post(f"{PAGE}/{SPAM}/unsubscribe", headers=HTMX)
    once_more = await admin.app.post(f"{PAGE}/{SPAM}/unsubscribe", headers=HTMX)
    assert once_more.status_code == 200 and await subscriptions(admin.ledger) == [PRIVACY]


@pytest.mark.parametrize("instrument", ["not:a-library-id", "acme~policy", "has%20a%20space"])
async def test_an_id_the_adapter_refuses_is_said_in_words_and_nothing_is_recorded(
    admin: World, instrument: str
) -> None:
    fragment = await admin.app.post(f"{PAGE}/{instrument}/subscribe", headers=HTMX)
    assert fragment.status_code == 200  # htmx does not swap an error status
    assert parsed(fragment.text).where("p", role="alert")
    assert rows_of(fragment.text) == {PRIVACY: "subscribed", SPAM: "unsubscribed"}
    page = await admin.app.post(f"{PAGE}/{instrument}/subscribe")
    assert page.status_code == 422 and "<html" in page.text
    assert "Not subscribed:" in words(page.text)
    assert await subscriptions(admin.ledger) == [PRIVACY]


async def test_a_subscription_the_library_no_longer_lists_can_still_be_removed(
    admin: World,
) -> None:
    await admin.ledger.subscribe(tenant_id=TENANT, instrument_id=STALE, subscribed_by="setup")
    page = (await admin.app.get(PAGE)).text
    assert rows_of(page)[STALE] == "subscribed"
    assert "not in the library" in words(page)
    response = await admin.app.post(f"{PAGE}/{STALE}/unsubscribe", headers=HTMX)
    assert STALE not in rows_of(response.text)
    assert await subscriptions(admin.ledger) == [PRIVACY]


# ---- unsubscribing touches the row and nothing else --------------------------------------


async def test_unsubscribing_leaves_the_clause_and_retrieval_stores_byte_unchanged(
    admin: World,
) -> None:
    clauses_before, retrieval_before = snapshot(admin.clauses), snapshot(admin.retrieval)
    assert clauses_before["_clauses"] and retrieval_before["_chunks"]  # something to leave alone
    other_before = await subscriptions(admin.ledger, "other-tenant")

    for path in (f"{PAGE}/{PRIVACY}/unsubscribe", f"{PAGE}/{PRIVACY}/subscribe"):
        assert (await admin.app.post(path, headers=HTMX)).status_code == 200
    assert (await admin.app.post(f"{PAGE}/{PRIVACY}/unsubscribe", headers=HTMX)).status_code == 200

    assert await subscriptions(admin.ledger) == []
    assert snapshot(admin.clauses) == clauses_before
    assert snapshot(admin.retrieval) == retrieval_before
    assert await subscriptions(admin.ledger, "other-tenant") == other_before


async def test_unsubscribing_ends_retrieval_of_the_instrument_and_ticking_again_restores_it(
    admin: World,
) -> None:
    async def visible() -> int:
        result = await admin.retrieval.search(
            "steps to protect personal information",
            law=LawScope.unscoped("test: the panel's effect on what the tenant can read"),
            tenant_id=TENANT,
            scopes=(Scope.PUBLIC,),
            as_of=dt.date(2026, 11, 8),
            limit=5,
        )
        return len(result.fused)

    assert await visible() > 0
    await admin.app.post(f"{PAGE}/{PRIVACY}/unsubscribe", headers=HTMX)
    assert await visible() == 0
    await admin.app.post(f"{PAGE}/{PRIVACY}/subscribe", headers=HTMX)
    assert await visible() > 0


# ---- a member sees no panel and is refused ------------------------------------------------


async def test_a_member_gets_a_403_page_not_the_panel(member: World) -> None:
    response = await member.app.get(PAGE)
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "subscriptions" not in rows_of(response.text) and rows_of(response.text) == {}
    assert PRIVACY not in response.text and SPAM_TITLE not in response.text


@pytest.mark.parametrize("action", ["subscribe", "unsubscribe"])
@pytest.mark.parametrize("headers", [{}, HTMX], ids=["plain", "htmx"])
async def test_a_member_gets_403_on_every_write_and_nothing_changes(
    member: World, action: str, headers: dict[str, str]
) -> None:
    before = await subscriptions(member.ledger)
    traces = dict(member.ledger.subscription_traces)
    response = await member.app.post(f"{PAGE}/{SPAM}/{action}", headers=headers)
    assert response.status_code == 403
    response = await member.app.post(f"{PAGE}/{PRIVACY}/{action}", headers=headers)
    assert response.status_code == 403
    assert await subscriptions(member.ledger) == before
    assert member.ledger.subscription_traces == traces


async def test_a_member_sees_no_link_to_the_panel_and_an_admin_does(
    member: World, admin: World
) -> None:
    for path in ("/chats", "/chats/new"):
        mine = await member.app.get(path)
        assert mine.status_code == 200 and PAGE not in mine.text, path
        theirs = await admin.app.get(path)
        assert parsed(theirs.text).where("a", href=PAGE), path
    # an error page names no one's role, so it has no link for anyone
    assert PAGE not in (await member.app.get(PAGE)).text


async def test_the_role_is_read_on_every_request_so_a_removed_admin_loses_the_panel() -> None:
    settings = api_settings(session_secret=SECRET)
    async for world in _world(settings=settings):
        await _signed_in(world, MEMBER)
        assert (await world.app.get(PAGE)).status_code == 200
        # the same app, the same cookie: only configuration changes
        object.__setattr__(settings, "api_admins", "")
        assert (await world.app.get(PAGE)).status_code == 403
        assert (await world.app.post(f"{PAGE}/{SPAM}/subscribe", headers=HTMX)).status_code == 403
        assert PAGE not in (await world.app.get("/chats")).text
        assert await subscriptions(world.ledger) == [PRIVACY]


async def test_nobody_signed_in_is_sent_to_sign_in_and_changes_nothing() -> None:
    async for world in _world():
        for method, path in (
            ("GET", PAGE),
            ("POST", f"{PAGE}/{SPAM}/subscribe"),
            ("POST", f"{PAGE}/{PRIVACY}/unsubscribe"),
        ):
            response = await world.app.request(method, path)
            assert response.status_code == 303, (method, path)
            assert response.headers["location"] == "/signin"
        assert await subscriptions(world.ledger) == [PRIVACY]


async def test_an_address_outside_the_workspace_is_refused_even_if_listed_as_an_admin() -> None:
    settings = api_settings(
        session_secret=SECRET, api_admins=f"{MEMBER}, outsider@elsewhere.example"
    )
    async for world in _world(settings=settings):
        # a verified token for a non-member never gets a session at all
        response = await world.app.post(
            "/session", headers=bearer(mint(email="outsider@elsewhere.example"))
        )
        assert response.status_code == 403
        assert (await world.app.get(PAGE)).status_code == 303


# ---- the rules every page keeps ----------------------------------------------------------


async def test_a_post_from_another_origin_with_the_cookie_is_refused(admin: World) -> None:
    for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}):
        response = await admin.app.post(f"{PAGE}/{SPAM}/subscribe", headers=headers)
        assert response.status_code == 403
        response = await admin.app.post(f"{PAGE}/{PRIVACY}/unsubscribe", headers=headers)
        assert response.status_code == 403
    assert await subscriptions(admin.ledger) == [PRIVACY]


async def test_the_one_strict_csp_is_on_the_page_the_fragment_and_every_refusal(
    admin: World, member: World
) -> None:
    responses = {
        "page": await admin.app.get(PAGE),
        "fragment": await admin.app.post(f"{PAGE}/{SPAM}/subscribe", headers=HTMX),
        "redirect": await admin.app.post(f"{PAGE}/{SPAM}/unsubscribe"),
        "member page": await member.app.get(PAGE),
        "member write": await member.app.post(f"{PAGE}/{SPAM}/subscribe", headers=HTMX),
        "GET on a write (405)": await admin.app.get(f"{PAGE}/{SPAM}/subscribe"),
        "cross-origin": await admin.app.post(
            f"{PAGE}/{SPAM}/subscribe", headers={"Origin": "https://evil.example"}
        ),
    }
    assert [r.status_code for r in responses.values()] == [200, 200, 303, 403, 403, 405, 403]
    for name, response in responses.items():
        assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY, name
        assert response.headers["x-content-type-options"] == "nosniff", name
        assert response.headers["cache-control"] == "no-store", name


async def test_the_page_has_no_inline_script_handler_or_style_and_loads_only_our_files(
    admin: World,
) -> None:
    page = (await admin.app.get(PAGE)).text
    fragment = (await admin.app.post(f"{PAGE}/{SPAM}/subscribe", headers=HTMX)).text
    for name, text in (("page", page), ("fragment", fragment)):
        scan = parsed(text)
        for tag, attrs in scan.elements:
            assert tag != "style", name
            assert not [k for k in attrs if k.startswith("on")], (name, tag)
            assert "style" not in attrs, (name, tag)
            for key in ("src", "href", "action"):
                value = attrs.get(key, "")
                assert not re.match(r"(?i)\s*(javascript:|data:|https?:|//)", value), (name, value)
        assert "<script>" not in text, name
    assert sorted(a["src"] for a in parsed(page).where("script")) == [
        "/static/admin.js",
        "/static/ask.js",
        "/static/vendor/htmx/htmx.min.js",
    ]
    assert not parsed(fragment).where("script")


async def test_markup_in_a_library_title_reaches_the_browser_as_text() -> None:
    hostile = '<img src=x onerror=alert(1)> & "quoted" <script>alert(2)</script>'
    async for world in _world(await _library_ledger(spam_title=hostile)):
        await _signed_in(world, MEMBER)
        for text in (
            (await world.app.get(PAGE)).text,
            (await world.app.post(f"{PAGE}/{SPAM}/subscribe", headers=HTMX)).text,
        ):
            assert "<img" not in text and "<script>alert" not in text
            assert "&lt;img src=x onerror=alert(1)&gt;" in text
            assert rows_of(text)[SPAM] in {"subscribed", "unsubscribed"}


def test_admin_js_never_writes_html_and_is_not_inline() -> None:
    script = (STATIC_DIR / "admin.js").read_text()
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "createContextualFragment",
        "DOMParser",
    ):
        assert forbidden not in script, forbidden
    assert "http://" not in script and "https://" not in script


async def test_the_panel_is_made_for_a_phone(admin: World) -> None:
    scan = parsed((await admin.app.get(PAGE)).text)
    (viewport,) = scan.where("meta", name="viewport")
    assert viewport["content"] == "width=device-width, initial-scale=1"
    css = (STATIC_DIR / "app.css").read_text()
    assert not re.search(r"(?<!-)width:\s*\d+(\.\d+)?px", css)
    assert (await admin.app.get("/static/admin.js")).status_code == 200


async def test_the_api_routes_still_take_the_same_writes_and_refuse_a_member(
    admin: World, member: World
) -> None:
    """The panel adds no rule of its own: the routes it shares an operation
    with answer as they did (#294)."""
    assert (await admin.app.put(f"/v1/subscriptions/{SPAM}")).status_code == 204
    assert (await admin.app.get(PAGE)).status_code == 200
    assert (await member.app.delete(f"/v1/subscriptions/{PRIVACY}")).status_code == 403
    assert (await admin.app.put("/v1/subscriptions/not:a-library-id")).status_code == 422
