"""Asking a question through the API (issue #149): Track A's scope, the
tenant's policies as the cached prefix, `answer()`, streamed as server-sent
events of stages and the verified result.

Everything is real but the generator: the signed Privacy Act mapping on the
fake graph, the golden corpus (law and Acme's six policies) in the fake
retrieval store and clause store, the real verifier, and a ledger with Acme
registered, subscribed to the Act and holding GA-05's facts (the APPs apply).
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import (
    FakeClauseStore,
    FakeGenerator,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.generation_residency import GenerationResidencyError, TenantTextGate
from compliance_rag.adapters.interfaces import LawScope, LedgerStore, Profile
from compliance_rag.api import answers
from compliance_rag.api.app import create_app
from compliance_rag.api.main import GeneratorAtStartup
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.corpus.models import Chunk, Scope
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.eval.applicability import load_applicability_set
from compliance_rag.eval.golden_corpus import POLICY_TENANT_ID, golden_chunks, golden_clauses
from compliance_rag.eval.harness import golden_clause_store, golden_ledger
from compliance_rag.graph.mappings import load_mapping_set
from compliance_rag.retrieval.pipeline import RetrievalPipeline, RetrievalTrace
from compliance_rag.settings import BUILD_PHASE_OFFSHORE_REGION, Settings
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier
from tests.api_support import MEMBER, api_settings, bearer, fake_api_services, mint
from tests.test_applicability_gate import CORPUS_MAPPINGS, fake_for

AS_OF = "2026-10-12"
QUESTION = "What must Acme do to protect the personal information it holds from misuse and loss?"
APP11 = "privacy-act-1988:C2026C00227:sch1/app11/1"
NOT_RETRIEVED = "privacy-act-1988:C2026C00227:s6c/2"
SECRET_QUOTE = "a sentence the model made up that must never reach a client"
GA05 = next(c for c in load_applicability_set().cases if c.id == "GA-05")


def _canonical(clause_id: str) -> str:
    return next(c.canonical_text for c in golden_clauses() if c.clause_id == clause_id)


def _reply(*citations: tuple[str, str], answer: str = "Acme must take reasonable steps.") -> str:
    return json.dumps(
        {"answer": answer, "citations": [{"clause_id": c, "quote": q} for c, q in citations]}
    )


async def _ledger() -> FakeLedgerStore:
    ledger = await golden_ledger()
    await ledger.put_profile(Profile(POLICY_TENANT_ID))
    await ledger.subscribe(
        tenant_id=POLICY_TENANT_ID, instrument_id="privacy-act-1988", subscribed_by="setup"
    )
    for evidence in GA05.evidence():  # recorded as an earlier request would have
        await ledger.record_evidence(replace(evidence, trace_id="5e7" + "0" * 29))
    return ledger


async def _client(
    generator: FakeGenerator,
    ledger: LedgerStore,
    clauses: FakeClauseStore,
    store: FakeRetrievalStore | None = None,
    settings: Settings | None = None,
) -> httpx.AsyncClient:
    labels = load_applicability_set()
    services = fake_api_services(
        settings=settings or api_settings(),
        ledger=ledger,
        clauses=clauses,
        graph=fake_for(load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), labels),
        pipeline=AssessmentPipeline(
            RetrievalPipeline(
                store or FakeRetrievalStore(golden_chunks(), ledger=ledger), FakeReranker()
            ),
            generator,
            Verifier(clauses, ledger, CanonicalCitationVerifier()),
            ledger,
            tenant_text=TenantTextGate.from_settings(settings or api_settings()),
        ),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)),
        base_url="http://api",
        headers=bearer(mint()),
    )


def _events(stream: str) -> list[tuple[str, dict[str, Any]]]:  # Any: JSON
    events: list[tuple[str, dict[str, Any]]] = []  # Any: JSON
    for block in stream.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


async def _ask(
    generator: FakeGenerator,
    *,
    ledger: FakeLedgerStore | None = None,
    clauses: FakeClauseStore | None = None,
    store: FakeRetrievalStore | None = None,
    settings: Settings | None = None,
    **body: Any,  # Any: JSON
) -> tuple[httpx.Response, list[tuple[str, dict[str, Any]]]]:  # Any: JSON
    clauses = clauses or await golden_clause_store()
    async with await _client(generator, ledger or await _ledger(), clauses, store, settings) as c:
        response = await c.post("/v1/answers", json={"question": QUESTION, "as_of": AS_OF, **body})
    return response, _events(response.text) if response.status_code == 200 else []


@pytest.fixture
def quote() -> str:
    return _canonical(APP11)[:60]


async def test_a_verified_answer_streams_its_stages_and_the_canonical_clause(quote: str) -> None:
    response, events = await _ask(FakeGenerator(_reply((APP11, quote))))
    assert response.headers["content-type"].startswith("text/event-stream")
    assert [name for name, _ in events] == ["coverage", "retrieval", "answer"]
    coverage, retrieval, answer = (data for _, data in events)
    assert coverage == {
        "chat_id": answer["chat_id"],  # every event names the chat (#162)
        "as_of": AS_OF,
        "counts": {"applicable": 35, "not_applicable": 2, "unassessable": 0},
    }
    assert retrieval["candidates"] > 0
    assert answer["verified"] is True and answer["blocked"] == []
    assert answer["text"] == "Acme must take reasonable steps."
    [citation] = answer["citations"]
    assert citation["clause_id"] == APP11 and citation["cite"] == "APP 11.1"
    # What a client is shown is the clause's text, not the model's quote of it.
    assert citation["canonical_text"] == _canonical(APP11) != quote
    assert answer["trace_id"] == response.headers["x-trace-id"]


async def test_a_bad_citation_blocks_the_whole_answer_and_nothing_it_wrote_is_sent(
    quote: str,
) -> None:
    """ADR-0003: a bad citation blocks the answer rather than shipping it.
    The prose repeats the refused quote, as a model's prose can, so sending
    the prose would send the quote."""
    prose = f'Section 6C(2) says "{SECRET_QUOTE}".'
    generator = FakeGenerator(_reply((APP11, quote), (NOT_RETRIEVED, SECRET_QUOTE), answer=prose))
    response, events = await _ask(generator)
    answer = dict(events)["answer"]
    assert answer["verified"] is False and answer["text"] is None
    assert answer["citations"] == []
    assert answer["withheld"] == answers.BLOCKED
    assert [b["clause_id"] for b in answer["blocked"]] == [NOT_RETRIEVED]
    assert SECRET_QUOTE not in response.text and "Section 6C(2)" not in response.text


async def test_an_answer_citing_nothing_is_withheld() -> None:
    _, events = await _ask(FakeGenerator(_reply(answer="Acme is probably fine.")))
    answer = dict(events)["answer"]
    assert answer["verified"] is False and answer["text"] is None
    assert answer["withheld"] == answers.UNCITED


async def test_a_malformed_generation_ends_in_an_error_and_no_answer() -> None:
    response, events = await _ask(FakeGenerator("this is not the JSON answer the schema needs"))
    assert [name for name, _ in events] == ["coverage", "retrieval", "error"]
    assert events[-1][1]["error"] == "malformed_generation"
    assert "this is not the JSON" not in response.text


async def test_the_tenants_policies_are_the_cached_prefix_and_a_withdrawn_one_is_nowhere(
    quote: str,
) -> None:
    policy = "acme/privacy-policy"
    texts = [c.canonical_text for c in golden_clauses() if c.instrument_id == policy]
    generator = FakeGenerator(_reply((APP11, quote)))
    await _ask(generator)
    assert all(t in str(generator.calls[0]["cached_prefix"]) for t in texts)

    # Withdrawn the way a member would: through CorpusLifecycle (ADR-0021),
    # which takes it out of the ledger's policies and out of retrieval.
    ledger, clauses = await _ledger(), await golden_clause_store()
    store = FakeRetrievalStore(golden_chunks(), ledger=ledger)
    lifecycle = CorpusLifecycle(
        ledger=ledger,
        retrieval=store,
        clauses=clauses,
        generation_region=BUILD_PHASE_OFFSHORE_REGION,
        synthetic_registry=SyntheticRegistry.load(),
    )
    await lifecycle.withdraw(
        tenant_id=POLICY_TENANT_ID,
        instrument_id=policy,
        version_id="v1",
        actor="lead@acme.example",
        reason="superseded by the new privacy policy",
    )
    again = FakeGenerator(_reply((APP11, quote)))
    await _ask(again, ledger=ledger, clauses=clauses, store=store)
    sent = str(again.calls[0]["cached_prefix"]) + str(again.calls[0]["user"])
    assert not any(t in sent for t in texts)
    assert f"{policy}:v1:" not in sent


D2_3_1 = "acme/data-security-policy:v1:3/1"
D2_3_1_CITED = "Acme Pty Ltd — Data Security and Retention Policy (Internal), 3.1"
"""The title recorded with the version (the fixture's front matter, as the
operator records it) and the clause's number as the policy writes it."""


