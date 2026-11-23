"""The mapping queue page (#305, ADR-0053 I5, ADR-0059 Q8): `/admin/mappings`.

An admin sees each policy version in force, a "Propose mappings for ..." button, and the
mappings the ledger holds for it in every status, read-only: approving and rejecting are
#306's. Driven end to end against `create_app` over fakes, with the admin signed in by the
session cookie as a browser holds it, over Acme's six synthetic policies and a scripted model.
Nothing is billable.
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from typing import Any

import httpx
import pytest

from compliance_rag.api import admin_mappings, mapping_proposals
from compliance_rag.api.pages import nav_sections
from compliance_rag.api.sign_in import CONTENT_SECURITY_POLICY, STATIC_DIR
from tests.api_support import MEMBER, SECOND, assert_script_writes_no_html, bearer, mint
from tests.findings_support import DAY, TENANT
from tests.mapping_support import APP11_1, MODEL, SEC_3_1, SECURITY, NamedModel, item, reply
from tests.test_api_check_claims import events_of
from tests.test_api_checks import ORIGIN
from tests.test_api_mapping_proposals import World, world

PAGE = "/admin/mappings"
POLICIES = (
    "acme/access-correction-procedure",
    "acme/data-security-policy",
    "acme/employee-policy",
    "acme/marketing-policy",
    "acme/privacy-policy",
    "acme/vendor-policy",
)


@pytest.fixture(autouse=True)
def _a_fixed_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_mappings, "today", lambda: DAY)
    monkeypatch.setattr(mapping_proposals, "today", lambda: DAY)


class Page(HTMLParser):
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


def parsed(text: str) -> Page:
    page = Page()
    page.feed(text)
    return page


def words(text: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


@asynccontextmanager
async def browser(w: World, email: str = MEMBER) -> AsyncGenerator[httpx.AsyncClient]:
    """A client signed in with the session cookie, as the page's script is."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=w.app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        response = await client.post("/session", headers=bearer(mint(email=email)))
        assert response.status_code == 200
        yield client


async def ask(w: World) -> None:
    async with w.client(mint()) as client:
        response = await client.post(
            "/v1/policy-mappings/proposals",
            json={"instrument_id": SECURITY[0], "version_id": SECURITY[1]},
        )
    assert events_of(response.text)[-1][0] == "proposals"


# ---- an admin sees the versions in force, with a button for each -------------------------


async def test_an_admin_sees_each_policy_version_in_force_with_a_button_and_no_mappings() -> None:
    w = await world()
    async with browser(w) as client:
        response = await client.get(PAGE)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    page = parsed(response.text)
    forms = page.where("form", **{"class": "propose"})
    assert sorted(f["data-instrument"] for f in forms) == list(POLICIES)
    assert {f["data-version"] for f in forms} == {"v1"}
    text = words(response.text)
    security = (
        f"Propose mappings for Acme Pty Ltd — Data Security and Retention Policy ({SECURITY[1]})"
    )
    assert "Propose mappings for" in text and security.split("(")[0] in text
    assert text.count("No mappings have been proposed for this version.") == 6
    assert len(page.where("button", type="submit")) == 6


async def test_the_page_lists_the_mappings_read_only_with_the_models_words_labelled() -> None:
    w = await world()
    await ask(w)
    async with browser(w) as client:
        response = await client.get(PAGE)
    text = words(response.text)
    page = parsed(response.text)
    listed = page.where("li", **{"data-status": "proposed"})
    assert [a["data-kind"] for a in listed] == ["implements", "partially_implements"]
    assert "Proposed" in text and f"proposed by {MODEL}" in text
    assert (
        "The model's reasoning (not evidence):" in text
        and "Section 3.2 covers retention only." in text
    )
    assert APP11_1 in text and "partially implements" in text
    # the clause opens at its source
    hrefs = [a["href"] for a in page.where("a") if SECURITY[0].split("/")[1] in a.get("href", "")]
    assert hrefs and all(h.startswith("/") for h in hrefs)
    # the five other versions still say nothing was proposed for them
    assert text.count("No mappings have been proposed for this version.") == 5


