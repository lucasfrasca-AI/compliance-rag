"""Chat history through the API (issue #162): `POST /v1/answers` records each
question and what the member was shown for it, as the next two turns of a
chat, and `GET /v1/chats` and `GET /v1/chats/{id}` read them back.

Everything is real but the generator, as in `test_api_answers.py`, whose
helpers and ADR-0003 fixture (a blocked quote the model's prose repeats) this
reuses. The ledger is the fake, and Postgres for the last test.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import os
import re
import uuid
from dataclasses import replace
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
from compliance_rag.adapters.interfaces import (
    ChatAnswer,
    ChatCitation,
    ChatExchange,
    Generation,
    LedgerStore,
    Profile,
    TurnOutcome,
    TurnRole,
    UnreadableCitation,
    UnreadableReason,
)
from compliance_rag.api import answers
from compliance_rag.api.app import create_app
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.corpus.models import PUBLIC_TENANT_ID, DocumentClass
from compliance_rag.eval.applicability import load_applicability_set
from compliance_rag.eval.golden_corpus import POLICY_TENANT_ID, golden_chunks, golden_clauses
from compliance_rag.eval.harness import GOLDEN_VALID_FROM, golden_clause_store
from compliance_rag.graph.mappings import load_mapping_set
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier
from tests.api_support import MEMBER, TENANT, api_settings, bearer, fake_api_services, mint
from tests.test_api_answers import (
    AS_OF,
    GA05,
    NOT_RETRIEVED,
    OFFSHORE,
    QUESTION,
    SECRET_QUOTE,
)
from tests.test_api_answers import _client as answers_client  # pyright: ignore[reportPrivateUsage]
from tests.test_api_answers import _events as events_of  # pyright: ignore[reportPrivateUsage]
from tests.test_api_answers import _ledger as acme_ledger  # pyright: ignore[reportPrivateUsage]
from tests.test_api_answers import _reply as reply_of  # pyright: ignore[reportPrivateUsage]
from tests.test_applicability_gate import CORPUS_MAPPINGS, fake_for

APP11 = "privacy-act-1988:C2026C00227:sch1/app11/1"
LAW_VERSION = "C2026C00227"
UID = "uid-123"  # the sign-in `mint()` makes by default
COLLEAGUE = mint(sub="uid-colleague", email="second@acme.example")
ANSWER = "Acme must take reasonable steps."
FOLLOW_UP = "And what about when the information is no longer needed?"
TRACE = re.compile(r"[0-9a-f]{32}")


@pytest.fixture
def quote() -> str:
    clause = next(c for c in golden_clauses() if c.clause_id == APP11)
    return clause.canonical_text[:60]


async def _ask(
    client: httpx.AsyncClient, *, token: str | None = None, **body: Any
) -> tuple[httpx.Response, list[tuple[str, dict[str, Any]]]]:  # Any: JSON
    response = await client.post(
        "/v1/answers",
        json={"question": QUESTION, "as_of": AS_OF, **body},
        headers=bearer(token) if token else None,
    )
    return response, events_of(response.text) if response.status_code == 200 else []


async def _asked(
    generator: FakeGenerator, *, ledger: FakeLedgerStore | None = None, **body: Any
) -> tuple[httpx.Response, list[tuple[str, dict[str, Any]]], FakeLedgerStore]:  # Any: JSON
    ledger = ledger or await acme_ledger()
    async with await answers_client(generator, ledger, await golden_clause_store()) as c:
        response, events = await _ask(c, **body)
    return response, events, ledger


def _chat_id(events: list[tuple[str, dict[str, Any]]]) -> str:  # Any: JSON
    """Every event names the chat, and it is the same chat."""
    ids = {data["chat_id"] for _, data in events}
    assert len(ids) == 1
    (chat_id,) = ids
    assert re.fullmatch(r"[0-9a-f]{32}", chat_id)
    return chat_id


async def _turns(
    ledger: LedgerStore, chat_id: str, uid: str = UID
) -> tuple[Any, ...]:  # Any: turns
    return await ledger.chat_turns(TENANT, uid, chat_id)


# ---- the question and the answer are recorded together -------------------------------


async def test_a_question_without_a_chat_starts_one_and_every_event_names_it(quote: str) -> None:
    response, events, ledger = await _asked(FakeGenerator(reply_of((APP11, quote))))
    assert [name for name, _ in events] == ["coverage", "retrieval", "answer"]
    chat_id = _chat_id(events)
    (started,) = await ledger.chats(TENANT, UID)
    assert (started.chat_id, started.turns, started.first_question) == (chat_id, 2, QUESTION)
    assert response.status_code == 200


async def test_the_exchange_is_recorded_with_the_requests_trace_the_members_uid_and_date(
    quote: str,
) -> None:
    response, events, ledger = await _asked(FakeGenerator(reply_of((APP11, quote))))
    question, answer = await _turns(ledger, _chat_id(events))
    assert [(t.turn_index, t.role) for t in (question, answer)] == [
        (0, TurnRole.USER),
        (1, TurnRole.ASSISTANT),
    ]
    # The uid the sign-in carries, never the email (ADR-0036).
    assert {t.user_id for t in (question, answer)} == {UID} != {MEMBER}
    assert {t.trace_id for t in (question, answer)} == {response.headers["x-trace-id"]}
    assert {t.as_of for t in (question, answer)} == {dt.date.fromisoformat(AS_OF)}
    assert question.content == QUESTION and question.outcome is None
    assert answer.content == ANSWER and answer.outcome is TurnOutcome.VERIFIED
    assert answer.citations == (ChatCitation(APP11, LAW_VERSION),)


async def test_what_is_recorded_is_what_the_member_was_shown(quote: str) -> None:
    _, events, ledger = await _asked(FakeGenerator(reply_of((APP11, quote))))
    shown = dict(events)["answer"]
    _, answer = await _turns(ledger, _chat_id(events))
    assert answer.content == shown["text"]
    assert [(c.clause_id, c.version_id) for c in answer.citations] == [
        (c["clause_id"], c["version_id"]) for c in shown["citations"]
    ]
    assert answer.trace_id == shown["trace_id"]


async def test_a_chat_continues_when_its_id_is_named_and_each_request_has_its_own_trace(
    quote: str,
) -> None:
    ledger = await acme_ledger()
    generator = FakeGenerator(reply_of((APP11, quote)))
    async with await answers_client(generator, ledger, await golden_clause_store()) as c:
        first, events = await _ask(c)
        chat_id = _chat_id(events)
        second, continued = await _ask(c, question=FOLLOW_UP, chat_id=chat_id, as_of="2026-10-13")
        assert _chat_id(continued) == chat_id  # the same chat, not a new one
        read = (await c.get(f"/v1/chats/{chat_id}")).json()
    turns = await _turns(ledger, chat_id)
    assert [(t.turn_index, t.role) for t in turns] == [
        (0, TurnRole.USER),
        (1, TurnRole.ASSISTANT),
        (2, TurnRole.USER),
        (3, TurnRole.ASSISTANT),
    ]
    assert [t.content for t in turns[0::2]] == [QUESTION, FOLLOW_UP]
    assert [t.trace_id for t in turns[0::3]] == [
        first.headers["x-trace-id"],
        second.headers["x-trace-id"],
    ]
    assert [t.as_of.isoformat() for t in turns[0::2]] == [AS_OF, "2026-10-13"]
    assert [t["turn_index"] for t in read["turns"]] == [0, 1, 2, 3]
    assert len(await ledger.chats(TENANT, UID)) == 1


async def test_earlier_turns_do_not_reach_the_model(quote: str) -> None:
    """The prompt and retrieval are as they were with no chat (#29): a follow-up
    sent in a chat is the prompt it would have been on its own. Whether history
    should reach the model is the design session's (#162)."""
    ledger = await acme_ledger()
    generator = FakeGenerator(reply_of((APP11, quote)))
    async with await answers_client(generator, ledger, await golden_clause_store()) as c:
        _, events = await _ask(c)
        await _ask(c, question=FOLLOW_UP, chat_id=_chat_id(events))
        await _ask(c, question=FOLLOW_UP)  # the same question, in no chat
    in_chat, alone = generator.calls[1], generator.calls[2]
    assert in_chat["user"] == alone["user"] and in_chat["cached_prefix"] == alone["cached_prefix"]
    assert QUESTION not in str(in_chat["user"]) and ANSWER not in str(in_chat["user"])


# ---- an answer turn holds only what the member was shown (ADR-0003) ------------------


async def test_a_withheld_answers_turn_holds_the_reason_and_the_blocked_ids_and_none_of_the_prose(
    quote: str,
) -> None:
    """The ADR-0003 fixture of `test_api_answers.py`: the prose repeats a quote
    the verifier refused. It reaches no client, and it reaches no row either."""
    prose = f'Section 6C(2) says "{SECRET_QUOTE}".'
    generator = FakeGenerator(reply_of((APP11, quote), (NOT_RETRIEVED, SECRET_QUOTE), answer=prose))
    ledger = await acme_ledger()
    async with await answers_client(generator, ledger, await golden_clause_store()) as c:
        _, events = await _ask(c)
        chat_id = _chat_id(events)
        read = await c.get(f"/v1/chats/{chat_id}")
    assert dict(events)["answer"]["withheld"] == answers.BLOCKED
    question, answer = await _turns(ledger, chat_id)
    assert (answer.outcome, answer.content) == (TurnOutcome.BLOCKED, None)
    assert answer.blocked == (NOT_RETRIEVED,)
    assert answer.citations == ()  # the one that did verify isn't shown either
    # Nowhere in the ledger: not in the chat's rows, not in any other store of it.
    for text in (SECRET_QUOTE, "Section 6C(2)", prose):
        assert text not in repr(vars(ledger))
        assert text not in read.text
    # Read back, it is withheld with its reason and its blocked clause.
    assert read.json()["turns"][1] == {
        "turn_index": 1,
        "role": "assistant",
        "as_of": AS_OF,
        "trace_id": answer.trace_id,
        "created_at": answer.created_at.isoformat() if answer.created_at else None,
        "verified": False,
        "text": None,
        "citations": [],
        "incomplete": [],
        "blocked": [{"clause_id": NOT_RETRIEVED, "cite": "s 6C(2)"}],
        "withheld": answers.BLOCKED,
        "error": None,
    }
    assert question.content == QUESTION


async def test_an_answer_citing_nothing_is_recorded_as_withheld_with_no_text() -> None:
    prose = "Acme is probably fine."
    _, events, ledger = await _asked(FakeGenerator(reply_of(answer=prose)))
    _, answer = await _turns(ledger, _chat_id(events))
    assert (answer.outcome, answer.content, answer.citations, answer.blocked) == (
        TurnOutcome.UNCITED,
        None,
        (),
        (),
    )
    assert prose not in repr(vars(ledger))


# ---- a request that ends in an error keeps the question and a marker ---------------


async def test_a_residency_refusal_records_the_question_and_the_marker_never_the_message() -> None:
    """The question is the member's own words, kept in Sydney; the refusal
    exists so that it never leaves it. The message names a rule, not a turn."""
    generator = FakeGenerator(reply_of())
    settings = api_settings(**OFFSHORE)
    ledger = await acme_ledger()
    clauses = await golden_clause_store()
    async with await answers_client(generator, ledger, clauses, settings=settings) as c:
        response, events = await _ask(c)
        chat_id = _chat_id(events)
        read = (await c.get(f"/v1/chats/{chat_id}")).json()
    assert [name for name, _ in events] == ["error"] and generator.calls == []
    assert events[0][1]["error"] == "residency" and "ADR-0032" in events[0][1]["message"]
    question, answer = await _turns(ledger, chat_id)
    assert question.content == QUESTION
    assert (answer.outcome, answer.content, answer.citations) == (TurnOutcome.RESIDENCY, None, ())
    assert answer.trace_id == response.headers["x-trace-id"]
    assert "ADR-0032" not in repr(vars(ledger))
    assert read["turns"][1]["error"] == "residency" and read["turns"][1]["text"] is None
    assert read["turns"][1]["withheld"] is None and read["turns"][1]["verified"] is False


async def test_a_malformed_generation_records_the_question_and_a_marker_never_the_reply() -> None:
    reply = "this is not the JSON answer the schema needs"
    _, events, ledger = await _asked(FakeGenerator(reply))
    assert events[-1][1]["error"] == "malformed_generation"
    question, answer = await _turns(ledger, _chat_id(events))
    assert (question.content, answer.outcome, answer.content) == (
        QUESTION,
        TurnOutcome.MALFORMED_GENERATION,
        None,
    )
    assert reply not in repr(vars(ledger))


async def test_a_failure_before_anything_is_generated_records_the_question_and_a_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def down(*_: object, **__: object) -> object:
        raise RuntimeError("the graph is down: secret detail")

    monkeypatch.setattr(FakeGraphStore, "coverage", down)
    generator = FakeGenerator(reply_of())
    response, events, ledger = await _asked(generator)
    assert [name for name, _ in events] == ["error"] and events[0][1] == {
        **answers.FAILED,
        "chat_id": events[0][1]["chat_id"],
    }
    _, answer = await _turns(ledger, _chat_id(events))
    assert (answer.outcome, answer.content) == (TurnOutcome.INTERNAL, None)
    assert generator.calls == [] and "secret detail" not in response.text
    assert "secret detail" not in repr(vars(ledger))


async def test_an_answer_the_ledger_cant_record_is_not_shown(
    quote: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is shown that the history lacks. The member sees an error, so
    an error is what the chat keeps, once the ledger takes anything."""
    original = FakeLedgerStore.record_chat_exchange
    attempts: list[TurnOutcome] = []

    async def flaky(self: FakeLedgerStore, exchange: ChatExchange) -> None:
        attempts.append(exchange.answer.outcome)
        if len(attempts) == 1:
            raise ConnectionError("the ledger is down: secret detail")
        await original(self, exchange)

    monkeypatch.setattr(FakeLedgerStore, "record_chat_exchange", flaky)
    response, events, ledger = await _asked(FakeGenerator(reply_of((APP11, quote))))
    assert [name for name, _ in events] == ["coverage", "retrieval", "error"]
    assert events[-1][1]["error"] == "internal"
    assert ANSWER not in response.text and "secret detail" not in response.text
    assert attempts == [TurnOutcome.VERIFIED, TurnOutcome.INTERNAL]
    _, answer = await _turns(ledger, _chat_id(events))
    assert (answer.outcome, answer.content) == (TurnOutcome.INTERNAL, None)
    assert ANSWER not in repr(vars(ledger))


class GatedGenerator(FakeGenerator):
    """Holds its answer until told to release it."""

    def __init__(self, reply: str) -> None:
        super().__init__(reply)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(
        self, *, system: str, cached_prefix: str | None, user: str, max_tokens: int
    ) -> Generation:
        self.started.set()
        await self.release.wait()
        return await super().generate(
            system=system, cached_prefix=cached_prefix, user=user, max_tokens=max_tokens
        )


async def test_an_answer_is_recorded_even_if_the_client_leaves_first(quote: str) -> None:
    """The tokens it spends are recorded either way, and so is the answer: the
    member finds it in the chat when they come back."""
    generator = GatedGenerator(reply_of((APP11, quote)))
    ledger = await acme_ledger()
    async with await answers_client(generator, ledger, await golden_clause_store()) as c:
        request = asyncio.ensure_future(_ask(c))
        await asyncio.wait_for(generator.started.wait(), timeout=10)
        request.cancel()  # the client goes, mid-generation
        with contextlib.suppress(asyncio.CancelledError):
            await request
        assert await ledger.chats(TENANT, UID) == ()  # still being generated
        generator.release.set()
        await asyncio.gather(*list(answers._running))  # pyright: ignore[reportPrivateUsage]
        (chat,) = await ledger.chats(TENANT, UID)
        read = (await c.get(f"/v1/chats/{chat.chat_id}")).json()
    question, answer = await _turns(ledger, chat.chat_id)
    assert (question.content, answer.outcome, answer.content) == (
        QUESTION,
        TurnOutcome.VERIFIED,
        ANSWER,
    )
    assert TRACE.fullmatch(answer.trace_id) and answer.trace_id != "0" * 32
    assert read["turns"][1]["text"] == ANSWER


# ---- a chat is its member's alone ----------------------------------------------------


def _theirs(
    chat_id: str, *, tenant_id: str = TENANT, user_id: str = "uid-colleague"
) -> ChatExchange:
    return ChatExchange(
        tenant_id=tenant_id,
        chat_id=chat_id,
        user_id=user_id,
        question="A private question about the vault keys",
        answer=ChatAnswer(
            TurnOutcome.VERIFIED, "A private answer.", (ChatCitation(APP11, LAW_VERSION),)
        ),
        as_of=dt.date.fromisoformat(AS_OF),
        trace_id="9" * 32,
    )


async def test_another_members_chat_another_tenants_and_none_at_all_are_the_same_404(
    quote: str,
) -> None:
    ledger = await acme_ledger()
    await ledger.put_profile(Profile("other-tenant"))
    colleagues, other_tenants, nobodys = (uuid.uuid4().hex for _ in range(3))
    await ledger.record_chat_exchange(_theirs(colleagues))
    # The same person, by uid, in a tenant that isn't the configured one.
    await ledger.record_chat_exchange(_theirs(other_tenants, tenant_id="other-tenant", user_id=UID))
    before = repr(vars(ledger))
    generator = FakeGenerator(reply_of((APP11, quote)))
    async with await answers_client(generator, ledger, await golden_clause_store()) as c:
        refusals = [
            [
                await c.get(f"/v1/chats/{chat_id}"),
                (await _ask(c, chat_id=chat_id))[0],
            ]
            for chat_id in (colleagues, other_tenants, nobodys)
        ]
    assert {r.status_code for pair in refusals for r in pair} == {404}
    # Not told apart: whatever the reason, the same answer.
    assert {r.text for pair in refusals for r in pair} == {'{"detail":"no such chat"}'}
    # Nothing was checked, retrieved, generated or written for any of them.
    assert generator.calls == [] and repr(vars(ledger)) == before
    assert "private" not in "".join(r.text for pair in refusals for r in pair)


async def test_a_chat_is_the_uid_s_not_the_emails(quote: str) -> None:
    """An email can be reassigned; a uid can't (ADR-0036). A sign-in with the
    same email and another uid is another member, and finds nothing of the
    first one's."""
    ledger = await acme_ledger()
    generator = FakeGenerator(reply_of((APP11, quote)))
    reassigned = mint(sub="uid-reassigned", email=MEMBER)
    async with await answers_client(generator, ledger, await golden_clause_store()) as c:
        _, events = await _ask(c)
        chat_id = _chat_id(events)
        mine = await c.get(f"/v1/chats/{chat_id}")
        theirs = await c.get(f"/v1/chats/{chat_id}", headers=bearer(reassigned))
        listed = await c.get("/v1/chats", headers=bearer(reassigned))
        continued = await _ask(c, token=reassigned, chat_id=chat_id)
    assert mine.status_code == 200
    assert theirs.status_code == 404 and continued[0].status_code == 404
    assert listed.json() == {"chats": []}
    assert len(await _turns(ledger, chat_id)) == 2


async def test_each_member_lists_their_own_chats_newest_first(quote: str) -> None:
    ledger = await acme_ledger()
    generator = FakeGenerator(reply_of((APP11, quote)))
    async with await answers_client(generator, ledger, await golden_clause_store()) as c:
        _, older = await _ask(c, question="First chat, first question")
        _, newer = await _ask(c, question="Second chat")
        _, colleagues = await _ask(c, token=COLLEAGUE, question="The colleague's chat")
        before = (await c.get("/v1/chats")).json()["chats"]
        # Continuing the older one makes it the one with the latest turn.
        await _ask(c, question=FOLLOW_UP, chat_id=_chat_id(older))
        after = (await c.get("/v1/chats")).json()["chats"]
        theirs = (await c.get("/v1/chats", headers=bearer(COLLEAGUE))).json()["chats"]
        # Their chat, read by the first member, is not found.
        hers = await c.get(f"/v1/chats/{_chat_id(colleagues)}")
    assert [x["chat_id"] for x in before] == [_chat_id(newer), _chat_id(older)]
    assert [x["chat_id"] for x in after] == [_chat_id(older), _chat_id(newer)]
    assert [(x["first_question"], x["turns"]) for x in after] == [
        ("First chat, first question", 4),
        ("Second chat", 2),
    ]
    assert [x["chat_id"] for x in theirs] == [_chat_id(colleagues)]
    assert after[0]["started_at"] < after[0]["updated_at"]
    assert hers.status_code == 404


async def test_a_chat_reads_back_turn_by_turn_as_the_member_was_shown(quote: str) -> None:
    ledger = await acme_ledger()
    generator = FakeGenerator(reply_of((APP11, quote)))
    async with await answers_client(generator, ledger, await golden_clause_store()) as c:
        response, events = await _ask(c)
        read = (await c.get(f"/v1/chats/{_chat_id(events)}")).json()
    assert read["chat_id"] == _chat_id(events)
    question, answer = read["turns"]
    assert question["role"] == "user" and question["text"] == QUESTION
    assert "citations" not in question and "verified" not in question
    assert answer["role"] == "assistant" and answer["verified"] is True
    assert answer["text"] == ANSWER and answer["withheld"] is None and answer["error"] is None
    # The clause and its version, which is what keeps it clickable.
    assert answer["citations"] == [
        {"clause_id": APP11, "cite": "APP 11.1", "version_id": LAW_VERSION, "incomplete": []}
    ]
    assert answer["trace_id"] == question["trace_id"] == response.headers["x-trace-id"]
    assert answer["as_of"] == AS_OF


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/chats"),
        ("GET", f"/v1/chats/{uuid.uuid4().hex}"),
        ("POST", "/v1/answers"),
    ],
)
async def test_every_chat_endpoint_needs_a_signed_in_member(method: str, path: str) -> None:
    async with await answers_client(FakeGenerator(), await acme_ledger(), FakeClauseStore()) as c:
        response = await c.request(
            method, path, json={"question": QUESTION}, headers={"Authorization": ""}
        )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "chat_id", ["not-a-chat-id", "0" * 32, "A" * 32, uuid.uuid4().hex[:31], uuid.uuid4().hex + "0"]
)
async def test_an_id_that_is_not_a_uuid4_is_a_422_before_anything_is_looked_up(
    chat_id: str,
) -> None:
    """An id the API couldn't have made names no chat, so it isn't looked up."""
    generator = FakeGenerator(reply_of())
    async with await answers_client(
        generator, await acme_ledger(), await golden_clause_store()
    ) as c:
        read = await c.get(f"/v1/chats/{chat_id}")
        asked = await _ask(c, chat_id=chat_id)
    assert (read.status_code, asked[0].status_code) == (422, 422)
    assert generator.calls == []


