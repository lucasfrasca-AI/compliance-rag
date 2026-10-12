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
from pathlib import Path

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
    assert "Track A for acme" in law.reason
    # Applicable or not, every clause Track A cites for what might apply is in.
    assert law.clause_ids is not None and law.clause_ids >= set(
        coverage.obligation("OB-T-KEEP").citations
    )
    possibly = await graph.coverage(
        Facts.of([fact("annual_turnover_aud", 900, LAST_FY)]), tenant_id="acme", as_of=AS_OF
    )
    assert LawScope.of(possibly).clause_ids  # unassessable still scopes law in


def test_an_unscoped_call_says_why() -> None:
    with pytest.raises(ValueError, match="says where it came from"):
        LawScope.unscoped(" ")
    assert LawScope.unscoped("eval").described == "unscoped: eval"


# ---- review of #127: both tracks composed ---------------------------------------


async def test_a_clause_of_an_obligation_that_does_not_apply_is_never_retrieved(
    tmp_path: Path,
) -> None:
    """End to end through `tracked_retrieve`: Track A finds OB-T-SEPARATE not
    applicable (it requires a health service the tenant says it doesn't
    provide), so its clause is never retrieved — not even for a query that is
    its exact text — while the tenant's own and user-scoped documents still
    are. The positive control is the same query unscoped."""
    import yaml

    from compliance_rag.adapters.fakes import FakeGraphStore, FakeReranker
    from compliance_rag.assessment.coverage import Outcome
    from compliance_rag.assessment.tracks import tracked_retrieve
    from compliance_rag.graph.mappings import load_mapping_set
    from compliance_rag.retrieval.pipeline import RetrievalPipeline
    from tests.test_applicability import AS_OF, CLAUSES, FIXTURE, LAST_FY, fact

    document = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    separate = "test-act:v1:s3/1"
    document["source_clause_ids"].append(separate)
    document["obligations"].append(
        {
            "id": "OB-T-SEPARATE",
            "clause_ids": [separate],
            "summary": "Report invented health incidents.",
            "valid_from": "2026-01-01",
            "applies_to": {"actors": ["test-act/actor/keeper"]},
            "requires": {"conditions": ["COND-T-HEALTH"]},
        }
    )
    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    graph = FakeGraphStore(
        load_mapping_set([path]), clause_validity={**CLAUSES, separate: CLAUSES["test-act:v1:s1/1"]}
    )

    text = "Report every invented health incident to the invented regulator within a day."
    ledger = FakeLedgerStore()
    await ledger.subscribe(tenant_id="acme", instrument_id="test-act", subscribed_by="test")
    store = FakeRetrievalStore(
        (
            Chunk(chunk_id="sep", clause_id=separate, text=text),
            Chunk(
                chunk_id="keep",
                clause_id="test-act:v1:s1/1",
                text="Keep invented health records safe.",
            ),
            Chunk(
                chunk_id="mine",
                clause_id="acme/incident-policy:v1:1/1",
                text="Acme reports every invented health incident within a day.",
                scope=Scope.TENANT,
                tenant_id="acme",
            ),
        ),
        ledger=ledger,
    )
    pipeline = RetrievalPipeline(store, FakeReranker())
    facts = Facts.of(
        [fact("annual_turnover_aud", 4000, LAST_FY), fact("provides_health_service", False)]
    )
    tracked = await tracked_retrieve(
        text, tenant_id="acme", as_of=AS_OF, facts=facts, graph=graph, retrieval=pipeline
    )
    assert tracked.coverage.obligation("OB-T-SEPARATE").outcome is Outcome.NOT_APPLICABLE
    returned = {c.clause_id for c in tracked.retrieval.fused}
    assert separate not in returned
    assert {"test-act:v1:s1/1", "acme/incident-policy:v1:1/1"} <= returned
    unscoped = await pipeline.retrieve(text, "acme", AS_OF, law=ALL_LAW)
    assert unscoped.fused[0].clause_id == separate  # positive control


async def test_user_scoped_documents_are_not_law_either() -> None:
    ledger = FakeLedgerStore()
    note = Chunk(
        chunk_id="note",
        clause_id="acme/my-notes:v1:1/1",
        text=OUT_TEXT,
        scope=Scope.USER,
        tenant_id="acme",
    )
    store = FakeRetrievalStore((*CHUNKS, note), ledger=ledger)
    await ledger.subscribe(tenant_id="acme", instrument_id="test-act", subscribed_by="test")
    result = await store.search(
        OUT_TEXT,
        law=TRACK_A,
        tenant_id="acme",
        scopes=(Scope.PUBLIC, Scope.USER),
        as_of=TODAY,
        limit=10,
    )
    assert "acme/my-notes:v1:1/1" in {c.clause_id for c in result.fused}
    assert OUT_OF_SCOPE not in {c.clause_id for c in result.fused}


def test_an_unscoped_scope_comes_only_from_unscoped() -> None:
    with pytest.raises(ValueError, match=r"only from LawScope\.unscoped"):
        LawScope(None, "a filter nobody named")
