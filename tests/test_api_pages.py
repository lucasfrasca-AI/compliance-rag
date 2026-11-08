"""The server-rendered Ask pages (issue #281, ADR-0053 decisions 14-17 and 19).

Driven end to end against `create_app` with fakes and the fake generator: a
member signs in (the cookie, as `test_api_session.py` does), lists chats, asks,
reads the verified answer, opens a citation, asks a follow-up in the same chat
and reads the history. The Ask script (`static/ask.js`) is a browser's; here the
requests it makes are made by the test in the order it makes them (the stream,
then the turns fragment, which takes no parameter),
so what is checked is what the server draws for each outcome.

One test per outcome state; escaping of markup in the question and the answer;
the CSP on every page response; no inline script, handler or style in any
rendered page or template.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from html.parser import HTMLParser
from typing import Any

import httpx
import pytest
from starlette.requests import Request

from compliance_rag.adapters.fakes import (
    FakeGenerator,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.interfaces import (
    ChatAnswer,
    ChatCitation,
    ChatExchange,
    Generation,
    Generator,
    Member,
    PrefixMeasure,
    TurnOutcome,
    UnreadableCitation,
    UnreadableReason,
)
from compliance_rag.api.app import create_app
from compliance_rag.api.pages import ENV, TEMPLATE_DIR, PageError, is_chat_id, page_error
from compliance_rag.api.session import COOKIE_NAME, SessionCodec
from compliance_rag.api.sign_in import (
    CONTENT_SECURITY_POLICY,
    STATIC_DIR,
    sign_in_content_security_policy,
)
from compliance_rag.assessment.environment import NotInEnvironment
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.assessment.prefix_cap import PREFIX_CAP_TOKENS
from compliance_rag.corpus.models import CANONICALISATION_VERSION, PUBLIC_TENANT_ID
from compliance_rag.eval.applicability import load_applicability_set
from compliance_rag.eval.golden_corpus import golden_chunks, golden_clauses
from compliance_rag.eval.harness import golden_clause_store, law_source_html
from compliance_rag.graph.mappings import load_mapping_set
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import Settings
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier
from tests.api_support import (
    MEMBER,
    PROJECT,
    TENANT,
    api_settings,
    bearer,
    fake_api_services,
    mint,
)
from tests.test_api_answers import (
    AS_OF,
    NOT_RETRIEVED,
    OFFSHORE,
    QUESTION,
    SECRET_QUOTE,
)
from tests.test_api_answers import _events as events_of  # pyright: ignore[reportPrivateUsage]
from tests.test_api_answers import _ledger as acme_ledger  # pyright: ignore[reportPrivateUsage]
from tests.test_api_answers import _reply as reply_of  # pyright: ignore[reportPrivateUsage]
from tests.test_api_not_in_environment import (
    MARKETING,
    SPAM_QUESTION,
)
from tests.test_api_not_in_environment import (
    _marketing_quote as marketing_quote,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_api_not_in_environment import (
    _reply as spam_reply,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_applicability_gate import CORPUS_MAPPINGS, fake_for
from tests.vertex_quota import SDK_WORDS, quota_exhausted_generator

SECRET = "s3cr3t-" + "k" * 40
ORIGIN = "https://api"
LAW = "privacy-act-1988"
LAW_VERSION = "C2026C00227"
APP11 = f"{LAW}:{LAW_VERSION}:sch1/app11/1"
S6DA = f"{LAW}:{LAW_VERSION}:s6da/2"
ANSWER = "Acme must take reasonable steps."
FOLLOW_UP = "And what about when the information is no longer needed?"
FOLLOW_UP_ANSWER = "Acme must destroy or de-identify it."
UID = "uid-123"
CHAT_ID = re.compile(r"^[0-9a-f]{32}$")
EVENT = tuple[str, dict[str, Any]]  # Any: a JSON event


# ---- the world ---------------------------------------------------------------------------


class Sequenced(FakeGenerator):
    """Replies in turn, the last one for every call after."""

    def __init__(self, *replies: str) -> None:
        super().__init__(replies[0])
        self._queue = list(replies)

    async def generate(
        self, *, system: str, cached_prefix: str | None, user: str, max_tokens: int
    ) -> Generation:
        self._reply = self._queue.pop(0) if len(self._queue) > 1 else self._queue[0]
        return await super().generate(
            system=system, cached_prefix=cached_prefix, user=user, max_tokens=max_tokens
        )


class Exploding(FakeGenerator):
    async def generate(
        self, *, system: str, cached_prefix: str | None, user: str, max_tokens: int
    ) -> Generation:
        raise RuntimeError("the generator broke: SECRET-INTERNAL-DETAIL")


def page_settings(**overrides: Any) -> Settings:  # Any: settings field values
    values: dict[str, Any] = {  # Any: settings field values
        "session_secret": SECRET,
        "firebase_web_api_key": "AIza-web-key-not-a-secret",
        **overrides,
    }
    return api_settings(**values)


async def client_for(
    generator: Generator,
    *,
    ledger: FakeLedgerStore | None = None,
    settings: Settings | None = None,
    policy_text: str | None = None,
) -> httpx.AsyncClient:
    """The app as a browser on `https://api` meets it: the golden corpus and
    the Privacy Act's stored source, Acme subscribed to it. `Origin` is what a
    browser's own `fetch` and form posts send."""
    ledger = ledger or await acme_ledger()
    settings = settings or page_settings()
    clauses = await golden_clause_store()
    await clauses.put_manifest(
        LAW,
        LAW_VERSION,
        tenant_id=PUBLIC_TENANT_ID,
        canonicalisation_version=CANONICALISATION_VERSION,
        source_html=law_source_html(),
    )
    if policy_text is not None:
        edited = next(c for c in golden_clauses() if c.instrument_id == "acme/privacy-policy")
        await clauses.put_clause(edited.model_copy(update={"canonical_text": policy_text}))
    services = fake_api_services(
        settings=settings,
        ledger=ledger,
        clauses=clauses,
        graph=fake_for(
            load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), load_applicability_set()
        ),
        pipeline=AssessmentPipeline(
            RetrievalPipeline(FakeRetrievalStore(golden_chunks(), ledger=ledger), FakeReranker()),
            generator,
            Verifier(clauses, ledger, CanonicalCitationVerifier()),
            ledger,
            tenant_text=TenantTextGate.from_settings(settings),
        ),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN},
    )


