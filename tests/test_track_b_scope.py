"""Track B filtered to Track A's set (D-03, issue #116, ADR-0030).

A library clause outside Track A's set cannot reach a caller by any route —
dense, sparse, fused, or exact-ID lookup — no matter how well it matches:
the query here *is* its text, word for word. One contract, run on the fake
and on the real Qdrant adapter, with a positive control (the same clause comes
back first when unscoped) so a filter that matches nothing can't pass it.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest

from compliance_rag.adapters.fakes import FakeEmbedder, FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.interfaces import LawScope, RetrievalStore
from compliance_rag.assessment.evidence import Facts
from compliance_rag.corpus.models import Chunk, Scope
from tests.conftest import ALL_LAW, qdrant_reachable

TODAY = dt.date(2026, 10, 12)
IN_SCOPE = "test-act:v1:s1/1"
OUT_OF_SCOPE = "test-act:v1:s2/1"
OUT_TEXT = "Destroy the invented records once they are no longer needed for any purpose."
CHUNKS = (
    Chunk(chunk_id="in", clause_id=IN_SCOPE, text="Keep the invented records safe and secure."),
    Chunk(chunk_id="out", clause_id=OUT_OF_SCOPE, text=OUT_TEXT),
    Chunk(
        chunk_id="own",
        clause_id="acme/records-policy:v1:2/1",
        text="Acme destroys invented records it no longer needs, every quarter.",
        scope=Scope.TENANT,
        tenant_id="acme",
    ),
)
TRACK_A = LawScope(frozenset({IN_SCOPE}), "test: Track A cites s1/1 only")


def _reach(result_ids: dict[str, set[str]]) -> set[str]:
    return set[str]().union(*result_ids.values())


async def _routes(
    store: RetrievalStore, law: LawScope, *, tenant_id: str = "acme"
) -> dict[str, set[str]]:
    """Every route a chunk can take to a caller."""
    result = await store.search(
        OUT_TEXT,
        law=law,
        tenant_id=tenant_id,
        scopes=(Scope.PUBLIC, Scope.TENANT),
        as_of=TODAY,
        limit=10,
    )
    routes = {
        channel.value: {c.clause_id for c in hits} for channel, hits in result.per_channel.items()
    }
    routes["fused"] = {c.clause_id for c in result.fused}
    hit = await store.lookup_clause(OUT_OF_SCOPE, law=law, tenant_id=tenant_id, as_of=TODAY)
    routes["lookup"] = {hit.clause_id} if hit else set()
    return routes


async def assert_track_b_scope_contract(store: RetrievalStore, ledger: FakeLedgerStore) -> None:
    await ledger.subscribe(tenant_id="acme", instrument_id="test-act", subscribed_by="test")

    # Positive control: unscoped, the out-of-scope clause is the best match.
    unscoped = await store.search(
        OUT_TEXT,
        law=ALL_LAW,
        tenant_id="acme",
        scopes=(Scope.PUBLIC, Scope.TENANT),
        as_of=TODAY,
        limit=10,
    )
    assert unscoped.fused[0].clause_id == OUT_OF_SCOPE
    assert OUT_OF_SCOPE in _reach(await _routes(store, ALL_LAW))

    # Scoped to Track A: gone from every route; the in-scope clause and the
    # tenant's own policy (not law, not Track A's to scope) still come back.
    routes = await _routes(store, TRACK_A)
    assert all(OUT_OF_SCOPE not in ids for ids in routes.values()), routes
    assert {IN_SCOPE, "acme/records-policy:v1:2/1"} <= routes["fused"]

    # Track A finding nothing applies is not "no filter": no law at all.
    nothing = await _routes(store, LawScope(frozenset(), "test: nothing applies"))
    assert not {IN_SCOPE, OUT_OF_SCOPE} & _reach(nothing)
    assert "acme/records-policy:v1:2/1" in nothing["fused"]


async def test_the_fake_keeps_the_scope() -> None:
    ledger = FakeLedgerStore()
    store = FakeRetrievalStore(CHUNKS, ledger=ledger)
    await assert_track_b_scope_contract(store, ledger)


QDRANT_URL = os.environ.get("QDRANT_URL", "")


@pytest.mark.skipif(not qdrant_reachable(QDRANT_URL), reason="needs a real Qdrant (QDRANT_URL)")
async def test_qdrant_keeps_the_scope() -> None:
    from qdrant_client import AsyncQdrantClient

    from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore

    ledger = FakeLedgerStore()
    store = QdrantRetrievalStore(
        AsyncQdrantClient(url=QDRANT_URL),
        collection_name=f"track-b-scope-{uuid.uuid4().hex[:8]}",
        embedder=FakeEmbedder(dimensions=8),
        ledger=ledger,
    )
    await store.ensure_ready()
    await store.upsert_chunks(CHUNKS)
    await assert_track_b_scope_contract(store, ledger)


async def test_track_a_coverage_becomes_the_scope() -> None:
    """End to end: what Track A decides is what Track B may return. An
    obligation that doesn't apply contributes no clause; one that might
    (unassessable) does — a "possibly" answer still cites the law that
    would apply."""
    from compliance_rag.adapters.fakes import FakeGraphStore
    from compliance_rag.graph.mappings import load_mapping_set
    from tests.test_applicability import AS_OF, CLAUSES, FIXTURE, LAST_FY, fact

    graph = FakeGraphStore(load_mapping_set([FIXTURE]), clause_validity=CLAUSES)
    over = Facts.of(
        [fact("annual_turnover_aud", 4000, LAST_FY), fact("provides_health_service", False)]
    )
    coverage = await graph.coverage(over, tenant_id="acme", as_of=AS_OF)
    law = LawScope.of(coverage)
    assert law.clause_ids == coverage.track_a_clause_ids
    assert "Track A for acme" in law.reason
    possibly = await graph.coverage(
        Facts.of([fact("annual_turnover_aud", 900, LAST_FY)]), tenant_id="acme", as_of=AS_OF
    )
    assert LawScope.of(possibly).clause_ids  # unassessable still scopes law in


def test_an_unscoped_call_says_why() -> None:
    with pytest.raises(ValueError, match="says where it came from"):
        LawScope.unscoped(" ")
    assert LawScope.unscoped("eval").described == "unscoped: eval"
