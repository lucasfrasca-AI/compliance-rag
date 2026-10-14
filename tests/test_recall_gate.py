"""The regression gate (issues #28, #180): recall@10 over the signed golden
set, through `route()` on a real Qdrant server, on every build.

The corpus is the gate corpus: the whole compilation's 779 selected clauses
and the six Acme policies (`gate_chunks()`), not the 75-clause excerpt — the
excerpt was an easier problem than production (ADR-0043). Dense vectors are
real `gemini-embedding-001` output served from the checked-in cache
(`CachedEmbedder`), BM25 is computed locally, and the reranker is `NoRerank` —
so the gate needs no cloud identity, no network and no model artefact, and
nothing in it can call an LLM. Fusion is held to its floor in every cell
(`FLOORS`), and every list of every query to its checked-in baseline, which
fails the build by query id. Targets are on the delivered list, in
`test_recall_gate_reranked.py`.

CI's `ci` job sets `REQUIRE_RECALL_GATE`, so a missing Qdrant fails there
instead of skipping.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import subprocess
import sys

import pytest

from compliance_rag.adapters.interfaces import Candidate, Channel
from compliance_rag.eval.embedding_cache import CachedEmbedder, EmbeddingCache, EmbeddingCacheMiss
from compliance_rag.eval.golden import Category, GoldenQuery, GoldenSet, load_golden_set
from compliance_rag.eval.golden_corpus import embedded_texts, gate_chunks
from compliance_rag.eval.harness import golden_store
from compliance_rag.eval.recall import (
    FLOORS,
    FUSED,
    LAW,
    OPEN_TARGETS,
    POLICY,
    RERANKED,
    TARGETS,
    NoRerank,
    QueryRecall,
    UnsignedGoldenSet,
    below_floor,
    by_category,
    by_cell,
    corpus_sha256,
    load_baseline,
    load_baseline_corpus,
    measure,
    measure_trace,
    recall_at_k,
    regressions,
    short_of_target,
    target_of,
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
    measured = measure_trace(query, trace)
    assert measured.by_list == {"keyword": 1.0, FUSED: 1.0, "reranked": 0.0}
    assert measured.document_class is LAW


def test_a_drop_names_the_query_that_regressed() -> None:
    now = (
        QueryRecall("GQ-09", Category.PARAPHRASE, LAW, {FUSED: 1.0}),
        QueryRecall("GQ-18", Category.CROSS_CLAUSE, LAW, {FUSED: 2 / 3}),
        QueryRecall("GQ-26", Category.PARAPHRASE, LAW, {FUSED: 0.0}),  # new, not in baseline
    )
    baseline = {"GQ-09": {FUSED: 1.0}, "GQ-18": {FUSED: 1.0}}
    assert regressions(now, baseline) == {"GQ-18/fused": (1.0, 2 / 3)}


def test_a_channel_collapse_fails_even_when_fusion_hides_it() -> None:
    now = (QueryRecall("GQ-09", Category.PARAPHRASE, LAW, {"dense": 0.0, FUSED: 1.0}),)
    baseline = {"GQ-09": {"dense": 1.0, FUSED: 1.0}}
    assert regressions(now, baseline) == {"GQ-09/dense": (1.0, 0.0)}


def test_the_baseline_is_never_rewritten_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")
    with pytest.raises(RuntimeError, match="never rewritten in CI"):
        write_baseline((), corpus="")


def test_the_gate_cannot_reach_a_language_model() -> None:
    """D-12 / issue #28: everything the gate composes, imported and run in a
    fresh interpreter — no LLM SDK or generator module is ever loaded."""
    program = """
import asyncio, sys
import compliance_rag.adapters.qdrant_retrieval_store
from compliance_rag.adapters.fakes import FakeLedgerStore, FakeRetrievalStore
from compliance_rag.eval.embedding_cache import CachedEmbedder, EmbeddingCache
from compliance_rag.eval.golden import load_golden_set
from compliance_rag.eval.golden_corpus import gate_chunks
from compliance_rag.eval.harness import golden_store, in_process_reranker
from compliance_rag.eval.recall import NoRerank, measure
from compliance_rag.retrieval.pipeline import RetrievalPipeline