# ---- one citation builder, and a corrupt citation that reads back unreadable (#232) --


def _without_text(citation: dict[str, Any]) -> dict[str, Any]:  # Any: JSON
    """The answer event's citation less the clause text, which a chat doesn't repeat."""
    return {k: v for k, v in citation.items() if k != "canonical_text"}


async def test_a_citation_is_the_same_in_the_answer_event_and_the_chat_read_back(
    quote: str,
) -> None:
    """One builder makes both (`answers.citation_json`), so the two can't
    drift: what a chat shows of a citation is what the answer showed, less the
    clause text."""
    ledger = await acme_ledger()
    generator = FakeGenerator(reply_of((APP11, quote)))
    async with await answers_client(generator, ledger, await golden_clause_store()) as c:
        _, events = await _ask(c)
        read = (await c.get(f"/v1/chats/{_chat_id(events)}")).json()
    shown = dict(events)["answer"]["citations"]
    assert shown and "canonical_text" in shown[0]
    assert [_without_text(c) for c in shown] == read["turns"][1]["citations"]


async def _chat_with_stored_citations(
    ledger: FakeLedgerStore,
    chat_id: str,
    citations: tuple[Any, ...],  # Any: citations
) -> None:
    """A verified answer's turn with `citations` in the ledger as stored. The
    fake keeps the turns it is given, so it can hold what a corrupt row reads
    back as; the real ledger's rows are corrupted in `test_postgres_ledger_store`."""
    await ledger.record_chat_exchange(
        ChatExchange(
            tenant_id=TENANT,
            chat_id=chat_id,
            user_id=UID,
            question=QUESTION,
            answer=ChatAnswer(TurnOutcome.VERIFIED, ANSWER, (ChatCitation(APP11, LAW_VERSION),)),
            as_of=dt.date.fromisoformat(AS_OF),
            trace_id="7" * 32,
        )
    )
    turns = ledger._chats[(TENANT, chat_id)]  # pyright: ignore[reportPrivateUsage]
    turns[1] = replace(turns[1], citations=citations)


