"""The Contextum shell (issue #353, ADR-0053 I1 and I6): every production page
draws inside one layout, from `base.html` and `app.css`.

What is pinned here is the shell, not each page's content (`test_api_pages.py`,
`test_api_checks.py` and the admin tests own that):

- the sidebar, and who sees what in it (a member, an admin, a signed-out
  reader's error pages);
- the one `<aside id="sources">` slot, empty until a page fills it;
- the logo and favicon served from `/static`, "Contextum" for the brand;
- the status pill and the `[n]` markers on a chat answer, and that neither
  alters the verified text;
- no inline script, style or handler on any page, and the CSP unchanged.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

from compliance_rag.adapters.fakes import FakeGenerator
from compliance_rag.api.sign_in import CONTENT_SECURITY_POLICY, STATIC_DIR
from compliance_rag.eval.golden_corpus import golden_clauses
from tests.api_support import MEMBER, SECOND, bearer, mint
from tests.test_api_answers import NOT_RETRIEVED, SECRET_QUOTE
from tests.test_api_answers import _reply as reply_of  # pyright: ignore[reportPrivateUsage]
from tests.test_api_pages import APP11, client_for, outcomes, scan, text_of
from tests.test_api_pages import ask as ask_in_chat

ADMIN_LINKS = ("/admin/subscriptions", "/admin/questionnaire")
MEMBER_LINKS = ("/chats/new", "/chats", "/checks/new")
SUBMITTED = (
    "# Customer list proposal\n\nAcme will keep customer addresses for five years.\n\n"
    "Acme will review the retention period each year.\n"
)


@pytest.fixture
def quote() -> str:
    """A quote the model can cite verbatim: the start of APP 11.1's clause."""
    clause = next(c for c in golden_clauses() if c.clause_id == APP11)
    return clause.canonical_text[:60]


async def signed_in(client: httpx.AsyncClient, email: str) -> httpx.AsyncClient:
    response = await client.post("/session", headers=bearer(mint(email=email)))
    assert response.status_code == 200
    return client


