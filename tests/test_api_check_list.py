"""The list of checks (#355, ADR-0053 I1, ADR-0059 Q5): `GET /checks`, and the
sidebar's Checks link with the reader's five latest under it.

Everything is real but the stores, which are the fakes (`test_api_checks.py`'s
world, through `test_api_pages.client_for`). The members are `lead@acme.example`
(an admin, uid `uid-123`), `second@acme.example` (uid `uid-second`, not an admin)
and a third reader, who signs in as the same listed email under another uid
(`uid-third`): visibility is by uid (ADR-0036), so they are two members. Each is
signed in on the one client in turn, so each request is one reader's, by the
session cookie.

The ledger's own contract (visibility, order, stage, paging, the one read) is
`tests/contracts/test_ledger.py`'s; what is pinned here is what the page and the
sidebar do with it.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.fakes import FakeGenerator, FakeLedgerStore
from compliance_rag.adapters.interfaces import (
    CheckCursor,
    CheckRecord,
    CheckStage,
    Reservation,
    check_stage_of,
)
from compliance_rag.api.check_list import PAGE_SIZE, parse_cursor
from compliance_rag.api.pages import SIDEBAR_CHECKS, nav_sections
from compliance_rag.corpus.models import DocumentClass
from compliance_rag.corpus.submitted_parser import SubmittedFormat
from tests.api_support import MEMBER, SECOND, bearer, mint
from tests.contracts.test_ledger import a_findings_run, an_extraction
from tests.test_api_answers import _ledger as acme_ledger  # pyright: ignore[reportPrivateUsage]
from tests.test_api_pages import client_for, page_settings, scan, text_of
from tests.test_api_shell import links_of, sidebar_of

ADMIN_UID, SECOND_UID, THIRD_UID = "uid-123", "uid-second", "uid-third"
JUN = dt.date(2026, 6, 1)
SECRET_WORDS = "Acme will keep customer addresses for five years."
PROPOSAL = f"# Customer list proposal\n\n{SECRET_WORDS}\n"


async def world(ledger: FakeLedgerStore | None = None) -> httpx.AsyncClient:
    return await client_for(
        FakeGenerator("unused"),
        ledger=ledger or await acme_ledger(),
        settings=page_settings(),
    )


async def sign_in(client: httpx.AsyncClient, uid: str, email: str) -> None:
    response = await client.post("/session", headers=bearer(mint(sub=uid, email=email)))
    assert response.status_code == 200


async def submit(client: httpx.AsyncClient, title: str | None = None) -> str:
    body = {"filename": "proposal.md", "content": PROPOSAL, **({"title": title} if title else {})}
    response = await client.post("/v1/checks", json=body)
    assert response.status_code == 201, response.text
    return str(response.json()["check_id"])


def rows_of(page: str) -> list[tuple[str, str]]:
    """(href, title) of every check in the list's body, not the sidebar's."""
    body = page.split("</nav>", 1)[1]
    return [
        (href, text_of(inner).split(" submitted ")[0])
        for href, inner in re.findall(
            r'<li>\s*<a href="(/checks/[0-9a-f]{32})">(.*?)</a>\s*</li>', body, re.S
        )
    ]


async def record_directly(
    ledger: FakeLedgerStore, number: int, uid: str, title: str | None = None
) -> str:
    """A check recorded in the ledger without the API: for what needs many."""
    check_id = uuid.UUID(int=number, version=4).hex
    instrument_id = f"acme/check-{check_id}"
    await ledger.record_version(
        tenant_id="acme",
        instrument_id=instrument_id,
        version_id="v1",
        valid_from=JUN,
        supersedes=None,
        document_class=DocumentClass.SUBMITTED_DOCUMENT,
        title=title or f"Document {number:03d}",
    )
    await ledger.record_check(
        CheckRecord(
            tenant_id="acme",
            check_id=check_id,
            user_id=uid,
            instrument_id=instrument_id,
            version_id="v1",
            filename=f"doc-{number}.md",
            document_format=SubmittedFormat.MARKDOWN,
            trace_id="a" * 32,
        )
    )
    return check_id


# ---- visibility (ADR-0059 Q5) -----------------------------------------------------------------


async def test_a_member_sees_only_their_own_checks_and_an_admin_sees_the_tenants() -> None:
    async with await world() as client:
        await sign_in(client, SECOND_UID, SECOND)
        mine_one, mine_two = (
            await submit(client, "Second's first"),
            await submit(client, "Second's"),
        )
        await sign_in(client, THIRD_UID, SECOND)
        thirds = await submit(client, "Third's")

        member = await client.get("/checks")
        assert member.status_code == 200
        assert [h for h, _ in rows_of(member.text)] == [f"/checks/{thirds}"]
        assert "Second's" not in text_of(member.text.split("</nav>", 1)[1])
        assert (await client.get(f"/checks/{mine_one}")).status_code == 404  # not theirs

        await sign_in(client, SECOND_UID, SECOND)
        second = await client.get("/checks")
        assert [h for h, _ in rows_of(second.text)] == [
            f"/checks/{mine_two}",
            f"/checks/{mine_one}",
        ]
        for href, _ in rows_of(second.text):
            assert (await client.get(href)).status_code == 200
        # a check outside visibility never appears, and its link is the same 404 as no check
        assert (await client.get(f"/checks/{thirds}")).status_code == 404
        assert thirds not in second.text

        await sign_in(client, ADMIN_UID, MEMBER)
        admin = await client.get("/checks")
        assert [h for h, _ in rows_of(admin.text)] == [
            f"/checks/{thirds}",
            f"/checks/{mine_two}",
            f"/checks/{mine_one}",
        ]
        for href, _ in rows_of(admin.text):
            assert (await client.get(href)).status_code == 200
        assert "another member" in text_of(admin.text)


async def test_the_list_shows_the_title_or_the_filename_and_when_it_was_submitted() -> None:
    async with await world() as client:
        await sign_in(client, SECOND_UID, SECOND)
        await submit(client)  # no title: the file's name
        await submit(client, "A chosen title")
        page = (await client.get("/checks")).text
    assert [t for _, t in rows_of(page)] == ["A chosen title", "proposal.md"]
    assert len(scan(page).where("time")) == 2
    assert scan(page).where("a", href="/checks/new")


async def test_the_list_is_metadata_only_no_document_words() -> None:
    async with await world() as client:
        await sign_in(client, SECOND_UID, SECOND)
        await submit(client, "Customer list")
        page = (await client.get("/checks")).text
    assert SECRET_WORDS not in page and "customer addresses" not in page


async def test_a_title_is_text_in_the_list_and_in_the_sidebar() -> None:
    markup = "<script>alert(1)</script><b>bold</b>"
    async with await world() as client:
        await sign_in(client, SECOND_UID, SECOND)
        await submit(client, markup)
        page = (await client.get("/checks")).text
    assert markup not in page and "&lt;script&gt;" in page
    assert page.count("&lt;script&gt;") >= 2  # the row, and the sidebar's link
    assert not [a for t, a in scan(page).elements if t == "b"]


async def test_no_checks_yet_is_said_and_links_to_the_form() -> None:
    async with await world() as client:
        await sign_in(client, SECOND_UID, SECOND)
        page = await client.get("/checks")
    assert page.status_code == 200 and "No checks yet" in text_of(page.text)
    assert "/checks/new" in links_of(page.text)


async def test_a_signed_out_reader_is_sent_to_sign_in() -> None:
    async with await world() as client:
        response = await client.get("/checks")
    assert response.status_code == 303 and response.headers["location"] == "/signin"


# ---- the stage is the ledger's ------------------------------------------------------------------


async def test_the_stage_shown_is_what_the_ledger_holds() -> None:
    ledger = await acme_ledger()
    async with await world(ledger) as client:
        await sign_in(client, SECOND_UID, SECOND)
        bare, claimed, found, running, failed = [
            await submit(client, name) for name in ("Bare", "Claimed", "Found", "Running", "Failed")
        ]
        for check_id in (claimed, found):
            await ledger.reserve_extraction("acme", check_id, as_of=JUN, trace_id="a" * 32)
            await ledger.record_claims(an_extraction(check_id=check_id))
        await ledger.reserve_findings("acme", found, trace_id="a" * 32)
        await ledger.record_findings(a_findings_run(found))
        await ledger.reserve_extraction("acme", running, as_of=JUN, trace_id="a" * 32)
        await ledger.reserve_extraction("acme", failed, as_of=JUN, trace_id="a" * 32)
        await ledger.fail_extraction("acme", failed)
        page = (await client.get("/checks")).text
    stages: dict[str, str] = {}
    for href, inner in re.findall(
        r'<li>\s*<a href="(/checks/[0-9a-f]{32})">(.*?)</a>\s*</li>',
        page.split("</nav>", 1)[1],
        re.S,
    ):
        stages[href] = " ".join(re.findall(r'<span class="pill [^"]*">(.*?)</span>', inner))
    assert stages == {
        f"/checks/{bare}": "submitted",
        f"/checks/{claimed}": "claims",
        f"/checks/{found}": "findings",
        f"/checks/{running}": "submitted running",
        f"/checks/{failed}": "submitted failed",
    }


# ---- paging -------------------------------------------------------------------------------------


async def test_the_list_pages_past_fifty_newest_first_without_a_gap_or_a_repeat() -> None:
    ledger = await acme_ledger()
    ids = [await record_directly(ledger, n, SECOND_UID) for n in range(1, PAGE_SIZE + 8)]
    async with await world(ledger) as client:
        await sign_in(client, SECOND_UID, SECOND)
        first = (await client.get("/checks")).text
        first_rows = [h for h, _ in rows_of(first)]
        assert first_rows == [f"/checks/{i}" for i in reversed(ids)][:PAGE_SIZE]
        assert "Newest" not in text_of(first)
        (older,) = scan(first).where("a", rel="next")
        second = await client.get(older["href"])
        assert second.status_code == 200
        second_rows = [h for h, _ in rows_of(second.text)]
        assert second_rows == [f"/checks/{i}" for i in reversed(ids)][PAGE_SIZE:]
        assert not scan(second.text).where("a", rel="next")  # the end
        assert "/checks" in links_of(second.text)  # back to the newest
        assert "Newest" in text_of(second.text)
        # past the end: the oldest check's own cursor has nothing older
        oldest = await ledger.get_check("acme", ids[0])
        assert oldest is not None and oldest.created_at is not None
        beyond = await client.get(
            "/checks", params={"before": f"{oldest.created_at.isoformat()}_{ids[0]}"}
        )
        assert beyond.status_code == 200 and not rows_of(beyond.text)
        assert "No older checks" in text_of(beyond.text)


async def test_exactly_a_page_has_no_older_link() -> None:
    ledger = await acme_ledger()
    for n in range(1, PAGE_SIZE + 1):
        await record_directly(ledger, n, SECOND_UID)
    async with await world(ledger) as client:
        await sign_in(client, SECOND_UID, SECOND)
        page = (await client.get("/checks")).text
    assert len(rows_of(page)) == PAGE_SIZE and not scan(page).where("a", rel="next")


@pytest.mark.parametrize(
    "junk", ["", "x", "_", "2026-06-01T00:00:00_x", "2026-06-01T00:00:00+00:00_" + "g" * 32, "';--"]
)
async def test_a_cursor_that_is_not_one_is_the_first_page(junk: str) -> None:
    ledger = await acme_ledger()
    check = await record_directly(ledger, 1, SECOND_UID)
    async with await world(ledger) as client:
        await sign_in(client, SECOND_UID, SECOND)
        page = await client.get("/checks", params={"before": junk})
    assert page.status_code == 200 and [h for h, _ in rows_of(page.text)] == [f"/checks/{check}"]


def test_a_cursor_round_trips_and_a_naive_time_is_refused() -> None:
    stamp = dt.datetime(2026, 11, 22, 1, 2, 3, 456789, tzinfo=dt.UTC)
    check_id = uuid.UUID(int=1, version=4).hex
    assert parse_cursor(f"{stamp.isoformat()}_{check_id}") == CheckCursor(stamp, check_id)
    assert parse_cursor(f"2026-11-22T01:02:03_{check_id}") is None
    assert parse_cursor(None) is None


# ---- one read ----------------------------------------------------------------------------------


def spy_on(ledger: FakeLedgerStore, *names: str) -> list[str]:
    """Record each call to these ledger reads, in order, and let it through."""
    calls: list[str] = []
    for name in names:
        original = getattr(ledger, name)

        async def wrapper(
            *args: object, _o: object = original, _n: str = name, **kw: object
        ) -> object:
            calls.append(_n)
            return await _o(*args, **kw)  # type: ignore[operator]  # pyright: ignore[reportCallIssue]

        setattr(ledger, name, wrapper)
    return calls


async def test_the_list_and_the_sidebar_are_one_ledger_read_whatever_the_length() -> None:
    ledger = await acme_ledger()
    for n in range(1, 13):
        await record_directly(ledger, n, SECOND_UID)
    reads = spy_on(ledger, "list_checks", "get_check", "version_titles", "get_claims")
    async with await world(ledger) as client:
        await sign_in(client, SECOND_UID, SECOND)
        reads.clear()
        page = await client.get("/checks")
    assert page.status_code == 200 and len(rows_of(page.text)) == 12
    assert reads == ["list_checks"]  # the sidebar's five are the list's own first


# ---- the sidebar ------------------------------------------------------------------------------


async def test_the_sidebar_links_checks_and_the_five_latest_under_it() -> None:
    ledger = await acme_ledger()
    ids = [await record_directly(ledger, n, SECOND_UID, f"Doc {n}") for n in range(1, 9)]
    async with await world(ledger) as client:
        await sign_in(client, SECOND_UID, SECOND)
        for path in ("/chats", "/checks", "/checks/new", "/chats/new"):
            page = (await client.get(path)).text
            bar = sidebar_of(page)
            assert "/checks" in links_of(bar), path
            latest = [h for h in links_of(bar) if re.fullmatch(r"/checks/[0-9a-f]{32}", h)]
            assert latest == [f"/checks/{i}" for i in reversed(ids)][:SIDEBAR_CHECKS], path
            assert text_of(bar).index("Check a document") < text_of(bar).index("Doc 8")
        bar = sidebar_of((await client.get("/chats")).text)
        (checks_item,) = re.findall(
            r'<li><a href="/checks"[^>]*>Checks</a>(.*?)</li>\s*</ul>', bar, re.S
        )
        assert checks_item.count("<li>") == SIDEBAR_CHECKS


async def test_the_sidebar_shows_an_admin_the_tenants_latest_and_a_member_their_own() -> None:
    ledger = await acme_ledger()
    theirs = await record_directly(ledger, 1, THIRD_UID, "Third's")
    mine = await record_directly(ledger, 2, SECOND_UID, "Second's")
    async with await world(ledger) as client:
        await sign_in(client, SECOND_UID, SECOND)
        member = links_of(sidebar_of((await client.get("/chats")).text))
        await sign_in(client, ADMIN_UID, MEMBER)
        admin = links_of(sidebar_of((await client.get("/chats")).text))
    assert f"/checks/{mine}" in member and f"/checks/{theirs}" not in member
    assert f"/checks/{mine}" in admin and f"/checks/{theirs}" in admin


async def test_the_checks_link_is_current_on_the_list_and_not_on_the_form() -> None:
    ledger = await acme_ledger()
    mine = await record_directly(ledger, 1, SECOND_UID, "Mine")
    async with await world(ledger) as client:
        await sign_in(client, SECOND_UID, SECOND)
        marked: dict[str, list[str]] = {}
        for path in ("/checks", "/checks/new", f"/checks/{mine}", "/chats"):
            bar = scan(sidebar_of((await client.get(path)).text))
            marked[path] = [a["href"] for a in bar.where("a", **{"aria-current": "page"})]
    assert marked == {
        "/checks": ["/checks"],
        "/checks/new": ["/checks/new"],
        f"/checks/{mine}": [f"/checks/{mine}"],  # the check itself, not Checks above it
        "/chats": ["/chats"],
    }


def test_nav_marks_checks_only_on_the_list_and_on_a_check_not_in_the_latest() -> None:
    def current(path: str) -> list[str]:
        return [i.href for s in nav_sections(path, admin=False) for i in s.items if i.active]

    assert current("/checks") == ["/checks"]
    assert current("/checks/new") == ["/checks/new"]
    assert current(f"/checks/{uuid.UUID(int=1, version=4).hex}") == ["/checks"]


async def test_a_ledger_that_cannot_be_reached_leaves_the_sidebar_bare_not_the_page_down(
    span_exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    ledger = await acme_ledger()
    detail = "db-host-7.internal:5432"

    async def unreachable(*args: object, **kwargs: object) -> object:
        raise ConnectionError(f"the ledger at {detail} can't be reached")

    ledger.list_checks = unreachable  # type: ignore[method-assign]  # pyright: ignore
    async with await world(ledger) as client:
        await sign_in(client, SECOND_UID, SECOND)
        span_exporter.clear()
        with caplog.at_level("WARNING", logger="compliance_rag.api.pages"):
            response = await client.get("/chats/new")
    assert response.status_code == 200
    assert "/checks" in links_of(sidebar_of(response.text))
    assert not [h for h in links_of(response.text) if re.fullmatch(r"/checks/[0-9a-f]{32}", h)]
    # the outage is not a healthy-looking span, and neither record carries the detail
    (span,) = (s for s in span_exporter.get_finished_spans() if s.name == "api.check.recent")
    assert span.attributes is not None and span.attributes["check.recent_unavailable"] is True
    assert [e.name for e in span.events] == ["exception"]
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "unreachable" in warnings[0].getMessage()
    assert detail not in warnings[0].getMessage()
    assert detail not in repr([dict(e.attributes or {}) for e in span.events])


def test_one_rule_says_which_stage_a_check_is_at() -> None:
    def held(status: str) -> Reservation:
        return Reservation(status, lapsed=False)

    for extraction, findings, stage in (
        (None, None, CheckStage.SUBMITTED),
        (held("pending"), None, CheckStage.SUBMITTED),
        (held("failed"), None, CheckStage.SUBMITTED),
        (held("done"), None, CheckStage.CLAIMS),
        (held("done"), held("pending"), CheckStage.CLAIMS),
        (held("done"), held("failed"), CheckStage.CLAIMS),
        (held("done"), held("done"), CheckStage.FINDINGS),
    ):
        assert check_stage_of(extraction, findings) is stage, (extraction, findings)


# ---- the shell --------------------------------------------------------------------------------


async def test_the_list_is_in_the_shell_with_no_inline_script_style_or_handler() -> None:
    ledger = await acme_ledger()
    await record_directly(ledger, 1, SECOND_UID)
    async with await world(ledger) as client:
        await sign_in(client, SECOND_UID, SECOND)
        for path in ("/checks", "/checks?before=junk"):
            response = await client.get(path)
            page = response.text
            parsed = scan(page)
            assert parsed.where("nav", **{"class": "sidebar"}), path
            assert len(parsed.where("aside", id="sources")) == 1, path
            assert not parsed.script_text.strip() and not parsed.style_text.strip(), path
            assert not [a for _, a in parsed.elements if "style" in a], path
            assert not [k for _, a in parsed.elements for k in a if k.startswith("on")], path
            assert "no-store" in response.headers["cache-control"], path
            assert "content-security-policy" in response.headers, path
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    assert title is not None and title.group(1).strip() == "Checks - Contextum"