async def test_an_unreadable_citation_stays_in_place_marked_and_the_chat_is_a_200() -> None:
    ledger = await acme_ledger()
    chat_id = uuid.uuid4().hex
    s6da = "privacy-act-1988:C2026C00227:s6da/2"
    await _chat_with_stored_citations(
        ledger,
        chat_id,
        (
            ChatCitation(APP11, LAW_VERSION),
            UnreadableCitation(UnreadableReason.BAD_INCOMPLETE, s6da, LAW_VERSION),
            UnreadableCitation(UnreadableReason.NOT_AN_OBJECT),
            ChatCitation(APP11, LAW_VERSION),
        ),
    )
    async with await answers_client(FakeGenerator(), ledger, FakeClauseStore()) as c:
        response = await c.get(f"/v1/chats/{chat_id}")
        listed = await c.get("/v1/chats")
    assert response.status_code == 200 and listed.status_code == 200
    question, answer = response.json()["turns"]
    assert question["text"] == QUESTION
    assert answer["verified"] is True and answer["text"] == ANSWER
    readable: dict[str, Any] = {  # Any: JSON
        "clause_id": APP11,
        "cite": "APP 11.1",
        "version_id": LAW_VERSION,
        "incomplete": [],
    }
    assert answer["citations"] == [
        readable,
        # Named, since its clause id could be read, and marked; no `incomplete`:
        # what the member was shown with it is lost.
        {
            "unreadable": True,
            "reason": "bad_incomplete",
            "clause_id": s6da,
            "cite": "s 6DA(2)",
            "version_id": LAW_VERSION,
        },
        {"unreadable": True, "reason": "not_an_object"},
        readable,
    ]
    # Nothing is dropped, so the readable ones are all still there and the
    # turn's own `incomplete` list is built from them alone.
    assert answer["incomplete"] == []