async def test_a_policy_citation_is_named_by_its_title_in_the_event_the_chat_and_the_fence(
    quote: str,
) -> None:
    """One form everywhere (#239, ADR-0049): the model's fence, the answer
    event and the chat read back all cite D2 3/1 as its document's title and
    "3.1", and law as before. The titles are read once for the answer and
    once for the read-back, however many policy clauses are in the context
    (six policies' worth here) or cited."""
    ledger = await _ledger()
    clauses = await golden_clause_store()
    d2 = _canonical(D2_3_1)[:50]
    generator = FakeGenerator(_reply((APP11, quote), (D2_3_1, d2)))
    async with await _client(generator, ledger, clauses) as c:
        before = ledger.title_reads
        response = await c.post("/v1/answers", json={"question": QUESTION, "as_of": AS_OF})
        assert ledger.title_reads == before + 1
        answer = dict(_events(response.text))["answer"]
        assert answer["verified"] is True
        assert {x["clause_id"]: x["cite"] for x in answer["citations"]} == {
            APP11: "APP 11.1",
            D2_3_1: D2_3_1_CITED,
        }
        read = (await c.get(f"/v1/chats/{answer['chat_id']}")).json()
        assert ledger.title_reads == before + 2
    shown = read["turns"][1]["citations"]
    assert {x["clause_id"]: x["cite"] for x in shown} == {APP11: "APP 11.1", D2_3_1: D2_3_1_CITED}
    sent = generator.calls[0]
    assert f'clause {D2_3_1} cited as "{D2_3_1_CITED}"]' in str(sent["cached_prefix"])
    assert f'clause {APP11} cited as "APP 11.1"' in str(sent["user"])
    # No policy clause reaches the model as its bare path.
    assert 'cited as "3.1"' not in str(sent["cached_prefix"])


