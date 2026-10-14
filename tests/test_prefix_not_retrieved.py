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

import pytest

from compliance_rag.adapters.fakes import FakeEmbedder, FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.interfaces import NO_PREFIX, PolicyCorpus, RetrievalStore
from compliance_rag.assessment.policies import tenant_policies
from compliance_rag.corpus.models import PUBLIC_TENANT_ID, Chunk, Scope
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
    store: RetrievalStore, *, sent: frozenset[str], lookup: str
) -> dict[str, set[str]]:
    """Every route a chunk can take to a caller."""
    result = await store.search(
        TEXT,
        law=ALL_LAW,
        tenant_id="acme",
        scopes=(Scope.PUBLIC, Scope.TENANT),
        as_of=TODAY,
        limit=10,
        policy_corpus=PolicyCorpus(sent),
    )
    routes = {
        channel.value: {c.clause_id for c in hits} for channel, hits in result.per_channel.items()
    }
    routes["fused"] = {c.clause_id for c in result.fused}
    hit = await store.lookup_clause(
        lookup, law=ALL_LAW, tenant_id="acme", as_of=TODAY, policy_corpus=PolicyCorpus(sent)
    )
    routes["lookup"] = {hit.clause_id} if hit else set()
    return routes


async def assert_prefix_contract(store: RetrievalStore, ledger: FakeLedgerStore) -> None:
    await ledger.subscribe(tenant_id="acme", instrument_id="test-act", subscribed_by="test")

    # Positive control: with nothing in the prefix, the policy comes back by
    # every route.
    control = await _routes(store, sent=frozenset(), lookup=POLICY)
    assert all(POLICY in ids for ids in control.values()), control

    # In the prefix: gone from every route. The law and the external upload
    # still come back.
    routes = await _routes(store, sent=frozenset({POLICY}), lookup=POLICY)
    assert all(POLICY not in ids for ids in routes.values()), routes
    assert {LAW, UPLOAD} <= routes["fused"]

    # The prefix holds tenant clauses only: a law id named in it never
    # narrows the law.
    law = await _routes(store, sent=frozenset({LAW}), lookup=LAW)
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
    from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore

    ledger = FakeLedgerStore()
    async with QdrantRetrievalStore.ephemeral(
        QDRANT_URL, embedder=FakeEmbedder(dimensions=8), ledger=ledger, prefix="prefix"
    ) as store:
        await store.upsert_chunks(CHUNKS)
        await assert_prefix_contract(store, ledger)


async def test_the_router_leaves_out_the_prefix_on_both_of_its_paths() -> None:
    """`route()`: a clause-id question goes to the lookup, anything else to
    `retrieve()`. The prefix is left out either way."""
    from compliance_rag.adapters.fakes import FakeReranker

    ledger = FakeLedgerStore()
    await ledger.subscribe(tenant_id="acme", instrument_id="test-act", subscribed_by="test")
    pipeline = RetrievalPipeline(FakeRetrievalStore(CHUNKS, ledger=ledger), FakeReranker())
    sent = PolicyCorpus(frozenset({POLICY}))

    async def found(question: str, policy_corpus: PolicyCorpus) -> set[str]:
        trace = await pipeline.route(
            question, "acme", TODAY, law=ALL_LAW, policy_corpus=policy_corpus
        )
        return {c.clause_id for c in trace.ranked_candidates()}

    assert POLICY in await found(POLICY, NO_PREFIX) and POLICY in await found(TEXT, NO_PREFIX)
    assert await found(POLICY, sent) == set()
    assert POLICY not in await found(TEXT, sent) and LAW in await found(TEXT, sent)


# ---- the number it changes: law in the top ten --------------------------------


def _law(trace: RetrievalTrace) -> int:
    return sum(c.clause_id.startswith(f"{LAW_INSTRUMENT_ID}:") for c in trace.ranked_candidates())