async def sign_in(client: httpx.AsyncClient, **claims: Any) -> httpx.AsyncClient:  # Any: claims
    response = await client.post("/session", headers=bearer(mint(**claims)))
    assert response.status_code == 200
    return client


@pytest.fixture
def quote() -> str:
    clause = next(c for c in golden_clauses() if c.clause_id == APP11)
    return clause.canonical_text[:60]


@pytest.fixture
async def member() -> AsyncIterator[httpx.AsyncClient]:
    """A signed-in member of a world whose model answers APP 11 correctly."""
    clause = next(c for c in golden_clauses() if c.clause_id == APP11)
    async with await client_for(FakeGenerator(reply_of((APP11, clause.canonical_text[:60])))) as c:
        yield await sign_in(c)


async def ask(
    client: httpx.AsyncClient,
    question: str = QUESTION,
    *,
    chat_id: str | None = None,
) -> tuple[list[EVENT], str]:
    """What `ask.js` does: stream `POST /v1/answers` with the cookie, then (the
    request being over) have the server draw the chat's turns from what the
    ledger recorded. Returns the events and the fragment."""
    body: dict[str, Any] = {"question": question, "as_of": AS_OF}  # Any: JSON
    if chat_id:
        body["chat_id"] = chat_id
    response = await client.post("/v1/answers", json=body)
    assert response.status_code == 200
    events = events_of(response.text)
    chat = events[0][1]["chat_id"]
    fragment = await client.get(f"/chats/{chat}/turns")
    assert fragment.status_code == 200
    return events, fragment.text


# ---- reading HTML --------------------------------------------------------------------------


class Scan(HTMLParser):
    """Every element of a page with its attributes, and what the page's script
    and style elements hold."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str]]] = []
        self.script_text = ""
        self.style_text = ""
        self._inside = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, {k: v or "" for k, v in attrs}))
        self._inside = tag

    def handle_endtag(self, tag: str) -> None:
        self._inside = ""

    def handle_data(self, data: str) -> None:
        if self._inside == "script":
            self.script_text += data
        elif self._inside == "style":
            self.style_text += data

    def where(self, tag: str, **attrs: str) -> list[dict[str, str]]:
        return [
            a for t, a in self.elements if t == tag and all(a.get(k) == v for k, v in attrs.items())
        ]


def scan(page: str) -> Scan:
    scanned = Scan()
    scanned.feed(page)
    return scanned


def outcomes(page: str) -> list[str]:
    return [a["data-outcome"] for _, a in scan(page).elements if "data-outcome" in a]


def text_of(page: str) -> str:
    """The page's words: tags gone, entities decoded, whitespace collapsed."""
    import html

    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


# ---- signing in ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/chats", "/chats/new", f"/chats/{'a' * 12}4{'b' * 3}8{'c' * 15}"]
)
async def test_a_page_without_a_session_redirects_to_sign_in(path: str) -> None:
    async with await client_for(FakeGenerator()) as client:
        response = await client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/signin"
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


async def test_the_turns_fragment_without_a_session_is_a_401_the_script_turns_into_a_sign_in() -> (
    None
):
    async with await client_for(FakeGenerator()) as client:
        response = await client.get(f"/chats/{uuid.uuid4().hex}/turns")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_an_expired_or_forged_cookie_redirects_too() -> None:
    async with await client_for(FakeGenerator()) as client:
        expired = SessionCodec(SECRET).seal(
            Member(uid=UID, email=MEMBER), expires_at=int(time.time()) - 5
        )
        forged = SessionCodec("0ther-" + "z" * 40).seal(
            Member(uid=UID, email=MEMBER), expires_at=int(time.time()) + 500
        )
        for value in (expired, forged):
            response = await client.get("/chats", headers={"Cookie": f"{COOKIE_NAME}={value}"})
            assert (response.status_code, response.headers["location"]) == (303, "/signin")


async def test_a_signed_in_non_member_gets_a_403_page_not_a_redirect_loop() -> None:
    async with await client_for(FakeGenerator()) as client:
        outsider = SessionCodec(SECRET).seal(
            Member(uid="uid-x", email="outsider@elsewhere.example"),
            expires_at=int(time.time()) + 500,
        )
        response = await client.get("/chats", headers={"Cookie": f"{COOKIE_NAME}={outsider}"})
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert "not a member" in text_of(response.text)
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


