"""The tenant's policies reach the model once, as the cached prefix (#156,
ADR-0033). Retrieval leaves out the clauses already in the prefix, by every
route (dense, sparse, fused and exact-ID lookup), so the slots they took go to
law.

One contract, run on the fake and on the real Qdrant adapter, with a positive
control (the same clause comes back by every route when the prefix is empty),
so a filter that matches nothing can't pass it. Then the number it changes:
law chunks in the top ten, before and after, on the golden corpus over real
Qdrant with real cached `gemini-embedding-001` vectors.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest

from compliance_rag.adapters.fakes import FakeEmbedder, FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.interfaces import Channel, RetrievalStore
from compliance_rag.assessment.policies import tenant_policies
from compliance_rag.corpus.models import Chunk, Scope
from compliance_rag.eval.embedding_cache import CachedEmbedder, EmbeddingCache
from compliance_rag.eval.golden import Category, load_golden_set
from compliance_rag.eval.golden_corpus import LAW_INSTRUMENT_ID, POLICY_TENANT_ID, golden_chunks
from compliance_rag.eval.harness import golden_clause_store, golden_ledger
from compliance_rag.eval.recall import WHOLE_LIBRARY, NoRerank
from compliance_rag.retrieval.pipeline import RetrievalPipeline, RetrievalTrace
from tests.conftest import ALL_LAW, qdrant_reachable

TODAY = dt.date(2026, 10, 12)
TEXT = "Destroy the invented records once they are no longer needed for any purpose."
LAW = "test-act:v1:s2/1"
POLICY = "acme/records-policy:v1:2/1"
"""An internal policy clause, in force: part of the prefix."""
UPLOAD = "acme/records-standard:v1:4/1"
"""An external upload: retrieval-only (CONTEXT.md: Document class), never in
the prefix, so it must stay retrievable."""
CHUNKS = (
    Chunk(chunk_id="law", clause_id=LAW, text=TEXT),
    Chunk(
        chunk_id="policy",
        clause_id=POLICY,
        text=f"Acme must {TEXT[0].lower()}{TEXT[1:]}",
        scope=Scope.TENANT,
        tenant_id="acme",
    ),
    Chunk(
        chunk_id="upload",
        clause_id=UPLOAD,
        text=f"The standard says: {TEXT}",
        scope=Scope.TENANT,
        tenant_id="acme",
    ),
)


async def _routes(
    store: RetrievalStore, *, in_prefix: frozenset[str], lookup: str
) -> dict[str, set[str]]:
    """Every route a chunk can take to a caller."""
    result = await store.search(
        TEXT,
        law=ALL_LAW,
        tenant_id="acme",
        scopes=(Scope.PUBLIC, Scope.TENANT),
        as_of=TODAY,
        limit=10,
        in_prefix=in_prefix,
    )
    routes = {
        channel.value: {c.clause_id for c in hits} for channel, hits in result.per_channel.items()
    }
    routes["fused"] = {c.clause_id for c in result.fused}
    hit = await store.lookup_clause(
        lookup, law=ALL_LAW, tenant_id="acme", as_of=TODAY, in_prefix=in_prefix
    )
    routes["lookup"] = {hit.clause_id} if hit else set()
    return routes


async def assert_prefix_contract(store: RetrievalStore, ledger: FakeLedgerStore) -> None:
    await ledger.subscribe(tenant_id="acme", instrument_id="test-act", subscribed_by="test")

    # Positive control: with nothing in the prefix, the policy comes back by
    # every route.
    control = await _routes(store, in_prefix=frozenset(), lookup=POLICY)
    assert all(POLICY in ids for ids in control.values()), control

    # In the prefix: gone from every route. The law and the external upload
    # still come back.
    routes = await _routes(store, in_prefix=frozenset({POLICY}), lookup=POLICY)
    assert all(POLICY not in ids for ids in routes.values()), routes
    assert {LAW, UPLOAD} <= routes["fused"]

    # The prefix holds tenant clauses only: a law id named in it never
    # narrows the law.
    law = await _routes(store, in_prefix=frozenset({LAW}), lookup=LAW)
    assert all(LAW in ids for ids in law.values()), law


async def test_the_fake_leaves_out_the_prefix() -> None:
    ledger = FakeLedgerStore()
    await assert_prefix_contract(FakeRetrievalStore(CHUNKS, ledger=ledger), ledger)


QDRANT_URL = os.environ.get("QDRANT_URL", "")
_needs_qdrant = pytest.mark.skipif(
    not os.environ.get("REQUIRE_RECALL_GATE") and not qdrant_reachable(QDRANT_URL),
    reason="needs a real Qdrant (QDRANT_URL)",
)


@_needs_qdrant
async def test_qdrant_leaves_out_the_prefix() -> None:
    from qdrant_client import AsyncQdrantClient

    from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore

    ledger = FakeLedgerStore()
    client = AsyncQdrantClient(url=QDRANT_URL)
    store = QdrantRetrievalStore(
        client,
        collection_name=f"prefix-{uuid.uuid4().hex[:8]}",
        embedder=FakeEmbedder(dimensions=8),
        ledger=ledger,
    )
    try:
        await store.ensure_ready()
        await store.upsert_chunks(CHUNKS)
        await assert_prefix_contract(store, ledger)
    finally:
        await client.delete_collection(store._collection_name)  # pyright: ignore[reportPrivateUsage]
        await client.close()


# ---- the number it changes: law in the top ten --------------------------------


def _law(trace: RetrievalTrace) -> int:
    return sum(c.clause_id.startswith(f"{LAW_INSTRUMENT_ID}:") for c in trace.ranked_candidates())


async def test_answer_leaves_the_prefix_out_and_its_slots_go_to_law() -> None:
    """#149's measurement, through `answer()`: Acme on GA-05's facts, Track
    A's scope, the question below. Before #156, seven of the top ten were Acme
    policy clauses already in the prefix, leaving three for law. Same store,
    scope and question, with the prefix left out: only law, every law clause
    from before among it, and none of the prefix. The fake scores by shared
    words, and only four law clauses in Track A's scope share any with the
    question, so four is all the law it can find; real Qdrant's dense channel
    ranks every clause (the test below)."""
    from compliance_rag.adapters.fakes import FakeGenerator, FakeReranker
    from compliance_rag.adapters.interfaces import LawScope
    from compliance_rag.assessment.evidence import Facts
    from compliance_rag.assessment.pipeline import AssessmentPipeline
    from compliance_rag.eval.applicability import load_applicability_set
    from compliance_rag.graph.mappings import load_mapping_set
    from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier
    from tests.test_applicability_gate import CORPUS_MAPPINGS, fake_for

    question = (
        "What must Acme do to protect the personal information it holds from misuse and loss?"
    )
    day = dt.date(2026, 10, 12)
    labels = load_applicability_set()
    ga05 = next(c for c in labels.cases if c.id == "GA-05")
    graph = fake_for(load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), labels)
    law = LawScope.of(
        await graph.coverage(Facts.of(ga05.evidence()), tenant_id=POLICY_TENANT_ID, as_of=day)
    )
    ledger, clauses = await golden_ledger(), await golden_clause_store()
    await ledger.subscribe(
        tenant_id=POLICY_TENANT_ID, instrument_id=LAW_INSTRUMENT_ID, subscribed_by="test"
    )
    policies = await tenant_policies(ledger, clauses, tenant_id=POLICY_TENANT_ID, as_of=day)
    prefix = {p.clause_id for p in policies}
    pipeline = AssessmentPipeline(
        RetrievalPipeline(FakeRetrievalStore(golden_chunks(), ledger=ledger), FakeReranker()),
        FakeGenerator('{"answer": "-", "citations": []}'),
        Verifier(clauses, ledger, CanonicalCitationVerifier()),
        ledger,
    )

    before = await pipeline.retrieval.retrieve(question, POLICY_TENANT_ID, day, law=law)
    after = (
        await pipeline.answer(question, POLICY_TENANT_ID, day, law=law, policies=policies)
    ).retrieval

    assert len(before.ranked_candidates()) == 10
    assert (_law(before), _law(after)) == (3, 4)
    assert len(after.ranked_candidates()) == len(after.per_channel[Channel.DENSE]) == 4
    retrieved_before = {c.clause_id for c in before.ranked_candidates()}
    retrieved_after = {c.clause_id for c in after.ranked_candidates()}
    assert len(retrieved_before & prefix) == 7 and not retrieved_after & prefix
    assert retrieved_before - prefix <= retrieved_after


@_needs_qdrant
async def test_on_real_qdrant_the_prefix_slots_go_to_law() -> None:
    """Every golden question that isn't a clause id, asked as Acme subscribed
    to the Act, over the whole library (the recall gate's scope). Before is
    retrieval as `answer()` ran it until #156; after leaves out Acme's
    policies, which the prefix already carries.

    RRF scores a candidate by its own rank in each channel, so taking
    candidates out can only move law up: no question may lose law, and none
    may get a prefix clause back."""
    from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore

    ledger = await golden_ledger()
    await ledger.subscribe(
        tenant_id=POLICY_TENANT_ID, instrument_id=LAW_INSTRUMENT_ID, subscribed_by="test"
    )
    golden = load_golden_set()
    asked = [q for q in golden.queries if q.category is not Category.EXACT_ID]
    as_of = asked[0].as_of
    assert all(q.as_of == as_of for q in asked)
    policies = await tenant_policies(
        ledger, await golden_clause_store(), tenant_id=POLICY_TENANT_ID, as_of=as_of
    )
    in_prefix = frozenset(p.clause_id for p in policies)
    assert in_prefix

    async with QdrantRetrievalStore.ephemeral(
        QDRANT_URL,
        embedder=CachedEmbedder(EmbeddingCache.load()),
        ledger=ledger,
        prefix="prefix-slots",
    ) as store:
        await store.upsert_chunks(golden_chunks())
        pipeline = RetrievalPipeline(store, NoRerank())
        counts: list[tuple[str, int, int]] = []
        for query in asked:
            before = await pipeline.retrieve(
                query.query, POLICY_TENANT_ID, as_of, law=WHOLE_LIBRARY
            )
            after = await pipeline.retrieve(
                query.query, POLICY_TENANT_ID, as_of, law=WHOLE_LIBRARY, in_prefix=in_prefix
            )
            assert not in_prefix & {c.clause_id for c in after.ranked_candidates()}, query.id
            counts.append((query.id, _law(before), _law(after)))

    print("\nlaw chunks in the top 10, before -> after (#156)")
    for query_id, law_before, law_after in counts:
        print(f"  {query_id}: {law_before} -> {law_after}")
    total_before, total_after = sum(c[1] for c in counts), sum(c[2] for c in counts)
    print(f"  total: {total_before} -> {total_after} over {len(counts)} questions")
    assert all(law_after >= law_before for _, law_before, law_after in counts), counts
    assert total_after > total_before
