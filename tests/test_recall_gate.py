"""The regression gate (issue #28): recall@10 over the signed golden set,
through `route()` on a real Qdrant server, on every build.

Dense vectors are real `gemini-embedding-001` output served from the
checked-in cache (`CachedEmbedder`), BM25 is computed locally, and the reranker
is `NoRerank` — so the gate needs no cloud identity, no network and no model
artefact, and nothing in it can call an LLM. Gated on fusion per category
against PHASES.md's bars; a query whose recall falls below its checked-in
baseline fails the build by name.

CI's `ci` job sets `REQUIRE_RECALL_GATE`, so a missing Qdrant fails there
instead of skipping.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator

import pytest

from compliance_rag.adapters.fakes import FakeLedgerStore
from compliance_rag.adapters.interfaces import Candidate, Channel
from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
from compliance_rag.eval.embedding_cache import CachedEmbedder, EmbeddingCache, EmbeddingCacheMiss
from compliance_rag.eval.golden import Category, GoldenQuery, GoldenSet, load_golden_set
from compliance_rag.eval.golden_corpus import golden_chunks
from compliance_rag.eval.recall import (
    EXIT_BARS,
    FUSED,
    NoRerank,
    QueryRecall,
    UnsignedGoldenSet,
    below_exit_bar,
    by_category,
    load_baseline,
    measure,
    measure_trace,
    recall_at_k,
    regressions,
    write_baseline,
)
from compliance_rag.retrieval.pipeline import RetrievalPipeline, RetrievalTrace
from tests.conftest import qdrant_reachable

_QDRANT_URL = os.environ.get("QDRANT_URL", "")
_REQUIRED = bool(os.environ.get("REQUIRE_RECALL_GATE"))
_needs_qdrant = pytest.mark.skipif(
    not _REQUIRED and not qdrant_reachable(_QDRANT_URL),
    reason="the recall gate needs a real Qdrant server (QDRANT_URL)",
)


def _candidate(clause_id: str) -> Candidate:
    return Candidate(chunk_id=clause_id, clause_id=clause_id, text="", score=0.0)


# ---- the arithmetic, without a server -------------------------------------


def test_recall_counts_distinct_clauses_within_the_first_k() -> None:
    ranked = [_candidate(c) for c in ("a", "a", "b", "c", "d")]
    assert recall_at_k(("a", "d"), ranked, k=3) == 0.5  # a, b, c — d is 4th distinct
    assert recall_at_k(("a", "d"), ranked, k=4) == 1.0


def test_an_exact_id_query_is_measured_on_the_router_lookup() -> None:
    query = GoldenQuery(
        id="GQ-01",
        category=Category.EXACT_ID,
        query="privacy-act-1988:C1:s6/1",
        tenant_id="_public",
        as_of=dt.date(2026, 1, 1),
        expected_clause_ids=("privacy-act-1988:C1:s6/1",),
    )
    hit = _candidate("privacy-act-1988:C1:s6/1")
    trace = RetrievalTrace(per_channel={Channel.KEYWORD: (hit,)}, fused=(hit,), reranked=())
    assert measure_trace(query, trace).by_list == {"keyword": 1.0, FUSED: 1.0, "reranked": 0.0}


def test_a_drop_names_the_query_that_regressed() -> None:
    now = (
        QueryRecall("GQ-09", Category.PARAPHRASE, {FUSED: 1.0}),
        QueryRecall("GQ-18", Category.CROSS_CLAUSE, {FUSED: 2 / 3}),
        QueryRecall("GQ-26", Category.PARAPHRASE, {FUSED: 0.0}),  # new, not in baseline
    )
    baseline = {"GQ-09": 1.0, "GQ-18": 1.0}
    assert regressions(now, baseline) == {"GQ-18": (1.0, 2 / 3)}


def test_a_category_under_its_bar_is_reported_with_its_value() -> None:
    now = (
        QueryRecall("GQ-01", Category.EXACT_ID, {FUSED: 1.0}),
        QueryRecall("GQ-09", Category.PARAPHRASE, {FUSED: 1.0}),
        QueryRecall("GQ-10", Category.PARAPHRASE, {FUSED: 0.0}),
    )
    assert below_exit_bar(now) == {Category.PARAPHRASE: 0.5}


async def test_an_unsigned_golden_set_is_refused() -> None:
    unsigned = load_golden_set().model_copy(update={"reviewed_by": "PENDING — reviewer"})
    pipeline = RetrievalPipeline(retrieval_store=None, reranker=NoRerank())  # pyright: ignore[reportArgumentType] — never reached
    with pytest.raises(UnsignedGoldenSet):
        await measure(pipeline, unsigned)


def test_every_golden_text_has_a_real_cached_vector() -> None:
    """Editing a query or re-parsing a clause without refreshing the cache
    fails here, naming the text, before the gate measures anything stale."""
    cache = EmbeddingCache.load()
    assert (cache.model, cache.dimensions) == ("gemini-embedding-001", 1536)
    texts = tuple(c.text for c in golden_chunks()) + tuple(
        q.query for q in load_golden_set().queries
    )
    assert cache.missing(texts) == ()


async def test_the_cached_embedder_refuses_text_it_has_never_seen() -> None:
    with pytest.raises(EmbeddingCacheMiss, match="refresh_golden_embeddings"):
        await CachedEmbedder(EmbeddingCache.load()).embed(("never embedded",))


# ---- the gate, on a real Qdrant server -------------------------------------


@pytest.fixture(scope="module")
def golden() -> GoldenSet:
    return load_golden_set()


@pytest.fixture
async def results(golden: GoldenSet) -> AsyncIterator[tuple[QueryRecall, ...]]:
    from qdrant_client import AsyncQdrantClient

    client = AsyncQdrantClient(url=_QDRANT_URL)
    collection = f"recall-gate-{uuid.uuid4().hex[:8]}"
    store = QdrantRetrievalStore(
        client,
        collection_name=collection,
        embedder=CachedEmbedder(EmbeddingCache.load()),
        ledger=FakeLedgerStore(),
    )
    try:
        await store.ensure_ready()
        await store.upsert_chunks(golden_chunks())
        yield await measure(RetrievalPipeline(store, NoRerank()), golden)
    finally:
        await client.delete_collection(collection)
        await client.close()


def _report(results: tuple[QueryRecall, ...]) -> None:
    print("\nrecall@10 by category (gated list: fused)")
    for category, lists in sorted(by_category(results).items()):
        cells = "  ".join(f"{name}={value:.3f}" for name, value in sorted(lists.items()))
        print(f"  {category.value:<13} bar={EXIT_BARS[category]:.1f}  {cells}")
    for r in results:
        if r.gated < 1.0:
            print(f"  miss {r.query_id} ({r.category.value}): {r.by_list}")


@_needs_qdrant
async def test_exact_id_and_paraphrase_meet_their_exit_bars(
    results: tuple[QueryRecall, ...],
) -> None:
    """Issue #28's enforced bars: exact-ID = 1.0, paraphrase >= 0.9."""
    _report(results)
    if os.environ.get("UPDATE_RECALL_BASELINE"):
        write_baseline(results)
    failing = below_exit_bar(results)
    assert {c: v for c, v in failing.items() if c is not Category.CROSS_CLAUSE} == {}


@_needs_qdrant
@pytest.mark.xfail(
    strict=True,
    reason="Measured 2026-10-08: fused cross_clause recall@10 = 0.792 against PHASES.md's "
    "0.8 (dense alone 0.896 — RRF demotes clauses the sparse channel misses). An unmet "
    "Phase 1 exit criterion, kept visible rather than lowered; the cross-encoder's effect "
    "is measured in #32. strict: once this passes, the marker must come off.",
)
async def test_cross_clause_meets_its_exit_bar(results: tuple[QueryRecall, ...]) -> None:
    assert Category.CROSS_CLAUSE not in below_exit_bar(results)


@_needs_qdrant
async def test_no_query_regressed_below_its_baseline(results: tuple[QueryRecall, ...]) -> None:
    """Every category, cross_clause included: a drop fails by query id."""
    assert regressions(results, load_baseline()) == {}