async def test_the_sign_in_page_leads_on_to_the_chats() -> None:
    async with await client_for(FakeGenerator()) as client:
        page = (await client.get("/signin")).text
    assert scan(page).where("a", href="/chats")


# ---- the whole path ----------------------------------------------------------------------------


async def test_sign_in_new_chat_ask_open_a_citation_follow_up_and_history(quote: str) -> None:
    follow_quote = quote  # the same clause, a different answer
    generator = Sequenced(
        reply_of((APP11, quote), answer=ANSWER),
        reply_of((APP11, follow_quote), answer=FOLLOW_UP_ANSWER),
    )
    async with await client_for(generator) as client:
        # 1. no chats yet, and the new chat's page is the form
        await sign_in(client)
        listed = await client.get("/chats")
        assert listed.status_code == 200 and "No chats yet" in listed.text
        assert scan(listed.text).where("a", href="/chats/new")
        fresh = scan((await client.get("/chats/new")).text)
        assert fresh.where("textarea", name="question", maxlength="2000")
        assert fresh.where("main", id="chat") == [] and fresh.where("div", id="chat")

        # 2. ask: the stream's stages, then the verified answer drawn by the server
        events, fragment = await ask(client)
        assert [n for n, _ in events] == ["coverage", "retrieval", "answer"]
        chat_id = events[0][1]["chat_id"]
        assert CHAT_ID.match(chat_id)
        assert outcomes(fragment) == ["verified"]
        assert ANSWER in text_of(fragment) and QUESTION in text_of(fragment)

        # 3. a numbered citation, opening its source at the clause
        parsed = scan(fragment)
        assert parsed.where("ol", **{"class": "citations"})
        (link,) = parsed.where("a", **{"data-source-link": ""})
        assert link["href"] == "/v1/sources/privacy-act-1988/C2026C00227#clause-sch1-app11-1"
        assert "APP 11.1" in text_of(fragment)
        path, anchor = link["href"].split("#")
        source = await client.get(path)  # a plain link, as the iframe loads it: cookie only
        assert source.status_code == 200
        assert f'id="{anchor}"' in source.text
        assert "sandbox" in source.headers["content-security-policy"]

        # 4. a follow-up in the same chat: the model is given the first exchange
        follow_events, follow_fragment = await ask(client, FOLLOW_UP, chat_id=chat_id)
        assert {d["chat_id"] for _, d in follow_events} == {chat_id}
        assert FOLLOW_UP_ANSWER in text_of(follow_fragment)
        assert QUESTION in str(generator.calls[1]["user"]) and ANSWER in str(
            generator.calls[1]["user"]
        )

        # 5. the chat page reads the whole history back, in order
        page = (await client.get(f"/chats/{chat_id}")).text
        words = text_of(page)
        assert [words.index(s) for s in (QUESTION, ANSWER, FOLLOW_UP, FOLLOW_UP_ANSWER)] == sorted(
            words.index(s) for s in (QUESTION, ANSWER, FOLLOW_UP, FOLLOW_UP_ANSWER)
        )
        assert outcomes(page) == ["verified", "verified"]
        assert len(scan(page).where("article", **{"class": "turn turn-question"})) == 2

        # 6. and the list names the chat by its first question, once
        listed_text = (await client.get("/chats")).text
        assert listed_text.count(f'href="/chats/{chat_id}"') == 1
        assert QUESTION in text_of(listed_text) and "4 turns" in text_of(listed_text)


async def test_the_chat_list_is_the_members_own_newest_first() -> None:
    ledger = await acme_ledger()
    older, newer, theirs = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    for chat_id, user, question in (  # recorded in this order, so stamped in it
        (older, UID, "older question"),
        (newer, UID, "newer question"),
        (theirs, "uid-colleague", "a colleague's question"),
    ):
        await ledger.record_chat_exchange(
            ChatExchange(
                tenant_id=TENANT,
                chat_id=chat_id,
                user_id=user,
                question=question,
                answer=ChatAnswer(TurnOutcome.INTERNAL),
                as_of=dt.date.fromisoformat(AS_OF),
                trace_id="1" * 32,
            )
        )
    async with await client_for(FakeGenerator(), ledger=ledger) as client:
        await sign_in(client)
        words = text_of((await client.get("/chats")).text)
        assert words.index("newer question") < words.index("older question")
        assert "colleague" not in words
        # Their chat is not found, the same page a chat that never existed gets.
        other = await client.get(f"/chats/{theirs}")
        absent = await client.get(f"/chats/{uuid.uuid4().hex}")
        junk = await client.get("/chats/not-a-chat-id")
        assert (other.status_code, absent.status_code, junk.status_code) == (404, 404, 404)
        assert other.text == absent.text and "No such chat" in other.text
        fragment = await client.get(f"/chats/{theirs}/turns")
        assert fragment.status_code == 404


# ---- each outcome is its own state -------------------------------------------------------------


async def test_a_verified_answer_is_numbered_cited_and_marked_verified(quote: str) -> None:
    async with await client_for(FakeGenerator(reply_of((APP11, quote)))) as client:
        await sign_in(client)
        _, fragment = await ask(client)
    assert outcomes(fragment) == ["verified"]
    words = text_of(fragment)
    assert "Verified" in words and "Answer withheld" not in words
    assert scan(fragment).where("li", **{"data-citation": APP11})
    assert "version C2026C00227" in words


