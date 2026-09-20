"""Contract test per interface, run against the in-memory fakes (D-08).

Each test asserts the promise the interface makes, not the fake's mechanics:
traversal is capped and `as_of`-scoped (D-05), Track B never escapes Track A's
filter (D-03), a bad citation fails the verifier (D-02).
"""

from __future__ import annotations

import datetime as dt

import pytest

from compliance_rag.adapters.fakes import (
    FakeCitationVerifier,
    FakeEmbedder,
    FakeGenerator,
    FakeGraphStore,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.interfaces import (
    Channel,
    CitationVerifier,
    Embedder,
    Generator,
    GraphStore,
    LedgerStore,
    Obligation,
    Profile,
    Reranker,
    RetrievalStore,
)
from compliance_rag.corpus.models import Chunk, ClauseRecord, Scope

TODAY = dt.date(2026, 1, 1)
LATER = dt.date(2027, 6, 1)

APP11 = "privacy-act-1988:C2024C00276:APP11.1"
APP12 = "privacy-act-1988:C2024C00276:APP12.1"


@pytest.fixture
def graph() -> FakeGraphStore:
    return FakeGraphStore(
        obligations={
            "OB-APP11": Obligation(
                obligation_id="OB-APP11",
                clause_id=APP11,
                instrument_id="privacy-act-1988",
                summary="Take reasonable steps to protect personal information.",
            ),
            "OB-APP12": Obligation(
                obligation_id="OB-APP12",
                clause_id=APP12,
                instrument_id="privacy-act-1988",
                summary="Give an individual access to their personal information.",
            ),
            "OB-ISO-A8": Obligation(
                obligation_id="OB-ISO-A8",
                clause_id="iso-27001:2022:A.8.3",
                instrument_id="iso-27001",
                summary="Information access restriction.",
            ),
        },
        applicability={"AU": ("OB-APP11", "OB-APP12"), "health": ("OB-APP11",)},
        crosswalk_edges={"OB-APP11": ("OB-ISO-A8",)},
        # Tranche-style commencement: OB-APP12 only binds from 2027.
        valid_from={"OB-APP12": dt.date(2027, 1, 1)},
    )


@pytest.fixture
def chunks() -> tuple[Chunk, ...]:
    return (
        Chunk(
            chunk_id="c1",
            clause_id=APP11,
            obligation_id="OB-APP11",
            text="An APP entity must take such steps as are reasonable to protect the information.",
        ),
        Chunk(
            chunk_id="c2",
            clause_id=APP12,
            obligation_id="OB-APP12",
            text="An APP entity must give the individual access to the information.",
        ),
        Chunk(
            chunk_id="c3",
            clause_id="acme-policy:v3:sec2.1",
            obligation_id=None,
            text="Acme retains customer records for seven years to protect the information.",
            scope=Scope.TENANT,
            tenant_id="acme",
        ),
    )


# --------------------------------------------------------------------------
# GraphStore
# --------------------------------------------------------------------------


async def test_graph_store_is_a_graph_store(graph: FakeGraphStore) -> None:
    assert isinstance(graph, GraphStore)


async def test_traversal_requires_a_depth_cap(graph: FakeGraphStore) -> None:
    """D-05: an uncapped traversal is a test failure."""
    profile = Profile(tenant_id="acme", jurisdictions=("AU",))
    with pytest.raises(ValueError, match="max_depth"):
        await graph.applicable_obligations(profile, as_of=TODAY, max_depth=0)


async def test_as_of_changes_the_obligation_set(graph: FakeGraphStore) -> None:
    """D-05: the same question on two dates gives different, correct answers."""
    profile = Profile(tenant_id="acme", jurisdictions=("AU",))
    now = await graph.applicable_obligations(profile, as_of=TODAY, max_depth=3)
    later = await graph.applicable_obligations(profile, as_of=LATER, max_depth=3)
    assert [o.obligation_id for o in now] == ["OB-APP11"]
    assert [o.obligation_id for o in later] == ["OB-APP11", "OB-APP12"]


async def test_applicable_obligations_are_untruncated(graph: FakeGraphStore) -> None:
    """D-03: Track A returns the full set; there is no top-k to pass."""
    profile = Profile(tenant_id="acme", jurisdictions=("AU",), sectors=("health",))
    found = await graph.applicable_obligations(profile, as_of=LATER, max_depth=3)
    assert {o.obligation_id for o in found} == {"OB-APP11", "OB-APP12"}


async def test_crosswalks_respect_the_depth_cap(graph: FakeGraphStore) -> None:
    hops = await graph.crosswalks("OB-APP11", as_of=TODAY, max_depth=1)
    assert [o.obligation_id for o in hops] == ["OB-ISO-A8"]
    with pytest.raises(ValueError, match="max_depth"):
        await graph.crosswalks("OB-APP11", as_of=TODAY, max_depth=0)


# --------------------------------------------------------------------------
# RetrievalStore
# --------------------------------------------------------------------------


async def test_retrieval_store_is_a_retrieval_store(chunks: tuple[Chunk, ...]) -> None:
    assert isinstance(FakeRetrievalStore(chunks), RetrievalStore)


async def test_track_b_never_escapes_track_a(chunks: tuple[Chunk, ...]) -> None:
    """D-03: filtered to Track A's obligation IDs, whatever the text similarity."""
    store = FakeRetrievalStore(chunks)
    result = await store.search(
        "steps to protect the information",
        obligation_ids=("OB-APP11",),
        tenant_id="acme",
        scopes=(Scope.PUBLIC, Scope.TENANT),
        as_of=TODAY,
        limit=10,
    )
    assert {c.chunk_id for c in result.fused} == {"c1"}


async def test_both_channels_are_returned_before_fusion(chunks: tuple[Chunk, ...]) -> None:
    """Both channel candidate lists are logged before fusion (CLAUDE.md)."""
    store = FakeRetrievalStore(chunks)
    result = await store.search(
        "access to the information",
        obligation_ids=(),
        tenant_id="acme",
        scopes=(Scope.PUBLIC,),
        as_of=LATER,
        limit=10,
    )
    assert set(result.per_channel) == {Channel.DENSE, Channel.SPARSE}
    assert result.fused


async def test_tenant_scope_is_enforced(chunks: tuple[Chunk, ...]) -> None:
    """D-14 / D-09: another tenant never sees a tenant-scoped chunk."""
    store = FakeRetrievalStore(chunks)
    result = await store.search(
        "retains customer records",
        obligation_ids=(),
        tenant_id="other-corp",
        scopes=(Scope.PUBLIC, Scope.TENANT),
        as_of=TODAY,
        limit=10,
    )
    assert all(c.chunk_id != "c3" for c in result.fused)


async def test_clause_id_lookup_skips_ranking(chunks: tuple[Chunk, ...]) -> None:
    """D-04 / D-12: the router's only decision, and it returns the keyword channel."""
    store = FakeRetrievalStore(chunks)
    hit = await store.lookup_clause(APP11, tenant_id="acme", as_of=TODAY)
    assert hit is not None
    assert hit.channel is Channel.KEYWORD
    assert hit.clause_id == APP11


# --------------------------------------------------------------------------
# LedgerStore
# --------------------------------------------------------------------------


async def test_ledger_store_is_a_ledger_store() -> None:
    assert isinstance(FakeLedgerStore(), LedgerStore)


async def test_ledger_round_trips_a_profile() -> None:
    ledger = FakeLedgerStore()
    profile = Profile(tenant_id="acme", jurisdictions=("AU",), sectors=("health",))
    await ledger.put_profile(profile)
    assert await ledger.get_profile("acme") == profile
    assert await ledger.get_profile("other-corp") is None


async def test_assessment_records_versions_and_trace_id() -> None:
    """PHASES.md: assessment records corpus_version, mapping_version, as_of, trace_id."""
    ledger = FakeLedgerStore()
    await ledger.record_assessment(
        tenant_id="acme",
        assessment_id="a1",
        corpus_version="corpus-2026.09",
        mapping_version="mappings-2026.09",
        as_of=TODAY,
        trace_id="0" * 32,
    )
    row = await ledger.get_assessment("acme", "a1")
    assert row is not None
    assert row["corpus_version"] == "corpus-2026.09"
    assert row["mapping_version"] == "mappings-2026.09"
    assert row["as_of"] == TODAY
    assert row["trace_id"] == "0" * 32
    assert await ledger.get_assessment("other-corp", "a1") is None


# --------------------------------------------------------------------------
# Reranker, Embedder, Generator
# --------------------------------------------------------------------------


async def test_reranker_is_a_reranker_and_truncates_to_top_k() -> None:
    reranker = FakeReranker()
    assert isinstance(reranker, Reranker)
    scored = await reranker.rerank(
        "protect the information",
        ("protect the information now", "unrelated text", "the information"),
        top_k=2,
    )
    assert len(scored) == 2
    assert scored[0].score >= scored[1].score


async def test_embedder_returns_configured_dimensions() -> None:
    embedder = FakeEmbedder(dimensions=1536)
    assert isinstance(embedder, Embedder)
    vectors = await embedder.embed(("hello world", "another"))
    assert len(vectors) == 2
    assert all(len(v) == 1536 for v in vectors)


async def test_generator_reports_cached_prefix_hit() -> None:
    """D-11: the cached prefix is the tenant policy corpus, and its hit is traced."""
    generator = FakeGenerator()
    assert isinstance(generator, Generator)
    warm = await generator.generate(
        system="s", cached_prefix="tenant policies", user="q", max_tokens=100
    )
    cold = await generator.generate(system="s", cached_prefix=None, user="q", max_tokens=100)
    assert warm.cached_prefix_hit is True
    assert cold.cached_prefix_hit is False


# --------------------------------------------------------------------------
# CitationVerifier
# --------------------------------------------------------------------------


@pytest.fixture
def clause() -> ClauseRecord:
    return ClauseRecord.build(
        instrument_id="privacy-act-1988",
        version_id="C2024C00276",
        clause_path="APP11.1",
        text="An APP entity must take such steps as are reasonable in the circumstances "
        "to protect the information from misuse, interference and loss.",
    )


async def test_verifier_is_a_verifier() -> None:
    assert isinstance(FakeCitationVerifier(), CitationVerifier)


async def test_good_citation_passes(clause: ClauseRecord) -> None:
    verifier = FakeCitationVerifier()
    assert await verifier.verify(
        clause.clause_id,
        "take such steps as are reasonable in the circumstances",
        clauses=(clause,),
    )


async def test_bad_citation_blocks(clause: ClauseRecord) -> None:
    """D-02: the deliberately bad citation PHASES.md requires us to prove blocks."""
    verifier = FakeCitationVerifier()
    assert not await verifier.verify(
        clause.clause_id,
        "must encrypt all personal information at rest using AES-256",
        clauses=(clause,),
    )


async def test_citation_matching_is_canonical_not_literal(clause: ClauseRecord) -> None:
    """D-02: curly quotes and collapsed whitespace still match."""
    verifier = FakeCitationVerifier()
    assert await verifier.verify(
        clause.clause_id,
        "  protect   the information from misuse,\ninterference and loss.  ",
        clauses=(clause,),
    )


async def test_citation_against_unknown_clause_blocks(clause: ClauseRecord) -> None:
    verifier = FakeCitationVerifier()
    assert not await verifier.verify(
        "privacy-act-1988:C2024C00276:APP99.9",
        "take such steps as are reasonable",
        clauses=(clause,),
    )