async def test_the_page_has_no_control_to_approve_or_reject_and_nothing_to_post_but_the_ask() -> (
    None
):
    """#306 builds those. A proposal is shown as a proposal, and approving one is not possible
    from here: the page's only form asks for proposals, and posts to no route of its own."""
    w = await world()
    await ask(w)
    async with browser(w) as client:
        page = (await client.get(PAGE)).text
    scan = parsed(page)
    assert {a.get("class") for a in scan.where("form")} == {"propose"}
    assert not [a for a in scan.where("form") if a.get("action") or a.get("hx-post")]
    lowered = words(page).lower()
    assert "approve" not in lowered.replace("is not approved", "")  # the note says it is not
    assert "reject" not in lowered.replace("approving or rejecting", "")
    # and a decision cannot be made by posting at the page
    async with browser(w) as client:
        for path in (PAGE, f"{PAGE}/1/approve", f"{PAGE}/1/reject"):
            assert (await client.post(path)).status_code in {404, 405}


async def test_a_run_going_or_failed_is_said_and_a_version_out_of_force_has_no_button() -> None:
    w = await world()
    await ask(w)
    await w.ledger.reserve_mapping_run(TENANT, "acme/privacy-policy", "v1", trace_id="a" * 32)
    await w.ledger.reserve_mapping_run(TENANT, "acme/vendor-policy", "v1", trace_id="b" * 32)
    await w.ledger.fail_mapping_run(TENANT, "acme/vendor-policy", "v1", trace_id="b" * 32)
    await w.ledger.end_version(
        tenant_id=TENANT, instrument_id=SECURITY[0], version_id="v1", valid_to=DAY
    )
    async with browser(w) as client:
        page = (await client.get(PAGE)).text
    sections = {s["data-instrument"]: s for s in parsed(page).where("section")}
    assert set(sections) == set(POLICIES)  # the ended version stays, with its mappings
    text = words(page)
    assert "proposing now" in text and "the last run did not finish" in text
    assert "not in force today" in text
    forms = {f["data-instrument"] for f in parsed(page).where("form", **{"class": "propose"})}
    assert SECURITY[0] not in forms and len(forms) == 5
    assert len(parsed(page).where("button", disabled="")) == 1  # the one being proposed for
    assert "Proposed" in text  # the ended version's mappings are still listed


async def test_markup_in_what_the_model_wrote_reaches_the_browser_as_text() -> None:
    hostile = '<img src=x onerror=alert(1)> & "quoted" <script>alert(2)</script>'
    w = await world(NamedModel(reply(item(APP11_1, SEC_3_1, rationale=hostile))))
    await ask(w)
    async with browser(w) as client:
        page = (await client.get(PAGE)).text
    assert "<img" not in page and "<script>alert" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page


# ---- admins only, on every request --------------------------------------------------------


async def test_a_member_gets_a_403_page_and_no_mapping_is_shown() -> None:
    w = await world()
    await ask(w)
    async with browser(w, SECOND) as client:
        response = await client.get(PAGE)
    assert response.status_code == 403
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert not parsed(response.text).where("form", **{"class": "propose"})
    assert APP11_1 not in response.text and "Propose mappings" not in response.text


async def test_a_signed_out_request_is_sent_to_sign_in() -> None:
    w = await world()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=w.app), base_url=ORIGIN
    ) as client:
        response = await client.get(PAGE)
    assert response.status_code == 303 and response.headers["location"] == "/signin"