async def test_a_withheld_answer_shows_its_blocked_citations_and_reason_and_none_of_the_prose(
    quote: str,
) -> None:
    prose = f'Section 6C(2) says "{SECRET_QUOTE}".'
    generator = FakeGenerator(reply_of((APP11, quote), (NOT_RETRIEVED, SECRET_QUOTE), answer=prose))
    async with await client_for(generator) as client:
        await sign_in(client)
        events, fragment = await ask(client)
    assert [n for n, _ in events][-1] == "answer"
    assert outcomes(fragment) == ["withheld"]  # not an error state
    words = text_of(fragment)
    assert "Answer withheld" in words and "A citation didn't match its clause" in words
    assert "Citations that did not verify" in words
    (blocked,) = scan(fragment).where("li", **{"data-blocked": NOT_RETRIEVED})
    assert blocked and "s 6C(2)" in words
    assert SECRET_QUOTE not in fragment and "Section 6C(2) says" not in fragment
    assert "Verified" not in words


async def test_an_answer_citing_nothing_is_withheld_with_its_own_reason() -> None:
    async with await client_for(FakeGenerator(reply_of(answer="Acme is probably fine."))) as c:
        await sign_in(c)
        _, fragment = await ask(c)
    assert outcomes(fragment) == ["withheld"]
    words = text_of(fragment)
    assert "The answer cited no clause" in words and "Citations that did not verify" not in words
    assert "Acme is probably fine." not in fragment


async def test_a_residency_refusal_is_its_own_state() -> None:
    generator = FakeGenerator(reply_of())
    settings = page_settings(**OFFSHORE)  # offshore generation, and MEMBER is no tester
    async with await client_for(generator, settings=settings) as client:
        await sign_in(client)
        events, fragment = await ask(client)
    assert [n for n, _ in events] == ["error"] and events[0][1]["error"] == "residency"
    assert outcomes(fragment) == ["residency"]
    words = text_of(fragment)
    assert "residency rule refused this question" in words
    assert "did not leave Australia" in words
    assert generator.calls == []


async def test_generation_unavailable_is_its_own_state_with_no_sdk_words() -> None:
    ledger = await acme_ledger()
    requests: list[httpx.Request] = []
    generator = quota_exhausted_generator(
        FakeRetrievalStore(golden_chunks(), ledger=ledger), requests
    )
    settings = page_settings(**OFFSHORE, offshore_generation_testers=MEMBER)
    async with await client_for(generator, ledger=ledger, settings=settings) as client:
        await sign_in(client)
        events, fragment = await ask(client)
        chat_id = events[0][1]["chat_id"]
        recorded = await client.get(f"/chats/{chat_id}/turns")  # as read back later
        chat_page = await client.get(f"/chats/{chat_id}")
        api = (await client.get(f"/v1/chats/{chat_id}")).json()
    assert events[-1][1]["error"] == "generation_unavailable" and len(requests) == 1
    assert outcomes(fragment) == ["generation_unavailable"]
    assert "isn't taking requests right now" in text_of(fragment)
    for leaked in (*SDK_WORDS, "Traceback"):
        assert leaked not in fragment
    # Its own recorded outcome (#289), so the same card after a reload, and the
    # chat's JSON says so with the reason and none of the provider's words.
    assert outcomes(recorded.text) == ["generation_unavailable"]
    assert text_of(recorded.text) == text_of(fragment)
    assert outcomes(chat_page.text) == ["generation_unavailable"]
    assert (
        api["turns"][1]["error"] == "generation_unavailable"
        and api["turns"][1]["reason"] == "quota"
    )


async def test_a_policy_corpus_over_the_cap_is_its_own_state_with_its_numbers() -> None:
    huge = "Acme keeps every record it holds, for as long as it can. " * 2_000
    async with await client_for(FakeGenerator(reply_of()), policy_text=huge) as client:
        await sign_in(client)
        events, fragment = await ask(client)
        reloaded = await client.get(f"/chats/{events[0][1]['chat_id']}")
    error = events[-1][1]
    assert error["error"] == "policy_corpus_too_large"
    assert outcomes(fragment) == ["policy_corpus_too_large"]
    # the same card, the same numbers, after a reload: the ledger holds them (#289)
    assert outcomes(reloaded.text) == ["policy_corpus_too_large"]
    assert f"about {error['estimated_tokens']} tokens" in text_of(reloaded.text)
    words = text_of(fragment)
    assert "too large to send with a question" in words
    assert f"about {error['estimated_tokens']} tokens" in words
    assert f"limit of {PREFIX_CAP_TOKENS}" in words
    assert "Acme keeps every record" not in fragment  # the policy's words stay out


async def test_a_malformed_generation_is_its_own_state() -> None:
    async with await client_for(FakeGenerator("this is not the JSON answer the schema needs")) as c:
        await sign_in(c)
        _, fragment = await ask(c)
    assert outcomes(fragment) == ["malformed_generation"]
    assert "not a valid answer" in text_of(fragment)
    assert "this is not the JSON" not in fragment