async def test_an_unreadable_citation_names_a_clause_it_could_read_and_nothing_else() -> None:
    ledger = await acme_ledger()
    chat_id = uuid.uuid4().hex
    await _chat_with_stored_citations(
        ledger, chat_id, (UnreadableCitation(UnreadableReason.WRONG_VERSION, APP11),)
    )
    async with await answers_client(FakeGenerator(), ledger, FakeClauseStore()) as c:
        read = (await c.get(f"/v1/chats/{chat_id}")).json()
    assert read["turns"][1]["citations"] == [
        {"unreadable": True, "reason": "wrong_version", "clause_id": APP11, "cite": "APP 11.1"}
    ]


# ---- a corrupt blocked element reads back unreadable too (#234) ---------------------


async def test_an_unreadable_blocked_element_stays_in_place_marked_and_the_chat_is_a_200() -> None:
    ledger = await acme_ledger()
    chat_id = uuid.uuid4().hex
    s6da = "privacy-act-1988:C2026C00227:s6da/2"
    await ledger.record_chat_exchange(
        ChatExchange(
            tenant_id=TENANT,
            chat_id=chat_id,
            user_id=UID,
            question=QUESTION,
            answer=ChatAnswer(TurnOutcome.BLOCKED, blocked=(s6da,)),
            as_of=dt.date.fromisoformat(AS_OF),
            trace_id="7" * 32,
        )
    )
    turns = ledger._chats[(TENANT, chat_id)]  # pyright: ignore[reportPrivateUsage]
    turns[1] = replace(
        turns[1], blocked=(s6da, UnreadableCitation(UnreadableReason.NO_CLAUSE), APP11)
    )
    async with await answers_client(FakeGenerator(), ledger, FakeClauseStore()) as c:
        response = await c.get(f"/v1/chats/{chat_id}")
        listed = await c.get("/v1/chats")
    assert response.status_code == 200 and listed.status_code == 200
    question, answer = response.json()["turns"]
    assert question["text"] == QUESTION
    assert answer["verified"] is False and answer["withheld"] == answers.BLOCKED
    assert answer["blocked"] == [
        {"clause_id": s6da, "cite": "s 6DA(2)"},
        {"unreadable": True, "reason": "no_clause"},
        {"clause_id": APP11, "cite": "APP 11.1"},
    ]