async def test_a_blocked_policy_citation_reads_back_by_its_title_too(quote: str) -> None:
    """A blocked clause is named in the read-back with the same form, from the
    same one read; one whose version the ledger never recorded is its number
    alone, never a title made from its id."""
    invented = "acme/made-up-policy:v1:2/4"
    ledger = await _ledger()
    generator = FakeGenerator(
        _reply((APP11, quote), (D2_3_1, SECRET_QUOTE), (invented, SECRET_QUOTE))
    )
    async with await _client(generator, ledger, await golden_clause_store()) as c:
        response = await c.post("/v1/answers", json={"question": QUESTION, "as_of": AS_OF})
        chat_id = dict(_events(response.text))["answer"]["chat_id"]
        read = (await c.get(f"/v1/chats/{chat_id}")).json()
    assert {b["clause_id"]: b["cite"] for b in read["turns"][1]["blocked"]} == {
        D2_3_1: D2_3_1_CITED,
        invented: "2.4",
    }


async def test_the_requests_usage_rows_carry_its_trace(quote: str) -> None:
    ledger = await _ledger()
    response, _ = await _ask(FakeGenerator(_reply((APP11, quote))), ledger=ledger)
    usage = await ledger.usage(POLICY_TENANT_ID)
    assert usage and {u.trace_id for u in usage} == {response.headers["x-trace-id"]}