CachedEmbedder(EmbeddingCache.load())
store = FakeRetrievalStore(gate_chunks(), ledger=FakeLedgerStore())
asyncio.run(measure(RetrievalPipeline(store, NoRerank()), load_golden_set()))
llm = ("anthropic", "vertexai.generative")
loaded = [
    m for m in sys.modules
    if m.startswith(llm) or (m.startswith("compliance_rag") and "generator" in m)
]
assert not loaded, loaded
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]


def test_a_cell_is_one_lists_mean_over_one_category_of_one_document_class() -> None:
    now = (
        QueryRecall("GQ-09", Category.PARAPHRASE, LAW, {FUSED: 1.0}),
        QueryRecall("GQ-10", Category.PARAPHRASE, LAW, {FUSED: 0.0}),
        QueryRecall("GQ-15", Category.PARAPHRASE, POLICY, {FUSED: 1.0}),
    )
    assert by_cell(now, FUSED) == {
        (FUSED, Category.PARAPHRASE, LAW): 0.5,
        (FUSED, Category.PARAPHRASE, POLICY): 1.0,
    }


def test_a_cell_under_its_floor_is_reported_with_its_floor_and_value() -> None:
    """Law paraphrase's fused floor is 5/6: one of the six law queries may miss
    (GQ-10 does, today), two may not. Policy can't make up for it."""
    now = (
        *(QueryRecall(f"L{i}", Category.PARAPHRASE, LAW, {FUSED: 1.0}) for i in range(4)),
        *(QueryRecall(f"M{i}", Category.PARAPHRASE, LAW, {FUSED: 0.0}) for i in range(2)),
        *(QueryRecall(f"P{i}", Category.PARAPHRASE, POLICY, {FUSED: 1.0}) for i in range(3)),
    )
    cell = (FUSED, Category.PARAPHRASE, LAW)
    assert below_floor(now, FUSED) == {cell: (FLOORS[cell], 4 / 6)}


def test_a_cell_with_no_floor_fails_rather_than_going_unguarded() -> None:
    """Every cell the golden set produces has a deliberate floor."""
    cell = ("dense", Category.PARAPHRASE, LAW)
    now = (QueryRecall("GQ-09", Category.PARAPHRASE, LAW, {"dense": 1.0}),)
    assert below_floor(now, "dense") == {cell: (1.0, 1.0)}


def test_every_cell_has_a_floor_and_a_floor_never_exceeds_one() -> None:
    cells = {(n, c, d) for n in (FUSED, RERANKED) for c in Category for d in (LAW, POLICY)}
    assert set(FLOORS) == cells
    assert all(0.0 < floor <= 1.0 for floor in FLOORS.values())


def test_targets_are_on_the_delivered_list_only() -> None:
    """Nothing delivers fusion's order, so a fused cell has a floor and no
    target; the reranked list carries PHASES.md's bars."""
    assert target_of((FUSED, Category.CROSS_CLAUSE, LAW)) is None
    assert target_of((RERANKED, Category.CROSS_CLAUSE, LAW)) == TARGETS[Category.CROSS_CLAUSE]
    assert dict(TARGETS) == {
        Category.EXACT_ID: 1.0,
        Category.PARAPHRASE: 0.9,
        Category.CROSS_CLAUSE: 0.8,
    }


def test_the_open_target_is_law_cross_clause() -> None:
    """ADR-0043: today's one cell under its target. When it is met, its xfail
    in test_recall_gate_reranked.py passes, strict xfail fails the build, and
    raising its floor to the new value closes it here too."""
    assert OPEN_TARGETS == ((RERANKED, Category.CROSS_CLAUSE, LAW),)


def test_a_cell_short_of_its_target_reports_its_value() -> None:
    now = (
        QueryRecall("GQ-18", Category.CROSS_CLAUSE, LAW, {RERANKED: 1 / 3}),
        QueryRecall("GQ-19", Category.CROSS_CLAUSE, LAW, {RERANKED: 1.0}),
    )
    assert short_of_target(now, (RERANKED, Category.CROSS_CLAUSE, LAW)) == 2 / 3
    met = (QueryRecall("GQ-19", Category.CROSS_CLAUSE, LAW, {RERANKED: 1.0}),)
    assert short_of_target(met, (RERANKED, Category.CROSS_CLAUSE, LAW)) is None


