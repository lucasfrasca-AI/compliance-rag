"""The router's fixed decision (`CONTEXT.md`: Router, D-12, D-04).

A clause-ID query is routed to `RetrievalStore.lookup_clause` on the keyword
channel and skips the reranker entirely — a cross-encoder can only degrade a
query that already has one exact answer (D-04). Everything else runs the
full hybrid pipeline. Issue #5 adds the tenant-prefixed case: a policy
clause id (`acme/hr-policy:v3:sec2.1`) must route exactly like a public one.

Issue #5 asks for "golden-set cases" — but the golden-set YAML harness
(`CONTEXT.md`: Golden set, `corpus/golden/`) is issue #27, not yet built, and
its entries require human `author`/`reviewed_by`/`date` front matter this
ticket has no mechanism to produce. These plain pytest cases are the interim
substitute; #27 should add golden-set coverage alongside them, not instead
of them — the router's own decision is cheap to unit-test directly and
doesn't need a labelled query/profile to prove.
"""

from __future__ import annotations

import datetime as dt

import pytest

from compliance_rag.adapters.fakes import FakeReranker, FakeRetrievalStore
from compliance_rag.corpus.models import Chunk, Scope
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.retrieval.router import is_clause_id_query

PUBLIC_ID = "privacy-act-1988:C2024C00276:sch1/app11/1"
TENANT_ID = "acme/hr-policy:v3:sec2.1"
TODAY = dt.date(2026, 1, 1)


@pytest.mark.parametrize("query", [PUBLIC_ID, TENANT_ID])
def test_regex_matches_public_and_tenant_prefixed_stable_ids(query: str) -> None:
    assert is_clause_id_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "See privacy-act-1988:C2024C00276:sch1/app11/1 for details",
        "what does APP 11.1 require",
        f"  {PUBLIC_ID} trailing prose",
    ],
    ids=["mid-sentence", "prose-display-form", "trailing-prose"],
)
def test_prose_mentioning_a_clause_id_does_not_match(query: str) -> None:
    """The regex is anchored to the whole query, not a search inside it —
    a near-miss must fall through to the full pipeline, never short-circuit
    to a single lookup."""
    assert not is_clause_id_query(query)


def test_a_bare_clause_id_with_surrounding_whitespace_still_matches() -> None:
    assert is_clause_id_query(f"  {PUBLIC_ID}  ")


@pytest.fixture
def public_chunk() -> Chunk:
    return Chunk(
        chunk_id="c1", clause_id=PUBLIC_ID, text="take reasonable steps", scope=Scope.PUBLIC
    )


@pytest.fixture
def tenant_chunk() -> Chunk:
    return Chunk(
        chunk_id="c2",
        clause_id=TENANT_ID,
        text="employees must report incidents",
        scope=Scope.TENANT,
        tenant_id="acme",
    )


async def test_a_public_clause_id_query_uses_the_keyword_channel_and_skips_rerank(
    public_chunk: Chunk,
) -> None:
    reranker = FakeReranker()
    pipeline = RetrievalPipeline(FakeRetrievalStore((public_chunk,)), reranker)

    trace = await pipeline.route(PUBLIC_ID, tenant_id="_public", as_of=TODAY)

    assert trace.fused
    assert trace.fused[0].channel == "keyword"
    assert reranker.rerank_calls == 0


async def test_a_tenant_prefixed_clause_id_query_routes_exactly_like_a_public_one(
    tenant_chunk: Chunk,
) -> None:
    reranker = FakeReranker()
    pipeline = RetrievalPipeline(FakeRetrievalStore((tenant_chunk,)), reranker)

    trace = await pipeline.route(TENANT_ID, tenant_id="acme", as_of=TODAY)

    assert trace.fused
    assert trace.fused[0].channel == "keyword"
    assert trace.fused[0].clause_id == TENANT_ID
    assert reranker.rerank_calls == 0


async def test_a_non_clause_id_query_runs_the_full_pipeline_and_reaches_the_reranker(
    public_chunk: Chunk,
) -> None:
    reranker = FakeReranker()
    pipeline = RetrievalPipeline(FakeRetrievalStore((public_chunk,)), reranker)

    await pipeline.route(
        "what steps must be taken to protect personal information",
        tenant_id="_public",
        as_of=TODAY,
    )

    assert reranker.rerank_calls == 1


async def test_a_clause_id_query_with_no_matching_chunk_returns_an_empty_trace() -> None:
    reranker = FakeReranker()
    pipeline = RetrievalPipeline(FakeRetrievalStore(()), reranker)

    trace = await pipeline.route(PUBLIC_ID, tenant_id="_public", as_of=TODAY)

    assert trace.fused == ()
    assert reranker.rerank_calls == 0