async def test_retrieval_is_reported_before_generation_starts() -> None:
    """`on_retrieved` runs between retrieval and the model, so the stream's
    `retrieval` event is progress, not a replay."""
    ledger = await _ledger()
    generator = FakeGenerator(_reply())
    pipeline = AssessmentPipeline(
        RetrievalPipeline(FakeRetrievalStore(golden_chunks(), ledger=ledger), FakeReranker()),
        generator,
        Verifier(await golden_clause_store(), ledger, CanonicalCitationVerifier()),
        ledger,
        tenant_text=TenantTextGate.from_settings(api_settings()),
    )
    seen: list[int] = []

    async def retrieved(trace: RetrievalTrace) -> None:
        seen.append(len(generator.calls))
        assert trace.ranked_candidates()

    await pipeline.answer(
        QUESTION,
        POLICY_TENANT_ID,
        dt.date(2026, 10, 12),
        law=LawScope.unscoped("a pipeline test"),
        on_retrieved=retrieved,
    )
    assert seen == [0] and len(generator.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"question": ""}, id="empty"),
        pytest.param({"question": "   "}, id="blank"),
        pytest.param({"question": "x" * 2001}, id="too-long"),
        pytest.param({"question": "a\x00b"}, id="nul"),
        pytest.param({"as_of": "9999-07-01"}, id="date-out-of-range"),
        pytest.param({"tenant_id": "other-tenant"}, id="names-a-tenant"),
        pytest.param({"law": "unscoped"}, id="names-a-scope"),
        pytest.param({"user_id": "uid-someone-else"}, id="names-a-member"),
    ],
)
async def test_an_invalid_question_is_a_422(body: dict[str, Any]) -> None:  # Any: JSON
    response, _ = await _ask(FakeGenerator(_reply()), **body)
    assert response.status_code == 422


async def test_asking_needs_a_signed_in_member() -> None:
    async with await _client(FakeGenerator(), await _ledger(), FakeClauseStore()) as c:
        response = await c.post(
            "/v1/answers", json={"question": QUESTION}, headers={"Authorization": ""}
        )
    assert response.status_code == 401


async def test_law_outside_track_as_scope_never_reaches_the_model() -> None:
    """The ACC's definition is law Track A doesn't cite for Acme. Asked as
    the question, word for word, it still isn't retrieved (D-03, ADR-0030)."""
    outside = "privacy-act-1988:C2026C00227:s6/1/acc"
    generator = FakeGenerator(_reply())
    response, _ = await _ask(generator, question=_canonical(outside))
    assert response.status_code == 200
    assert outside not in str(generator.calls[0]["user"])


# ---- the residency gate, per request (#155, ADR-0032) -----------------------------

OFFSHORE = {"vertex_claude_region": BUILD_PHASE_OFFSHORE_REGION}


async def test_offshore_a_member_who_isnt_a_registered_tester_is_refused_before_the_model() -> None:
    """The question is the one input no store holds and no registry can
    vouch for: a member can type real customer data into it."""
    generator = FakeGenerator(_reply())
    response, events = await _ask(generator, settings=api_settings(**OFFSHORE))
    assert [name for name, _ in events] == ["error"]  # before any work
    error = events[-1][1]
    assert error["error"] == "residency" and "ADR-0032" in error["message"]
    assert generator.calls == []
    assert QUESTION not in response.text


async def test_offshore_a_registered_tester_with_the_synthetic_prefix_is_answered(
    quote: str,
) -> None:
    settings = api_settings(**OFFSHORE, offshore_generation_testers=MEMBER.upper())
    generator = FakeGenerator(_reply((APP11, quote)))
    _, events = await _ask(generator, settings=settings)
    assert [name for name, _ in events] == ["coverage", "retrieval", "answer"]
    assert len(generator.calls) == 1 and generator.calls[0]["cached_prefix"]


async def test_offshore_a_policy_clause_the_registry_doesnt_vouch_for_is_refused_for_a_tester() -> (
    None
):
    """The prefix is read from the bucket, which the startup gate never asks.
    One clause edited away from its registered text is real data (ADR-0026)."""
    clauses = await golden_clause_store()
    edited = next(c for c in golden_clauses() if c.instrument_id == "acme/privacy-policy")
    real = "Acme keeps payroll records for seven years, then shreds them on site."
    await clauses.put_clause(edited.model_copy(update={"canonical_text": real}))
    settings = api_settings(**OFFSHORE, offshore_generation_testers=MEMBER)
    generator = FakeGenerator(_reply())
    response, events = await _ask(generator, clauses=clauses, settings=settings)
    assert [name for name, _ in events] == ["coverage", "error"]
    error = events[-1][1]
    assert error["error"] == "residency" and "CAG prefix" in error["message"]
    assert edited.clause_id in error["message"]
    assert generator.calls == [] and real not in response.text