async def test_an_internal_failure_is_its_own_state_and_says_nothing_of_the_cause() -> None:
    async with await client_for(Exploding()) as client:
        await sign_in(client)
        events, fragment = await ask(client)
    assert events[-1][1]["error"] == "internal"
    assert outcomes(fragment) == ["internal"]
    assert "couldn't be completed" in text_of(fragment)
    assert "SECRET-INTERNAL-DETAIL" not in fragment and "RuntimeError" not in fragment


async def test_the_not_in_environment_card_is_shown_with_a_verified_answer() -> None:
    async with await client_for(FakeGenerator(spam_reply(marketing_quote()))) as client:
        await sign_in(client)
        _, fragment = await ask(client, SPAM_QUESTION)
    assert outcomes(fragment) == ["not-in-environment", "verified"]  # the card leads
    words = text_of(fragment)
    assert "Not in your environment" in words
    assert "Spam Act 2003" in words and 'you wrote "spam act"' in words
    assert scan(fragment).where("li", **{"data-citation": MARKETING})
    # The card is a state of its own: another answer to the same turn doesn't change it.
    assert words.index("Not in your environment") < words.index("Under the Spam Act you may")


async def test_the_not_in_environment_card_is_shown_with_a_withheld_answer() -> None:
    async with await client_for(FakeGenerator(spam_reply("a quote the policy never says"))) as c:
        await sign_in(c)
        _, fragment = await ask(c, SPAM_QUESTION)
    assert outcomes(fragment) == ["not-in-environment", "withheld"]
    assert "Spam Act 2003" in text_of(fragment)


async def test_a_question_that_names_nothing_outside_the_environment_has_no_card(
    member: httpx.AsyncClient,
) -> None:
    _, fragment = await ask(member)
    assert "not-in-environment" not in outcomes(fragment)


def _stored(
    chat_id: str, citations: tuple[Any, ...], *, nie: tuple[NotInEnvironment, ...] = ()
) -> ChatExchange:  # Any: citations
    return ChatExchange(
        tenant_id=TENANT,
        chat_id=chat_id,
        user_id=UID,
        question=QUESTION,
        answer=ChatAnswer(TurnOutcome.VERIFIED, ANSWER, citations, not_in_environment=nie),
        as_of=dt.date.fromisoformat(AS_OF),
        trace_id="7" * 32,
    )


async def test_an_incomplete_clause_is_flagged_beside_its_citation_and_in_a_summary() -> None:
    from compliance_rag.corpus.parser import FORMULA_NOT_TEXT

    ledger = await acme_ledger()
    chat_id = uuid.uuid4().hex
    await ledger.record_chat_exchange(
        _stored(
            chat_id,
            (
                ChatCitation(APP11, LAW_VERSION),
                ChatCitation(S6DA, LAW_VERSION, (FORMULA_NOT_TEXT,)),
            ),
        )
    )
    async with await client_for(FakeGenerator(), ledger=ledger) as client:
        await sign_in(client)
        page = (await client.get(f"/chats/{chat_id}")).text
    parsed = scan(page)
    assert parsed.where("div", **{"data-incomplete-summary": ""})
    words = text_of(page)
    assert "Incomplete clauses cited" in words and "s 6DA(2)" in words
    # beside the citation: the flag, and the words a person signed for it
    assert words.count("Incomplete") >= 3
    assert "Read the source before relying on them" in words
    # numbered, in order, the complete one without a flag
    complete = re.search(r'data-citation="[^"]*app11/1".*?</li>', page, re.S)
    assert complete is not None and "Incomplete" not in complete.group(0)


async def test_an_unreadable_stored_citation_stays_in_place_marked_and_is_not_a_link() -> None:
    ledger = await acme_ledger()
    chat_id = uuid.uuid4().hex
    await ledger.record_chat_exchange(_stored(chat_id, (ChatCitation(APP11, LAW_VERSION),)))
    turns = ledger._chats[(TENANT, chat_id)]  # pyright: ignore[reportPrivateUsage]
    from dataclasses import replace

    turns[1] = replace(
        turns[1],
        citations=(
            ChatCitation(APP11, LAW_VERSION),
            UnreadableCitation(UnreadableReason.BAD_INCOMPLETE, S6DA, LAW_VERSION),
            UnreadableCitation(UnreadableReason.NOT_AN_OBJECT),
        ),
    )
    async with await client_for(FakeGenerator(), ledger=ledger) as client:
        await sign_in(client)
        response = await client.get(f"/chats/{chat_id}")
    assert response.status_code == 200
    parsed = scan(response.text)
    assert len(parsed.where("a", **{"data-source-link": ""})) == 1
    assert len(parsed.where("li", **{"data-citation": "unreadable"})) == 2
    assert text_of(response.text).count("Unreadable") == 2


async def test_the_fragment_takes_no_parameter_so_a_query_cannot_change_what_is_shown(
    quote: str,
) -> None:
    """The page shows what the ledger recorded and nothing the browser says: a
    `refine`, or numbers, in the query change nothing."""
    forged = "?refine=policy_corpus_too_large&estimated_tokens=1&cap_tokens=2&error=residency"
    async with await client_for(FakeGenerator(reply_of((APP11, quote)))) as client:
        await sign_in(client)
        events, fragment = await ask(client)
        chat_id = events[0][1]["chat_id"]
        assert (await client.get(f"/chats/{chat_id}/turns{forged}")).text == fragment
    async with await client_for(Exploding()) as client:
        await sign_in(client)
        events, fragment = await ask(client)
        chat_id = events[0][1]["chat_id"]
        marked = await client.get(f"/chats/{chat_id}/turns{forged}&x=<b>1</b>")
        assert marked.text == fragment and outcomes(marked.text) == ["internal"]
        assert "tokens" not in text_of(marked.text)