# ---- the real ledger -----------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "")


@pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set — needs a migrated ledger")
async def test_a_chat_is_recorded_and_read_back_through_the_api_on_postgres(quote: str) -> None:
    """The whole path on the real ledger: the pipeline's usage rows and the
    verifier's version checks, then the exchange in one transaction, then the
    read-back of a withheld answer beside a verified one."""
    import psycopg

    from tests.test_postgres_ledger_store import empty_ledger

    postgres = await empty_ledger()
    try:
        clauses = await golden_clause_store()
        await postgres.put_profile(Profile(POLICY_TENANT_ID))
        for tenant_id, instrument_id, version_id in sorted(
            {(c.tenant_id, c.instrument_id, c.version_id) for c in golden_clauses()}
        ):
            await postgres.record_version(
                tenant_id=tenant_id,
                instrument_id=instrument_id,
                version_id=version_id,
                valid_from=GOLDEN_VALID_FROM,
                supersedes=None,
                document_class=DocumentClass.LIBRARY_LAW
                if tenant_id == PUBLIC_TENANT_ID
                else DocumentClass.INTERNAL_POLICY,
            )
        await postgres.subscribe(
            tenant_id=POLICY_TENANT_ID, instrument_id="privacy-act-1988", subscribed_by="setup"
        )
        for evidence in GA05.evidence():
            await postgres.record_evidence(replace(evidence, trace_id="5e7" + "0" * 29))
        good = FakeGenerator(reply_of((APP11, quote)))
        prose = f'Section 6C(2) says "{SECRET_QUOTE}".'
        bad = FakeGenerator(reply_of((APP11, quote), (NOT_RETRIEVED, SECRET_QUOTE), answer=prose))

        def app_with(generator: FakeGenerator) -> httpx.AsyncClient:
            services = fake_api_services(
                settings=api_settings(),
                ledger=postgres,
                clauses=clauses,
                graph=fake_for(
                    load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))),
                    load_applicability_set(),
                ),
                pipeline=AssessmentPipeline(
                    RetrievalPipeline(
                        FakeRetrievalStore(golden_chunks(), ledger=postgres), FakeReranker()
                    ),
                    generator,
                    Verifier(clauses, postgres, CanonicalCitationVerifier()),
                    postgres,
                    tenant_text=TenantTextGate.from_settings(api_settings()),
                ),
            )
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(services)),
                base_url="http://api",
                headers=bearer(mint()),
            )

        async with app_with(good) as c:
            first, events = await _ask(c)
            chat_id = _chat_id(events)
        async with app_with(bad) as c:
            second, withheld = await _ask(c, question=FOLLOW_UP, chat_id=chat_id)
            assert _chat_id(withheld) == chat_id
            read = (await c.get(f"/v1/chats/{chat_id}")).json()
            listed = (await c.get("/v1/chats")).json()["chats"]
            other = await c.get(f"/v1/chats/{chat_id}", headers=bearer(COLLEAGUE))
        assert other.status_code == 404
        assert [t["turn_index"] for t in read["turns"]] == [0, 1, 2, 3]
        assert read["turns"][1]["citations"] == [
            {"clause_id": APP11, "cite": "APP 11.1", "version_id": LAW_VERSION, "incomplete": []}
        ]
        assert read["turns"][3]["text"] is None
        assert read["turns"][3]["withheld"] == answers.BLOCKED
        assert [(c["chat_id"], c["turns"]) for c in listed] == [(chat_id, 4)]
        assert SECRET_QUOTE not in str(read)
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            cursor = await conn.execute(
                "SELECT turn_index, user_id, role, content IS NULL, outcome, trace_id FROM chat "
                "WHERE tenant_id = %s AND chat_id = %s ORDER BY turn_index",
                (TENANT, chat_id),
            )
            rows = await cursor.fetchall()
            cursor = await conn.execute(
                "SELECT count(*) FROM chat WHERE content LIKE %s OR content LIKE %s",
                (f"%{SECRET_QUOTE}%", "%6C(2)%"),
            )
            leaked = await cursor.fetchone()
        one, two = first.headers["x-trace-id"], second.headers["x-trace-id"]
        assert rows == [
            (0, UID, "user", False, None, one),
            (1, UID, "assistant", False, "verified", one),
            (2, UID, "user", False, None, two),
            (3, UID, "assistant", True, "blocked", two),
        ]
        assert leaked == (0,)
    finally:
        await empty_ledger()


@pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set — needs a migrated ledger")
async def test_a_corrupt_citation_row_reads_back_unreadable_through_the_api_on_postgres() -> None:
    """#232 on the real ledger: a row with a malformed `citations` element is a
    200 with that citation marked, not a 500."""
    import json

    import psycopg

    from tests.test_postgres_ledger_store import empty_ledger

    postgres = await empty_ledger()
    try:
        await postgres.put_profile(Profile(TENANT))
        chat_id = uuid.uuid4().hex
        await postgres.record_chat_exchange(
            ChatExchange(
                tenant_id=TENANT,
                chat_id=chat_id,
                user_id=UID,
                question=QUESTION,
                answer=ChatAnswer(
                    TurnOutcome.VERIFIED, ANSWER, (ChatCitation(APP11, LAW_VERSION),)
                ),
                as_of=dt.date.fromisoformat(AS_OF),
                trace_id="7" * 32,
            )
        )
        good = {"clause_id": APP11, "version_id": LAW_VERSION}
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            await conn.execute(
                "UPDATE chat SET citations = %s WHERE turn_index = 1",
                (json.dumps([good, {"clause_id": APP11}, "SECRET", good]),),
            )
            await conn.commit()
        async with await answers_client(FakeGenerator(), postgres, FakeClauseStore()) as c:
            response = await c.get(f"/v1/chats/{chat_id}")
        assert response.status_code == 200
        cited = response.json()["turns"][1]["citations"]
        assert [c.get("unreadable", False) for c in cited] == [False, True, True, False]
        assert cited[1] == {
            "unreadable": True,
            "reason": "wrong_version",
            "clause_id": APP11,
            "cite": "APP 11.1",
        }
        assert cited[2] == {"unreadable": True, "reason": "not_an_object"}
        assert "SECRET" not in response.text
    finally:
        await empty_ledger()


@pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set — needs a migrated ledger")
async def test_a_corrupt_blocked_row_reads_back_unreadable_through_the_api_on_postgres() -> None:
    """#234 on the real ledger: a row with a malformed `blocked` element is a
    200 with that entry marked, not a 500, and everything else intact."""
    import json

    import psycopg

    from tests.test_postgres_ledger_store import empty_ledger

    postgres = await empty_ledger()
    try:
        await postgres.put_profile(Profile(TENANT))
        chat_id = uuid.uuid4().hex
        await postgres.record_chat_exchange(
            ChatExchange(
                tenant_id=TENANT,
                chat_id=chat_id,
                user_id=UID,
                question=QUESTION,
                answer=ChatAnswer(TurnOutcome.BLOCKED, blocked=(APP11,)),
                as_of=dt.date.fromisoformat(AS_OF),
                trace_id="7" * 32,
            )
        )
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            await conn.execute(
                "UPDATE chat SET blocked = %s WHERE turn_index = 1",
                (json.dumps([APP11, "SECRET", 7, APP11 + "\n"]),),
            )
            await conn.commit()
        async with await answers_client(FakeGenerator(), postgres, FakeClauseStore()) as c:
            response = await c.get(f"/v1/chats/{chat_id}")
            listed = await c.get("/v1/chats")
        assert response.status_code == 200 and listed.status_code == 200
        question, answer = response.json()["turns"]
        assert question["text"] == QUESTION
        assert answer["withheld"] == answers.BLOCKED and answer["text"] is None
        unreadable = {"unreadable": True, "reason": "no_clause"}
        assert answer["blocked"] == [
            {"clause_id": APP11, "cite": "APP 11.1"},
            unreadable,
            unreadable,
            unreadable,
        ]
        assert "SECRET" not in response.text
    finally:
        await empty_ledger()