async def a_check(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/v1/checks", json={"filename": "proposal.md", "content": SUBMITTED}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["check_id"])


def links_of(page: str) -> list[str]:
    return [a["href"] for a in scan(page).where("a") if "href" in a]


def sidebar_of(page: str) -> str:
    """The sidebar's own markup: from its `<nav>` to its end."""
    match = re.search(r'<nav class="sidebar".*?</nav>', page, re.S)
    assert match is not None, "no sidebar"
    return match.group(0)


async def every_page(client: httpx.AsyncClient) -> dict[str, httpx.Response]:
    """Each page a member can reach, signed in as the caller."""
    events, _ = await ask_in_chat(client)
    chat_id = events[0][1]["chat_id"]
    check_id = await a_check(client)
    return {
        "/chats": await client.get("/chats"),
        "/chats/new": await client.get("/chats/new"),
        f"/chats/{chat_id}": await client.get(f"/chats/{chat_id}"),
        "/checks/new": await client.get("/checks/new"),
        f"/checks/{check_id}": await client.get(f"/checks/{check_id}"),
        "a 404 page": await client.get(f"/chats/{uuid.uuid4().hex}"),
    }


# ---- a member's shell ------------------------------------------------------------------------


async def test_a_member_sees_the_shell_on_every_page_and_no_admin_link(quote: str) -> None:
    async with await client_for(FakeGenerator(reply_of((APP11, quote)))) as client:
        await signed_in(client, SECOND)
        pages = await every_page(client)
        pages["/admin/subscriptions"] = await client.get("/admin/subscriptions")
        pages["/admin/questionnaire"] = await client.get("/admin/questionnaire")
    # an admin page is refused to a member, as before (403), and says so in the shell
    assert pages["/admin/subscriptions"].status_code == 403
    assert pages["/admin/questionnaire"].status_code == 403
    for name, response in pages.items():
        page = response.text
        bar = sidebar_of(page)
        for href in MEMBER_LINKS:
            assert href in links_of(bar), (name, href)
        assert not [h for h in links_of(page) if h.startswith("/admin")], name
        assert "Admin" not in text_of(bar), name
        assert "/checks" not in links_of(bar), name  # no checks list yet: no placeholder
        assert text_of(bar).count("Sign out") == 1, name
        assert scan(bar).where("button", **{"data-sign-out": ""}), name
        assert SECOND in text_of(bar), name  # the member's own email, bottom of the bar
        (avatar,) = scan(bar).where("span", **{"class": "avatar"})
        assert avatar
        assert "Contextum" in text_of(bar), name
        assert "Compliance Ask" not in page, name


async def test_the_sidebar_has_ask_and_check_sections_with_real_links_only(quote: str) -> None:
    async with await client_for(FakeGenerator(reply_of((APP11, quote)))) as client:
        await signed_in(client, SECOND)
        page = (await client.get("/chats")).text
    bar = sidebar_of(page)
    headings = [text_of(h) for h in re.findall(r'<p class="nav-heading">(.*?)</p>', bar, re.S)]
    assert headings == ["Ask", "Check"]
    # every item is a real link to a page that exists (and none is a placeholder)
    items = re.findall(r"<li>(.*?)</li>", bar, re.S)
    assert len(items) == 3 and all("<a " in i for i in items)
    assert [text_of(i) for i in items] == ["New chat", "Chats", "Check a document"]


async def test_the_current_page_is_marked_in_the_sidebar(quote: str) -> None:
    async with await client_for(FakeGenerator(reply_of((APP11, quote)))) as client:
        await signed_in(client, SECOND)
        events, _ = await ask_in_chat(client)
        chat = f"/chats/{events[0][1]['chat_id']}"
        marked = {}
        for path in ("/chats", "/chats/new", chat, "/checks/new"):
            bar = scan(sidebar_of((await client.get(path)).text))
            marked[path] = [a["href"] for a in bar.where("a", **{"aria-current": "page"})]
    assert marked == {
        "/chats": ["/chats"],
        "/chats/new": ["/chats/new"],
        chat: ["/chats"],
        "/checks/new": ["/checks/new"],
    }


# ---- an admin's ------------------------------------------------------------------------------


async def test_an_admin_sees_the_admin_section_on_every_page(quote: str) -> None:
    async with await client_for(FakeGenerator(reply_of((APP11, quote)))) as client:
        await signed_in(client, MEMBER)
        pages = await every_page(client)
        pages["/admin/subscriptions"] = await client.get("/admin/subscriptions")
        pages["/admin/questionnaire"] = await client.get("/admin/questionnaire")
    assert pages["/admin/subscriptions"].status_code == 200
    assert pages["/admin/questionnaire"].status_code == 200
    for name, response in pages.items():
        bar = sidebar_of(response.text)
        for href in (*MEMBER_LINKS, *ADMIN_LINKS):
            assert href in links_of(bar), (name, href)
        headings = [text_of(h) for h in re.findall(r'<p class="nav-heading">(.*?)</p>', bar, re.S)]
        assert headings == ["Ask", "Check", "Admin"], name
        assert MEMBER in text_of(bar), name
        assert "Compliance Ask" not in response.text, name
    here = scan(sidebar_of(pages["/admin/questionnaire"].text)).where(
        "a", **{"aria-current": "page"}
    )
    assert [a["href"] for a in here] == ["/admin/questionnaire"]


# ---- the brand: the name, the logo, the favicon -------------------------------------------------


async def test_the_pages_say_contextum_and_carry_the_logo_and_favicon(quote: str) -> None:
    async with await client_for(FakeGenerator(reply_of((APP11, quote)))) as client:
        await signed_in(client, SECOND)
        pages = await every_page(client)
        pages["/signin"] = await client.get("/signin")
        logo = await client.get("/static/contextum-logo.png")
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/png"
    assert logo.content.startswith(b"\x89PNG")
    for name, response in pages.items():
        parsed = scan(response.text)
        (icon,) = parsed.where("link", rel="icon")
        assert icon["href"] == "/static/contextum-logo.png", name
        title = re.search(r"<title>(.*?)</title>", response.text, re.S)
        assert title is not None and title.group(1).strip().endswith(" - Contextum"), name
        # the logo is the stylesheet's, drawn on the brand (no page holds an <img>)
        assert parsed.where("a", **{"class": "brand"}) or name == "/signin", name
        assert "Contextum" in text_of(response.text), name
    title = re.search(r"<title>(.*?)</title>", pages["/chats"].text, re.S)
    assert title is not None and title.group(1).strip() == "Your chats - Contextum"


async def test_the_sign_in_page_shows_the_logo_and_the_name_in_the_same_palette() -> None:
    async with await client_for(FakeGenerator()) as client:
        page = (await client.get("/signin")).text
    parsed = scan(page)
    assert parsed.where("div", **{"class": "signin-brand"}) and not parsed.where("img")
    assert "Contextum" in text_of(page) and "Compliance Ask" not in page
    # the sign-in form is as it was: the script finds its parts by these ids
    assert parsed.where("form", id="signin-form") and parsed.where("button", id="google-sign-in")
    assert parsed.where("p", id="signin-status") and parsed.where("main", id="signin")
    # one palette: the sign-in page takes the shell's stylesheet, then its own
    assert [a["href"] for a in parsed.where("link", rel="stylesheet")] == [
        "/static/app.css",
        "/static/signin.css",
        "/static/banner.css",
    ]


# ---- the Sources slot ----------------------------------------------------------------


async def test_every_page_has_the_one_sources_slot_and_only_the_chat_fills_it(quote: str) -> None:
    async with await client_for(FakeGenerator(reply_of((APP11, quote)))) as client:
        await signed_in(client, SECOND)
        pages = await every_page(client)
    chat_pages = {"/chats/new"} | {n for n in pages if re.fullmatch(r"/chats/[0-9a-f]{32}", n)}
    for name, response in pages.items():
        parsed = scan(response.text)
        assert len(parsed.where("aside", id="sources")) == 1, name
        (slot,) = re.findall(
            r'<aside id="sources"[^>]*>(.*?)</aside>\s*</div>', response.text, re.S
        ) or [""]
        assert ('id="panel"' in slot) == (name in chat_pages), name
    # the chat's citation area keeps its behaviour, inside the slot
    chat = next(r.text for n, r in pages.items() if n.startswith("/chats/") and n != "/chats/new")
    inside = re.search(r'<aside id="sources".*?</aside>\s*</div>', chat, re.S)
    assert inside is not None
    slot = scan(inside.group(0))
    (frame,) = slot.where("iframe")
    assert frame["sandbox"] == "" and "src" not in frame
    assert slot.where("button", id="panel-close")


# ---- no inline anything; the CSP as it was -------------------------------------------------------


def test_the_csp_is_unchanged() -> None:
    assert CONTENT_SECURITY_POLICY == (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "frame-src 'self'; connect-src 'self' https://identitytoolkit.googleapis.com "
        "https://securetoken.googleapis.com; form-action 'self'; base-uri 'none'; "
        "frame-ancestors 'none'"
    )


async def test_the_shell_has_no_inline_script_style_or_handler_and_the_htmx_config_is_as_it_was(
    quote: str,
) -> None:
    async with await client_for(FakeGenerator(reply_of((APP11, quote)))) as client:
        await signed_in(client, MEMBER)
        pages = await every_page(client)
        pages["/admin/subscriptions"] = await client.get("/admin/subscriptions")
        pages["/admin/questionnaire"] = await client.get("/admin/questionnaire")
    for name, response in pages.items():
        assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY, name
        parsed = scan(response.text)
        assert not parsed.script_text.strip() and not parsed.style_text.strip(), name
        for tag, attrs in parsed.elements:
            assert tag != "style", name
            assert "style" not in attrs, (name, tag)
            assert not [k for k in attrs if k.startswith("on")], (name, tag)
            if tag == "script":
                assert attrs["src"].startswith("/static/"), name
            for key in ("src", "href"):
                assert not re.match(r"(?i)\s*(javascript:|data:|https?:|//)", attrs.get(key, ""))
        (config,) = parsed.where("meta", name="htmx-config")
        assert json.loads(config["content"]) == {
            "includeIndicatorStyles": False,
            "allowEval": False,
            "allowScriptTags": False,
            "selfRequestsOnly": True,
            "historyEnabled": False,
        }, name


async def test_a_members_email_is_escaped_in_the_sidebar(quote: str) -> None:
    async with await client_for(FakeGenerator(reply_of((APP11, quote)))) as client:
        await signed_in(client, SECOND)
        page = (await client.get("/chats")).text
    assert html.escape(SECOND) in sidebar_of(page)


# ---- the answer: status pill and [n] markers -----------------------------------------


def answer_text_of(page: str) -> str:
    """What is in the `.answer-text` element, entities decoded, as a reader gets it."""
    match = re.search(r'<p class="answer-text">(.*?)</p>', page, re.S)
    assert match is not None, "no answer text"
    return html.unescape(match.group(0)[len('<p class="answer-text">') : -len("</p>")])


async def test_a_verified_answer_has_a_green_pill_outside_the_answer_text(quote: str) -> None:
    async with await client_for(FakeGenerator(reply_of((APP11, quote)))) as client:
        await signed_in(client, SECOND)
        events, fragment = await ask_in_chat(client)
        (answer,) = [d for n, d in events if n == "answer"]
    assert answer["verified"] is True
    assert outcomes(fragment) == ["verified"]
    (pill,) = scan(fragment).where("p", **{"data-pill": "verified"})
    assert "pill-ok" in pill["class"]
    n = len(answer["citations"])
    assert n >= 1
    assert f"✓ Verified — {n} of {n} citations checked" in text_of(fragment)
    # the pill is not inside the answer's text, and the text is the API's, whole
    assert answer_text_of(fragment) == answer["text"]


async def test_a_withheld_answer_has_an_amber_pill_with_its_reason() -> None:
    prose = f'Section 6C(2) says "{SECRET_QUOTE}".'
    generator = FakeGenerator(reply_of((NOT_RETRIEVED, SECRET_QUOTE), answer=prose))
    async with await client_for(generator) as client:
        await signed_in(client, SECOND)
        events, fragment = await ask_in_chat(client)
        (answer,) = [d for n, d in events if n == "answer"]
    assert answer["verified"] is False
    (pill,) = scan(fragment).where("p", **{"data-pill": "withheld"})
    assert "pill-warn" in pill["class"]
    assert f"Withheld — {answer['withheld']}" in text_of(fragment)
    assert "✓" not in text_of(fragment)
    assert SECRET_QUOTE not in fragment  # nothing the model wrote


async def test_each_citation_in_a_verified_answer_carries_its_number(quote: str) -> None:
    async with await client_for(FakeGenerator(reply_of((APP11, quote)))) as client:
        await signed_in(client, SECOND)
        _, fragment = await ask_in_chat(client)
    items = re.findall(r'<li class="citation"[^>]*>(.*?)</li>', fragment, re.S)
    assert items
    for i, item in enumerate(items, start=1):
        assert text_of(item).startswith(f"[{i}]")


NODE = shutil.which("node")
NEEDS_NODE = pytest.mark.skipif(
    NODE is None and not os.environ.get("CI"),  # in CI a missing node fails, it does not skip
    reason="node is not installed",
)
MARKERS = STATIC_DIR / "markers.js"


def run_markers(text: str, cites: list[str | None]) -> list[dict[str, object]]:
    """`markers.js`'s one pure function, run under Node."""
    assert NODE is not None
    script = (
        f"import {{ segments }} from {json.dumps(MARKERS.as_uri())};"
        f"console.log(JSON.stringify(segments({json.dumps(text)}, {json.dumps(cites)})));"
    )
    done = subprocess.run(
        [NODE, "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(done.stdout)


@NEEDS_NODE
@pytest.mark.parametrize(
    ("text", "cites"),
    [
        ("Acme must act (s 6D(4)(a)) and then (APP 11.1).", ["APP 11.1", "s 6D(4)(a)", None]),
        ('She said "APP 11.1" twice: APP 11.1.', ["APP 11.1"]),
        ("No cite is named here.", ["APP 11.1"]),
        ("", ["APP 11.1"]),
        ("APP 11.1 and APP 11.10 differ.", ["APP 11.1", "APP 11.10"]),
        ("Ünïcode — “quoted” APP 11.1” end", ["APP 11.1"]),
        ("<b>markup</b> APP 11.1 & more", ["APP 11.1"]),
    ],
)
def test_markers_are_placed_after_each_cite_and_the_text_is_never_altered(
    text: str, cites: list[str | None]
) -> None:
    parts = run_markers(text, cites)
    # the text, put back together from its pieces, is the text, character for character
    assert "".join(str(p["text"]) for p in parts if "text" in p) == text
    markers = [p["marker"] for p in parts if "marker" in p]
    named = [c for c in cites if c and c in text]
    assert len(markers) >= len(named) if named else markers == []
    for p in parts:
        assert ("text" in p) != ("marker" in p)
        assert "marker" not in p or isinstance(p["marker"], int)


@NEEDS_NODE
def test_the_longest_cite_wins_and_a_closing_quote_stays_before_the_marker() -> None:
    parts = run_markers('See "APP 11.10" and APP 11.1.', ["APP 11.1", "APP 11.10"])
    assert parts == [
        {"text": 'See "APP 11.10"'},
        {"marker": 2},
        {"text": " and APP 11.1"},
        {"marker": 1},
        {"text": "."},
    ]


def test_ask_js_places_markers_as_empty_elements_so_the_text_content_is_untouched() -> None:
    script = (STATIC_DIR / "ask.js").read_text()
    markers = (STATIC_DIR / "markers.js").read_text()
    assert 'from "./markers.js"' in script
    for forbidden in ("innerHTML", "insertAdjacentHTML", "outerHTML"):
        assert forbidden not in markers and forbidden not in script
    # a marker is an empty <sup> whose number the stylesheet draws: textContent is untouched
    assert "data-n" in markers or "dataset.n" in script
    css = (STATIC_DIR / "app.css").read_text()
    assert re.search(r"\.marker::(before|after)\s*\{[^}]*attr\(data-n\)", css)


# ---- the stylesheet: the palette, the one breakpoint ----------------------------------


def test_the_stylesheet_has_the_contextum_palette_and_no_gradient() -> None:
    css = (STATIC_DIR / "app.css").read_text()
    for token in ("#0F1E3D", "#16264A", "#E6E9F0", "#8A94A8", "#C9A227", "#2E9E6B", "#D08A2E"):
        assert token.lower() in css.lower(), token
    assert not re.search(r"gradient\s*\(", css) and "Inter" in css
    assert "border-radius: 8px" in css or "--radius: 8px" in css
    signin = (STATIC_DIR / "signin.css").read_text()
    assert not re.search(r"gradient\s*\(", signin) and "var(--strong)" in signin


def test_the_stylesheets_draw_the_logo_from_static_and_it_is_there() -> None:
    for sheet in ("app.css", "signin.css"):
        assert 'url("/static/contextum-logo.png")' in (STATIC_DIR / sheet).read_text(), sheet
    assert Path(STATIC_DIR / "contextum-logo.png").read_bytes().startswith(b"\x89PNG")