async def test_an_unsigned_golden_set_is_refused() -> None:
    unsigned = load_golden_set().model_copy(update={"reviewed_by": "PENDING — reviewer"})
    pipeline = RetrievalPipeline(retrieval_store=None, reranker=NoRerank())  # pyright: ignore[reportArgumentType] — never reached
    with pytest.raises(UnsignedGoldenSet):
        await measure(pipeline, unsigned)


def test_every_golden_text_has_a_real_cached_vector() -> None:
    """Editing a query or re-parsing a clause without refreshing the cache
    fails here, naming the text, before the gate measures anything stale.
    Covers the gate corpus and the golden excerpt both."""
    cache = EmbeddingCache.load()
    assert (cache.model, cache.dimensions) == ("gemini-embedding-001", 1536)
    # A snapshot says what it is a snapshot of (scripts/check_embedding_drift.py).
    assert cache.task_type == "RETRIEVAL_DOCUMENT"
    assert cache.sdk.startswith("google-genai ")
    dt.date.fromisoformat(cache.generated_on)
    assert cache.missing(embedded_texts()) == ()


def test_the_baseline_covers_every_golden_query() -> None:
    """A query missing from the baseline is treated as new, so it would
    escape the regression check — every signed query must be in it."""
    assert set(load_baseline()) == {q.id for q in load_golden_set().queries}


def test_the_baseline_was_recorded_on_the_gate_corpus() -> None:
    """A baseline is only a bar for the corpus it was measured on. Re-exporting
    the clauses (a parser or manifest change) or editing a policy fixture fails
    here until the baseline is re-recorded, deliberately, on the new corpus."""
    assert load_baseline_corpus() == corpus_sha256(gate_chunks())


async def test_the_cached_embedder_refuses_text_it_has_never_seen() -> None:
    with pytest.raises(EmbeddingCacheMiss, match="refresh_golden_embeddings"):
        await CachedEmbedder(EmbeddingCache.load()).embed(("never embedded",))


# ---- the gate, on a real Qdrant server -------------------------------------


async def _measure(golden: GoldenSet) -> tuple[QueryRecall, ...]:
    async with golden_store(_QDRANT_URL, prefix="recall-gate", chunks=gate_chunks()) as store:
        return await measure(RetrievalPipeline(store, NoRerank()), golden)


@pytest.fixture(scope="module")
def results() -> tuple[QueryRecall, ...]:
    """Measured once for the module: ~800 chunks are upserted per measurement."""
    return asyncio.run(_measure(load_golden_set()))


def _report(results: tuple[QueryRecall, ...]) -> None:
    print("\nrecall@10 by category, fused gate (all lists)")
    for category, lists in sorted(by_category(results).items()):
        cells = "  ".join(f"{name}={value:.3f}" for name, value in sorted(lists.items()))
        print(f"  {category.value:<13} {cells}")
    for cell, now in sorted(by_cell(results, FUSED).items()):
        print(f"  {cell[1].value:<13} {cell[2].value:<16} floor={FLOORS[cell]:.3f}  {now:.3f}")
    for r in results:
        if r.by_list[FUSED] < 1.0:
            print(f"  miss {r.query_id} ({r.category.value}): {r.by_list}")


@_needs_qdrant
def test_fusion_holds_its_floor_in_every_cell(results: tuple[QueryRecall, ...]) -> None:
    """ADR-0043: each category, law and policy apart, at what it achieves on
    the whole compilation — so any drop fails, and a gain in one half can't
    hide a loss in the other."""
    _report(results)
    assert below_floor(results, FUSED) == {}


@_needs_qdrant
def test_no_query_regressed_below_its_baseline(results: tuple[QueryRecall, ...]) -> None:
    """Every category, every list: a drop fails by query id and list."""
    if os.environ.get("UPDATE_RECALL_BASELINE"):
        write_baseline(results, corpus=corpus_sha256(gate_chunks()))
    assert regressions(results, load_baseline()) == {}