async def test_a_turn_recorded_as_internal_before_0019_is_the_generic_card() -> None:
    ledger = await acme_ledger()
    chat_id = uuid.uuid4().hex
    await ledger.record_chat_exchange(
        ChatExchange(
            tenant_id=TENANT,
            chat_id=chat_id,
            user_id=UID,
            question=QUESTION,
            answer=ChatAnswer(TurnOutcome.INTERNAL),
            as_of=dt.date.fromisoformat(AS_OF),
            trace_id="3" * 32,
        )
    )
    async with await client_for(FakeGenerator(), ledger=ledger) as client:
        await sign_in(client)
        page = (await client.get(f"/chats/{chat_id}")).text
    assert outcomes(page) == ["internal"] and "couldn't be completed" in text_of(page)


async def test_an_over_cap_turn_whose_stored_measure_cant_be_read_still_has_its_card() -> None:
    """The chat JSON says `null` for a measure it couldn't read (#289): the card
    keeps its outcome and says the numbers are unavailable."""
    ledger = await acme_ledger()
    chat_id = uuid.uuid4().hex
    await ledger.record_chat_exchange(
        ChatExchange(
            tenant_id=TENANT,
            chat_id=chat_id,
            user_id=UID,
            question=QUESTION,
            answer=ChatAnswer(TurnOutcome.POLICY_CORPUS_TOO_LARGE, prefix=PrefixMeasure(9, 3)),
            as_of=dt.date.fromisoformat(AS_OF),
            trace_id="4" * 32,
        )
    )
    turns = ledger._chats[(TENANT, chat_id)]  # pyright: ignore[reportPrivateUsage]
    turns[1] = replace(turns[1], prefix=None)
    async with await client_for(FakeGenerator(), ledger=ledger) as client:
        await sign_in(client)
        page = (await client.get(f"/chats/{chat_id}")).text
    assert outcomes(page) == ["policy_corpus_too_large"]
    assert "can't be read" in text_of(page)


def test_a_chat_id_is_all_of_its_form_not_just_a_line_of_it() -> None:
    good = uuid.uuid4().hex
    assert is_chat_id(good)
    for bad in (good + "\n", "\n" + good, good[:-1], good.upper(), good + " ", "", "new"):
        assert not is_chat_id(bad), repr(bad)


def test_the_page_error_handler_handles_only_a_page_error() -> None:
    """Not an `assert`, which `python -O` removes: any other exception handed
    to it is raised again, never shown as a page."""
    request = Request({"type": "http", "method": "GET", "path": "/chats", "headers": []})
    assert page_error(request, PageError(404, "No such chat", "x")).status_code == 404
    with pytest.raises(ValueError, match="not mine"):
        page_error(request, ValueError("not mine"))


# ---- escaping -----------------------------------------------------------------------------------

MARKUP_QUESTION = (
    '<script>alert("q")</script><img src=x onerror=alert(1)> and <b onclick="steal()">bold</b> '
    "&amp; </textarea></title></main>"
)
MARKUP_ANSWER = (
    '<script>alert("a")</script><a href="javascript:steal()">click</a> <i onmouseover="x()">'
    "&lt;already&gt;</i> </article></section>"
)


async def test_markup_in_the_question_and_the_answer_reaches_the_browser_as_text(
    quote: str,
) -> None:
    generator = FakeGenerator(reply_of((APP11, quote), answer=MARKUP_ANSWER))
    async with await client_for(generator) as client:
        await sign_in(client)
        events, fragment = await ask(client, MARKUP_QUESTION)
        chat_id = events[0][1]["chat_id"]
        pages = {
            "fragment": fragment,
            "chat": (await client.get(f"/chats/{chat_id}")).text,
            "list": (await client.get("/chats")).text,
        }
    for name, page in pages.items():
        parsed = scan(page)
        tags = {t for t, _ in parsed.elements}
        assert "img" not in tags and "i" not in tags and "b" not in tags, name
        assert not parsed.script_text, name
        assert not [a for _, a in parsed.elements if any(k.startswith("on") for k in a)], name
        assert "<script>alert" not in page and "javascript:steal" not in {
            a.get("href") for _, a in parsed.elements
        }, name
        assert "&lt;script&gt;alert(&#34;q&#34;)&lt;/script&gt;" in page, name
    # The words are all still there, as words.
    assert '<script>alert("q")</script>' in text_of(pages["fragment"])
    assert '<script>alert("a")</script>' in text_of(pages["fragment"])
    # The page is still one page: nothing in the question closed an element of it.
    assert len(scan(pages["chat"]).where("main")) == 1
    assert len(scan(pages["chat"]).where("title")) == 1