async def test_answer_leaves_the_prefix_out_and_its_slots_go_to_law() -> None:
    """#149's measurement, through `answer()`: Acme on GA-05's facts, Track
    A's scope, the question below. Before #156, seven of the top ten were Acme
    policy clauses already in the prefix, leaving three for law. Same store,
    scope and question, with the prefix left out: ten law clauses, every law
    clause from before among them, and none of the prefix. (Until #163 put a
    cited subclause's paragraphs in scope, the fake found only four law
    clauses in scope that shared a word with the question.)"""
    from compliance_rag.adapters.fakes import FakeGenerator, FakeReranker
    from compliance_rag.adapters.generation_residency import TenantTextGate
    from compliance_rag.adapters.interfaces import LawScope
    from compliance_rag.assessment.evidence import Facts
    from compliance_rag.assessment.pipeline import AssessmentPipeline
    from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
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
        tenant_text=TenantTextGate("us", SyntheticRegistry.load()),
    )

    before = await pipeline.retrieval.retrieve(question, POLICY_TENANT_ID, day, law=law)
    after = (
        await pipeline.answer(question, POLICY_TENANT_ID, day, law=law, policies=policies)
    ).retrieval

    assert len(before.ranked_candidates()) == len(after.ranked_candidates()) == 10
    assert (_law(before), _law(after)) == (3, 10)
    retrieved_before = {c.clause_id for c in before.ranked_candidates()}
    retrieved_after = {c.clause_id for c in after.ranked_candidates()}
    assert len(retrieved_before & prefix) == 7 and not retrieved_after & prefix
    assert retrieved_before - prefix <= retrieved_after


def _recall(trace: RetrievalTrace, expected: frozenset[str]) -> float:
    found = {c.clause_id for c in trace.ranked_candidates()}
    return len(expected & found) / len(expected)


@_needs_qdrant
async def test_on_real_qdrant_the_prefix_slots_go_to_law() -> None:
    """Every golden question that isn't a clause id (17), asked as Acme
    subscribed to the Act, over the whole library (the recall gate's scope).
    Before is retrieval as `answer()` ran it until #156; after leaves out
    Acme's policies, which the prefix already carries.

    Pinned, because deterministic:
    - after, every question's top ten is law, with no prefix clause;
    - after, it is what the library itself retrieves (the `_public` view the
      recall gate measures), since all of Acme's own documents are in the
      prefix: the same fused scores, and the same clauses above the tenth. So
      the answer path's law recall is now the gate's own, which CI pins per
      question. Qdrant orders candidates with equal RRF scores differently
      from call to call, so tied places are compared by score, not by clause.

    Printed, not pinned: before. Its tenth place sits on such ties between law
    and policy candidates, so a question's before moves by a clause between
    runs. A given law clause can
    also drop out after, overtaken by other law once policy is gone, so
    recall of expected law is reported per question, not asserted to rise."""
    from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore

    ledger = await golden_ledger()
    await ledger.subscribe(
        tenant_id=POLICY_TENANT_ID, instrument_id=LAW_INSTRUMENT_ID, subscribed_by="test"
    )
    golden = load_golden_set()
    asked = [q for q in golden.queries if q.category is not Category.EXACT_ID]
    as_of = asked[0].as_of
    assert len(asked) == 17 and all(q.as_of == as_of for q in asked)
    policies = await tenant_policies(
        ledger, await golden_clause_store(), tenant_id=POLICY_TENANT_ID, as_of=as_of
    )
    sent = PolicyCorpus(frozenset(p.clause_id for p in policies))
    assert sent.clause_ids

    def ids(trace: RetrievalTrace) -> list[str]:
        return [c.clause_id for c in trace.ranked_candidates()]

    def settled(trace: RetrievalTrace) -> tuple[list[float], set[str]]:
        """What RRF ties can't move: the fused scores in order, and the clauses
        scored strictly above the tenth."""
        ranked = trace.ranked_candidates()
        scores = [round(c.score, 9) for c in ranked]
        return scores, {c.clause_id for c, s in zip(ranked, scores, strict=True) if s > scores[-1]}

    print("\n#156 on real Qdrant, as Acme: law in the top 10; recall@10 of the law expected")
    async with QdrantRetrievalStore.ephemeral(
        QDRANT_URL,
        embedder=CachedEmbedder(EmbeddingCache.load()),
        ledger=ledger,
        prefix="prefix-slots",
    ) as store:
        pipeline = RetrievalPipeline(store, NoRerank())
        await store.upsert_chunks(golden_chunks())
        for query in asked:
            before = await pipeline.retrieve(
                query.query, POLICY_TENANT_ID, as_of, law=WHOLE_LIBRARY
            )
            after = await pipeline.retrieve(
                query.query, POLICY_TENANT_ID, as_of, law=WHOLE_LIBRARY, policy_corpus=sent
            )
            library = await pipeline.retrieve(
                query.query, PUBLIC_TENANT_ID, as_of, law=WHOLE_LIBRARY
            )
            assert _law(after) == len(ids(after)) == 10, query.id
            assert settled(after) == settled(library), query.id
            expected = frozenset(
                c for c in query.expected_clause_ids if c.startswith(f"{LAW_INSTRUMENT_ID}:")
            )
            recall = (
                f"  recall {_recall(before, expected):.3f} -> {_recall(after, expected):.3f}"
                if expected
                else ""
            )
            print(f"  {query.id}: law {_law(before)} -> {_law(after)}{recall}")
