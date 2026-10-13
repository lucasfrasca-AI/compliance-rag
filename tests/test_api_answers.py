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

from compliance_rag.adapters.fakes import (
    FakeClauseStore,
    FakeGenerator,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.interfaces import LawScope, Profile
from compliance_rag.api.app import create_app
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.eval.applicability import load_applicability_set
from compliance_rag.eval.golden_corpus import POLICY_TENANT_ID, golden_chunks, golden_clauses
from compliance_rag.eval.harness import golden_clause_store, golden_ledger
from compliance_rag.graph.mappings import load_mapping_set
from compliance_rag.retrieval.pipeline import RetrievalPipeline, RetrievalTrace
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier
from tests.api_support import bearer, fake_api_services, mint
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
    generator: FakeGenerator, ledger: FakeLedgerStore, clauses: FakeClauseStore
) -> httpx.AsyncClient:
    labels = load_applicability_set()
    services = fake_api_services(
        ledger=ledger,
        clauses=clauses,
        graph=fake_for(load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), labels),
        answers=AssessmentPipeline(
            RetrievalPipeline(FakeRetrievalStore(golden_chunks(), ledger=ledger), FakeReranker()),
            generator,
            Verifier(clauses, ledger, CanonicalCitationVerifier()),
            ledger,
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
    **body: Any,  # Any: JSON
) -> tuple[httpx.Response, list[tuple[str, dict[str, Any]]]]:  # Any: JSON
    clauses = await golden_clause_store()
    async with await _client(generator, ledger or await _ledger(), clauses) as c:
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
        "as_of": AS_OF,
        "counts": {"applicable": 35, "not_applicable": 2, "unassessable": 0},
    }
    assert retrieval["candidates"] > 0
    assert answer["verified"] is True and answer["blocked"] == []
    [citation] = answer["citations"]
    assert citation["clause_id"] == APP11 and citation["cite"] == "APP 11.1"
    # What a client is shown is the clause's text, not the model's quote of it.
    assert citation["canonical_text"] == _canonical(APP11) != quote
    assert answer["trace_id"] == response.headers["x-trace-id"]


async def test_a_citation_never_retrieved_is_blocked_and_its_quote_never_sent(quote: str) -> None:
    generator = FakeGenerator(_reply((APP11, quote), (NOT_RETRIEVED, SECRET_QUOTE)))
    response, events = await _ask(generator)
    answer = dict(events)["answer"]
    assert answer["verified"] is False
    assert [c["clause_id"] for c in answer["citations"]] == [APP11]
    assert [b["clause_id"] for b in answer["blocked"]] == [NOT_RETRIEVED]
    assert SECRET_QUOTE not in response.text


async def test_a_malformed_generation_ends_in_an_error_and_no_answer() -> None:
    response, events = await _ask(FakeGenerator("this is not the JSON answer the schema needs"))
    assert [name for name, _ in events] == ["coverage", "retrieval", "error"]
    assert events[-1][1]["error"] == "malformed_generation"
    assert "this is not the JSON" not in response.text


async def test_the_tenants_policies_are_the_cached_prefix_and_a_withdrawn_one_isnt(
    quote: str,
) -> None:
    generator = FakeGenerator(_reply((APP11, quote)))
    await _ask(generator)
    prefix = str(generator.calls[0]["cached_prefix"])
    policy_text = next(
        c.canonical_text for c in golden_clauses() if c.instrument_id == "acme/privacy-policy"
    )
    assert policy_text in prefix

    ledger = await _ledger()
    await ledger.mark_withdrawn(
        tenant_id=POLICY_TENANT_ID,
        instrument_id="acme/privacy-policy",
        version_id="v1",
        withdrawn_by="lead@acme.example",
    )
    again = FakeGenerator(_reply((APP11, quote)))
    await _ask(again, ledger=ledger)
    assert policy_text not in str(again.calls[0]["cached_prefix"])


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