async def test_the_not_in_environment_names_are_escaped_too() -> None:
    ledger = await acme_ledger()
    chat_id = uuid.uuid4().hex
    nasty = NotInEnvironment('<script>alert("n")</script>', '<b onclick="x()">Act</b>')
    await ledger.record_chat_exchange(
        _stored(chat_id, (ChatCitation(APP11, LAW_VERSION),), nie=(nasty,))
    )
    async with await client_for(FakeGenerator(), ledger=ledger) as client:
        await sign_in(client)
        page = (await client.get(f"/chats/{chat_id}")).text
    assert "<script>alert" not in page and "<b onclick" not in page
    assert {t for t, _ in scan(page).elements}.isdisjoint({"b"})


# ---- the CSP, and no inline script ---------------------------------------------------------------


async def _every_page(client: httpx.AsyncClient, chat_id: str) -> dict[str, httpx.Response]:
    return {
        "/chats": await client.get("/chats"),
        "/chats/new": await client.get("/chats/new"),
        f"/chats/{chat_id}": await client.get(f"/chats/{chat_id}"),
        f"/chats/{chat_id}/turns": await client.get(f"/chats/{chat_id}/turns"),
        "a 404 page": await client.get(f"/chats/{uuid.uuid4().hex}"),
        "/signin": await client.get("/signin"),
    }


async def test_one_strict_csp_is_on_every_page_response(quote: str) -> None:
    async with await client_for(FakeGenerator(reply_of((APP11, quote)))) as client:
        responses = {"signed out": await client.get("/chats")}
        await sign_in(client)
        events, _ = await ask(client)
        responses.update(await _every_page(client, events[0][1]["chat_id"]))
    assert len(responses) == 7
    # and on the refusals no page makes: the fragment's 404, 401 and 422, and a
    # method the routes don't take
    async with await client_for(FakeGenerator()) as other:
        responses["fragment, signed out (401)"] = await other.get(
            f"/chats/{uuid.uuid4().hex}/turns"
        )
        await sign_in(other)
        responses["fragment, no such chat (404)"] = await other.get(
            f"/chats/{uuid.uuid4().hex}/turns"
        )
        responses["fragment, not a chat id (404)"] = await other.get("/chats/nope/turns")
        responses["POST /chats (405)"] = await other.post("/chats")
        responses["POST /chats/new (405)"] = await other.post("/chats/new")
    assert [r.status_code for n, r in responses.items() if "(" in n] == [401, 404, 404, 405, 405]
    for name, response in responses.items():
        # the sign-in page alone has the wider policy for Google sign-in (#291);
        # tests/test_api_session.py pins what it adds
        expected = (
            sign_in_content_security_policy(f"{PROJECT}.firebaseapp.com")
            if name == "/signin"
            else CONTENT_SECURITY_POLICY
        )
        assert response.headers["content-security-policy"] == expected, name
        assert response.headers["x-content-type-options"] == "nosniff", name
    csp = {d.split()[0]: d.split()[1:] for d in CONTENT_SECURITY_POLICY.split("; ")}
    assert csp["script-src"] == ["'self'"] and csp["style-src"] == ["'self'"]
    assert csp["default-src"] == ["'none'"] and csp["frame-src"] == ["'self'"]
    assert csp["frame-ancestors"] == ["'none'"] and csp["base-uri"] == ["'none'"]
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY
    # pages hold member data: never cached
    assert responses["/chats"].headers["cache-control"] == "no-store"


async def test_no_page_has_an_inline_script_handler_style_or_a_foreign_asset(quote: str) -> None:
    ledger = await acme_ledger()
    several = uuid.uuid4().hex
    from compliance_rag.corpus.parser import FORMULA_NOT_TEXT

    # A chat with a verified answer, an incomplete clause, an unreadable one,
    # a flag and every kind of failure: every branch of the templates.
    await ledger.record_chat_exchange(
        _stored(
            several,
            (ChatCitation(S6DA, LAW_VERSION, (FORMULA_NOT_TEXT,)),),
            nie=(NotInEnvironment("spam act", "Spam Act 2003"), NotInEnvironment("x", None, "Y")),
        )
    )
    for outcome in (
        TurnOutcome.BLOCKED,
        TurnOutcome.UNCITED,
        TurnOutcome.RESIDENCY,
        TurnOutcome.MALFORMED_GENERATION,
        TurnOutcome.INTERNAL,
    ):
        await ledger.record_chat_exchange(
            ChatExchange(
                tenant_id=TENANT,
                chat_id=several,
                user_id=UID,
                question=QUESTION,
                answer=ChatAnswer(
                    outcome, blocked=(NOT_RETRIEVED,) if outcome is TurnOutcome.BLOCKED else ()
                ),
                as_of=dt.date.fromisoformat(AS_OF),
                trace_id="8" * 32,
            )
        )
    async with await client_for(FakeGenerator(reply_of((APP11, quote))), ledger=ledger) as client:
        await sign_in(client)
        events, _ = await ask(client)
        pages = await _every_page(client, several)
        pages["new chat answered"] = await client.get(f"/chats/{events[0][1]['chat_id']}")
        pages["403"] = await client.get(
            "/chats",
            headers={
                "Cookie": f"{COOKIE_NAME}="
                + SessionCodec(SECRET).seal(
                    Member(uid="u", email="no@elsewhere.example"), expires_at=int(time.time()) + 99
                )
            },
        )
    assert outcomes(pages[f"/chats/{several}"].text) == [
        "not-in-environment",
        "verified",
        "withheld",
        "withheld",
        "residency",
        "malformed_generation",
        "internal",
    ]
    for name, response in pages.items():
        if response.status_code == 303:
            continue
        if not response.headers["content-type"].startswith("text/html"):
            continue
        parsed = scan(response.text)
        assert not parsed.script_text.strip(), name
        assert not parsed.style_text.strip(), name
        for tag, attrs in parsed.elements:
            assert tag != "style", name
            assert not [k for k in attrs if k.startswith("on")], (name, tag)
            assert "style" not in attrs, (name, tag)
            if tag == "script":
                assert attrs["src"].startswith("/static/"), name
            for key in ("src", "href", "action"):
                value = attrs.get(key, "")
                assert not re.match(r"(?i)\s*(javascript:|data:|https?:|//)", value), (name, value)
    # the chat page loads exactly htmx and the one script of ours, from here
    chat = scan(pages[f"/chats/{several}"].text)
    assert sorted(a["src"] for a in chat.where("script")) == [
        "/static/ask.js",
        "/static/vendor/htmx/htmx.min.js",
    ]
    assert [a["href"] for a in chat.where("link")] == ["/static/app.css"]