async def test_in_sydney_any_member_is_answered_over_any_prefix(quote: str) -> None:
    clauses = await golden_clause_store()
    edited = next(c for c in golden_clauses() if c.instrument_id == "acme/privacy-policy")
    await clauses.put_clause(edited.model_copy(update={"canonical_text": "Real, unregistered."}))
    generator = FakeGenerator(_reply((APP11, quote)))
    _, events = await _ask(generator, clauses=clauses, settings=api_settings())
    assert [name for name, _ in events] == ["coverage", "retrieval", "answer"]
    assert "Real, unregistered." in str(generator.calls[0]["cached_prefix"])


# ---- one person's private additions (#171, ADR-0036) ---------------------------------

NOTE_TEXT = "Quokka note: the spare backup keys for the vault are in the blue drawer."
NOTE_QUESTION = "Where are the spare quokka backup keys for the vault kept?"


def _note(owner: str) -> Chunk:
    return Chunk(
        chunk_id="note",
        clause_id="acme/my-notes:v1:1/1",
        text=NOTE_TEXT,
        scope=Scope.USER,
        tenant_id=POLICY_TENANT_ID,
        user_id=owner,
    )


async def _asked_as(token: str, note: Chunk) -> str:
    """What the model was shown for `NOTE_QUESTION` when the member behind
    `token` asked it, with `note` stored."""
    ledger = await _ledger()
    generator = FakeGenerator(_reply())
    store = FakeRetrievalStore((*golden_chunks(), note), ledger=ledger)
    async with await _client(generator, ledger, await golden_clause_store(), store) as c:
        response = await c.post(
            "/v1/answers",
            json={"question": NOTE_QUESTION, "as_of": AS_OF},
            headers=bearer(token),
        )
    assert response.status_code == 200
    return str(generator.calls[0]["user"])


async def test_a_members_private_addition_reaches_their_answers_and_no_one_elses() -> None:
    """The member is the sign-in's uid. Their own note is in front of the model
    when they ask; a second member of the same tenant asking the same question
    is not shown it."""
    owner = mint(sub="uid-owner")
    colleague = mint(sub="uid-colleague", email="second@acme.example")
    note = _note("uid-owner")
    assert NOTE_TEXT in await _asked_as(owner, note)  # the positive control
    assert NOTE_TEXT not in await _asked_as(colleague, note)


async def test_a_private_addition_is_keyed_by_the_uid_not_the_email() -> None:
    """An email address can change hands; the uid a sign-in carries doesn't.
    A note owned by an email address belongs to no one."""
    shown = await _asked_as(mint(sub="uid-owner"), _note(MEMBER))
    assert NOTE_TEXT not in shown


# ---- the generator the real app builds at startup ------------------------------


async def test_the_generator_refuses_to_answer_before_the_app_starts() -> None:
    generator = GeneratorAtStartup(
        api_settings(), corpus=FakeRetrievalStore(ledger=FakeLedgerStore())
    )
    with pytest.raises(RuntimeError, match="starts with the app"):
        await generator.generate(system="s", cached_prefix=None, user="u", max_tokens=1)


def test_a_residency_refusal_at_startup_stops_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate runs in `VertexClaudeGenerator.create` (ADR-0023). When it
    refuses, the app never starts serving."""
    from compliance_rag.adapters import vertex_claude_generator

    async def refused(*_: object, **__: object) -> object:
        raise GenerationResidencyError("offshore generation over real tenant data")

    monkeypatch.setattr(vertex_claude_generator.VertexClaudeGenerator, "create", refused)
    generator = GeneratorAtStartup(
        api_settings(), corpus=FakeRetrievalStore(ledger=FakeLedgerStore())
    )
    app = create_app(fake_api_services(checks=(generator.start,)))
    with pytest.raises(GenerationResidencyError), TestClient(app):
        pass


async def test_once_started_the_generator_is_the_one_the_gate_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from compliance_rag.adapters import vertex_claude_generator

    built = FakeGenerator(_reply())

    async def create(*_: object, **__: object) -> FakeGenerator:
        return built

    monkeypatch.setattr(vertex_claude_generator.VertexClaudeGenerator, "create", create)
    generator = GeneratorAtStartup(
        api_settings(), corpus=FakeRetrievalStore(ledger=FakeLedgerStore())
    )
    await generator.start()
    await generator.generate(system="s", cached_prefix=None, user="u", max_tokens=1)
    assert built.calls and built.calls[0]["user"] == "u"
