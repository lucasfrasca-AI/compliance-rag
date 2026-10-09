"""Tracer bullet: retrieve() and answer() through every fake, one clause (issue #16).

Every later ticket then replaces exactly one fake with a real adapter, and a
break is attributable to that adapter rather than to the composition this
test locks down (`retrieve()` -> rerank -> generate -> verify).
"""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import AsyncMock

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.fakes import (
    FakeCitationVerifier,
    FakeClauseStore,
    FakeGenerator,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.interfaces import Candidate, Channel, ScoredDocument
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.corpus.models import Chunk, ClauseRecord, Scope
from compliance_rag.retrieval.pipeline import RetrievalPipeline, RetrievalTrace
from compliance_rag.verifier.citations import Verifier

TENANT_ID = "acme"
AS_OF = dt.date(2026, 1, 1)
QUERY = "reasonable steps information misuse"


@pytest.fixture
def clause() -> ClauseRecord:
    """The one hand-written clause the whole tracer bullet runs on."""
    return ClauseRecord.build(
        instrument_id="acme/privacy-policy",
        version_id="TEST-FIXTURE-001",
        clause_path="sch1/app11/1",
        text="An APP entity must take such steps as are reasonable in the circumstances "
        "to protect the information from misuse, interference and loss.",
        scope=Scope.TENANT,
        tenant_id=TENANT_ID,
    )


@pytest.fixture
async def assessment(clause: ClauseRecord) -> AssessmentPipeline:
    chunk = Chunk(
        chunk_id="chunk-1",
        clause_id=clause.clause_id,
        text=clause.canonical_text,
        scope=clause.scope,
        tenant_id=clause.tenant_id,
    )
    retrieval = RetrievalPipeline(
        retrieval_store=FakeRetrievalStore((chunk,), ledger=FakeLedgerStore()),
        reranker=FakeReranker(),
    )
    clause_store = FakeClauseStore()
    await clause_store.put_clause(clause)
    ledger = FakeLedgerStore()
    await ledger.record_version(
        tenant_id=TENANT_ID,
        instrument_id=clause.instrument_id,
        version_id=clause.version_id,
        valid_from=dt.date(2025, 1, 1),
        supersedes=None,
    )
    reply = json.dumps(
        {
            "answer": "Take reasonable steps to protect it.",
            "citations": [
                {"clause_id": clause.clause_id, "quote": "reasonable in the circumstances"}
            ],
        }
    )
    return AssessmentPipeline(
        retrieval=retrieval,
        generator=FakeGenerator(reply=reply),
        verifier=Verifier(clause_store, ledger, FakeCitationVerifier()),
        ledger=ledger,
    )


def test_ranked_candidates_reorders_fused_by_rerank_score() -> None:
    """The single-clause fixture can't reveal a reordering bug — this proves
    `ranked_candidates()`/`top_candidate()` follow rerank order, not fused
    order, independent of the fakes."""
    first_fused = Candidate(chunk_id="a", clause_id="x:v:1", text="A", score=1.0)
    second_fused = Candidate(chunk_id="b", clause_id="x:v:2", text="B", score=0.5)
    trace = RetrievalTrace(
        per_channel={},
        fused=(first_fused, second_fused),
        reranked=(ScoredDocument(index=1, score=0.9), ScoredDocument(index=0, score=0.1)),
    )
    assert trace.ranked_candidates() == (second_fused, first_fused)
    assert trace.top_candidate() == second_fused


def test_top_candidate_is_none_when_nothing_was_retrieved() -> None:
    trace = RetrievalTrace(per_channel={}, fused=(), reranked=())
    assert trace.top_candidate() is None


async def test_retrieve_returns_channels_fused_and_rerank_scores(
    assessment: AssessmentPipeline,
) -> None:
    result = await assessment.retrieval.retrieve(QUERY, TENANT_ID, AS_OF)
    assert Channel.DENSE in result.per_channel
    assert Channel.SPARSE in result.per_channel
    assert result.fused
    assert result.reranked
    assert result.reranked[0].score > 0


async def test_answer_composes_retrieve_generate_and_verify(
    assessment: AssessmentPipeline,
) -> None:
    result = await assessment.answer(QUERY, TENANT_ID, AS_OF)
    assert result.retrieval.fused
    assert result.generation.text
    assert result.verified is True


async def test_answer_calls_retrieve_rather_than_duplicating_the_pipeline(
    assessment: AssessmentPipeline,
) -> None:
    """The pipeline's own retrieve() must run, not a parallel reimplementation
    of search + rerank inlined into answer()."""
    spy = AsyncMock(wraps=assessment.retrieval.retrieve)
    assessment.retrieval.retrieve = spy  # type: ignore[method-assign]

    await assessment.answer(QUERY, TENANT_ID, AS_OF)

    spy.assert_awaited_once_with(QUERY, TENANT_ID, AS_OF)


async def test_answer_runs_with_fakes_only(assessment: AssessmentPipeline) -> None:
    """No network, no credentials, no billing — every dependency is a fake."""
    for adapter in (
        assessment.retrieval.retrieval_store,
        assessment.retrieval.reranker,
        assessment.generator,
    ):
        assert type(adapter).__name__.startswith("Fake")


async def test_spans_are_emitted_at_every_stage_with_required_attributes(
    assessment: AssessmentPipeline, span_exporter: InMemorySpanExporter
) -> None:
    """CLAUDE.md: every function touching an LLM or a retriever emits a span
    with structured attributes — proving the trace shape, not just that the
    pipeline runs."""
    await assessment.answer(QUERY, TENANT_ID, AS_OF)

    spans = {s.name: s for s in span_exporter.get_finished_spans()}
    assert "retrieve" in spans
    assert "answer" in spans

    retrieve_attrs = spans["retrieve"].attributes or {}
    assert retrieve_attrs.get("query") == QUERY
    assert any(str(k).startswith("retrieval.channel.") for k in retrieve_attrs)
    assert "retrieval.fused" in retrieve_attrs
    assert "retrieval.rerank_scores" in retrieve_attrs

    answer_attrs = spans["answer"].attributes or {}
    assert "generation.cached_prefix_hit" in answer_attrs
    assert "generation.input_tokens" in answer_attrs
    assert "generation.output_tokens" in answer_attrs
    assert "generation.cost_usd" in answer_attrs
    assert answer_attrs.get("verifier.result") is True

    # retrieve()'s span nests inside answer()'s — one trace, not two.
    retrieve_parent = spans["retrieve"].parent
    answer_context = spans["answer"].context
    assert retrieve_parent is not None
    assert answer_context is not None
    assert retrieve_parent.span_id == answer_context.span_id