def test_no_template_marks_anything_safe_or_turns_escaping_off() -> None:
    templates = sorted(TEMPLATE_DIR.glob("*.html"))
    assert len(templates) >= 5
    for template in templates:
        source = template.read_text()
        assert not re.search(
            r"\|\s*safe\b|\|\s*forceescape|Markup|\{%-?\s*autoescape|\|\s*striptags", source
        ), template.name
        assert "{% raw" not in source and "<script>" not in source, template.name
        assert not re.search(r"\son[a-z]+\s*=", source), template.name
        assert not re.search(r"<[^>]+\sstyle\s*=", source), template.name
        assert not re.search(r"https?://|//cdn", source), template.name
    assert ENV.autoescape is True  # for every template, whatever its extension


def test_ask_js_never_writes_html_and_htmx_cant_eval() -> None:
    script = (STATIC_DIR / "ask.js").read_text()
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
    # htmx is configured, on every page, not to insert a style or to eval
    base = (TEMPLATE_DIR / "base.html").read_text()
    config = re.search(r'name="htmx-config" content=\'([^\']+)\'', base)
    assert config is not None
    for setting in ("includeIndicatorStyles", "allowEval", "allowScriptTags", "historyEnabled"):
        assert f'"{setting}":false' in config.group(1)
    assert '"selfRequestsOnly":true' in config.group(1)


def test_htmx_is_vendored_with_its_version_and_hash_recorded() -> None:
    directory = STATIC_DIR / "vendor" / "htmx"
    recorded = (directory / "README.md").read_text()
    digest = hashlib.sha256((directory / "htmx.min.js").read_bytes()).hexdigest()
    assert "**version 2.0.11**" in recorded
    assert digest in recorded
    assert (directory / "LICENSE").read_text().strip()


async def test_the_scripts_and_styles_are_served_by_the_api_itself() -> None:
    async with await client_for(FakeGenerator()) as client:  # no session: assets are public
        for path, kind in (
            ("/static/ask.js", "javascript"),
            ("/static/vendor/htmx/htmx.min.js", "javascript"),
            ("/static/app.css", "css"),
        ):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert kind in response.headers["content-type"], path
            assert response.headers["x-content-type-options"] == "nosniff", path


# ---- the panel, the form, the phone ------------------------------------------------------------


async def test_the_chat_page_has_the_form_the_progress_and_a_sandboxed_citation_panel(
    member: httpx.AsyncClient,
) -> None:
    events, _ = await ask(member)
    parsed = scan((await member.get(f"/chats/{events[0][1]['chat_id']}")).text)
    (frame,) = parsed.where("iframe")
    assert frame["sandbox"] == "" and "src" not in frame  # every capability off; nothing loaded
    assert frame["referrerpolicy"] == "no-referrer"
    (panel,) = parsed.where("aside", id="panel")
    assert "hidden" in panel
    assert [a["data-stage"] for t, a in parsed.elements if t == "li" and "data-stage" in a] == [
        "coverage",
        "retrieval",
        "answer",
    ]
    (progress,) = parsed.where("ol", id="progress")
    assert "hidden" in progress
    assert parsed.where("button", id="panel-close") and parsed.where("form", id="ask-form")
    assert parsed.where("button", type="button", **{"data-sign-out": ""})


async def test_the_pages_are_made_for_a_phone(member: httpx.AsyncClient) -> None:
    events, _ = await ask(member)
    for path in ("/chats", "/chats/new", f"/chats/{events[0][1]['chat_id']}"):
        parsed = scan((await member.get(path)).text)
        (viewport,) = parsed.where("meta", name="viewport")
        assert viewport["content"] == "width=device-width, initial-scale=1", path
    css = (STATIC_DIR / "app.css").read_text()
    # mobile first: the base rules are the one-column phone layout, and room
    # beside the conversation for the panel is the only thing a width adds
    assert re.findall(r"@media \((min|max)-width: [\d.]+rem\)", css) == ["min"]
    assert not re.search(r"(?<!-)width:\s*\d+(\.\d+)?px", css) and "min-width: 3" not in css
    assert "overflow-wrap: anywhere" in css  # a long clause id can't widen the page
    assert "position: fixed" in css  # the panel is a sheet over the screen until there's room