async def test_the_sidebar_links_the_page_for_an_admin_and_not_for_a_member() -> None:
    w = await world()
    async with browser(w) as client:
        admin_page = (await client.get(PAGE)).text
    async with browser(w, SECOND) as client:
        member_page = (await client.get("/chats")).text
    link = parsed(admin_page).where("a", href=PAGE)
    assert link and link[0].get("aria-current") == "page"
    assert not parsed(member_page).where("a", href=PAGE)
    items = [i for s in nav_sections(PAGE, admin=True) for i in s.items]
    assert [i.label for i in items if i.href == PAGE] == ["Mapping proposals"]
    assert not [i for s in nav_sections(PAGE, admin=False) for i in s.items if i.href == PAGE]


# ---- the rules every page keeps ------------------------------------------------------------


async def test_the_page_has_no_inline_script_handler_or_style_and_loads_only_our_files() -> None:
    w = await world()
    await ask(w)
    async with browser(w) as client:
        page = (await client.get(PAGE)).text
    for tag, attrs in parsed(page).elements:
        assert tag != "style"
        assert not [k for k in attrs if k.startswith("on")], tag
        assert "style" not in attrs, tag
        for key in ("src", "href", "action"):
            value = attrs.get(key, "")
            assert not re.match(r"(?i)\s*(javascript:|data:|https?:|//)", value), value
    assert "<script>" not in page
    assert sorted(a["src"] for a in parsed(page).where("script")) == [
        "/static/admin_mappings.js",
        "/static/ask.js",
        "/static/vendor/htmx/htmx.min.js",
    ]


def test_the_pages_script_never_writes_html_and_asks_the_one_route() -> None:
    script = (STATIC_DIR / "admin_mappings.js").read_text()
    assert_script_writes_no_html(script)
    assert "/v1/policy-mappings/proposals" in script and "textContent" in script
    assert "credentials" in script and "same-origin" in script
    assert "approve" not in script.lower() and "reject" not in script.lower()


async def test_the_script_is_served_beside_the_page() -> None:
    w = await world()
    async with browser(w) as client:
        response = await client.get("/static/admin_mappings.js")
    assert response.status_code == 200
    assert response.text == (STATIC_DIR / "admin_mappings.js").read_text()


async def test_the_page_reads_every_versions_run_and_title_in_one_query_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six versions are in force: the page asks the ledger for their runs once, and for their
    titles once, never once per version."""
    w = await world()
    await ask(w)
    reads: list[str] = []
    real_runs, real_titles = w.ledger.mapping_runs, w.ledger.version_titles

    async def runs(*args: Any, **kwargs: Any) -> Any:  # Any: passed through
        reads.append("mapping_runs")
        return await real_runs(*args, **kwargs)

    async def titles(*args: Any, **kwargs: Any) -> Any:  # Any: passed through
        reads.append("version_titles")
        return await real_titles(*args, **kwargs)

    async def one_at_a_time(*_: Any, **__: Any) -> Any:  # Any: never called
        raise AssertionError("the page read a version's run on its own")

    monkeypatch.setattr(w.ledger, "mapping_runs", runs)
    monkeypatch.setattr(w.ledger, "version_titles", titles)
    monkeypatch.setattr(w.ledger, "mapping_run", one_at_a_time)
    async with browser(w) as client:
        response = await client.get(PAGE)
    assert response.status_code == 200 and len(parsed(response.text).where("section")) == 6
    assert reads.count("mapping_runs") == 1 and reads.count("version_titles") == 1


def test_one_rule_says_what_is_running_and_what_did_not_finish() -> None:
    """The page and the API both ask the reservation, so they cannot disagree."""
    from compliance_rag.adapters.interfaces import Reservation

    assert Reservation("pending", lapsed=False).running
    assert not Reservation("pending", lapsed=True).running  # its holder died: not running
    assert Reservation("pending", lapsed=True).unfinished
    assert (
        Reservation("failed", lapsed=False).unfinished and not Reservation("failed", False).running
    )
    assert not Reservation("done", lapsed=False).running
    assert not Reservation("done", lapsed=True).unfinished
    assert not Reservation("pending", lapsed=False).unfinished
