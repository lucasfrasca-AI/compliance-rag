"""The bars on what `retrieve()` actually ranks and delivers (issues #32,
#180, #200), on the gate corpus (the whole compilation and the Acme policies),
per category and document class. Two metrics, reported and gated apart, never
combined into one number:

- **recall@10 of the cross-encoder's ranking** over the top
  `RERANK_CANDIDATES` fused candidates: the retrieval metric, since
  retrieval's job is to rank. The targets are here.
- **Delivered-recall** (ADR-0045): the share of the expected clauses in those
  ten each delivered whole, every library-law clause with the chapeaux and
  paragraphs of its provision. That list holds more than ten clauses, and the
  report prints how many beside it. It measures delivery, not ranking, so it
  has floors and no target.

The fused gate (`test_recall_gate.py`) runs on every build with no model; this
one needs the int8 cross-encoder, so it runs in CI's `reranker-fidelity` job
(`REQUIRE_REAL_RETRIEVE` turns a missing model or Qdrant into a failure). The
reranker sits behind its own FastAPI app and the real `RerankerClient`, over
in-process ASGI — the production code path, no network.

Two kinds of bar (ADR-0043):

- **Floors**, enforced on both metrics: every cell at what it achieves today,
  so any drop fails. No per-query baseline here: the int8 model scores in
  batches with dynamic activation quantisation, so a query's ranking can
  depend on the platform and on the other candidates. Per-query drops are held
  on the fused gate, which is platform-exact.
- **Targets**, what Phase 1 needs (PHASES.md), on the reranked list only
  here (fused paraphrase's is in `test_recall_gate.py`). A cell under its
  target is expected to fail, strictly: the day it meets the target the build
  fails (XPASS) until its floor is raised to the new value, which closes the
  target. A target is not a floor, so an open one doesn't hold the phase's
  exit (ADR-0043).
"""

from __future__ import annotations

import asyncio
import os

import pytest

from compliance_rag.eval.golden import load_golden_set
from compliance_rag.eval.golden_corpus import gate_chunks
from compliance_rag.eval.harness import golden_store, in_process_reranker
from compliance_rag.eval.recall import (
    DELIVERED,
    RERANKED,
    Cell,
    QueryRecall,
    below_floor,
    by_cell,
    describe_cell,
    measure,
    open_targets_on,
    short_of_target,
)
from compliance_rag.reranker_service.model import DEFAULT_ONNX_MODEL_PATH, RerankerModel
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from tests.conftest import qdrant_reachable

_QDRANT_URL = os.environ.get("QDRANT_URL", "")
_REQUIRED = bool(os.environ.get("REQUIRE_REAL_RETRIEVE"))

pytestmark = pytest.mark.skipif(
    not _REQUIRED and not (qdrant_reachable(_QDRANT_URL) and DEFAULT_ONNX_MODEL_PATH.exists()),
    reason="needs a real Qdrant server (QDRANT_URL) and the converted reranker model",
)


async def _measure() -> tuple[QueryRecall, ...]:
    async with (
        golden_store(_QDRANT_URL, prefix="recall-reranked", chunks=gate_chunks()) as store,
        in_process_reranker(RerankerModel()) as reranker,
    ):
        return await measure(RetrievalPipeline(store, reranker), load_golden_set())


@pytest.fixture(scope="module")
def results() -> tuple[QueryRecall, ...]:
    """Measured once for the module: reranking is the slow part."""
    measured = asyncio.run(_measure())
    print("\nrecall@10 of the reranked list, by category and document class")
    for cell, now in sorted(by_cell(measured, RERANKED).items()):
        print(f"  {describe_cell(cell, now)}")
    for r in measured:
        if r.by_list[RERANKED] < 1.0:
            print(f"  reranked miss {r.query_id} ({r.category.value}): {r.by_list[RERANKED]:.3f}")
    print("delivered-recall, a separate metric: the ten delivered whole (ADR-0045)")
    for cell, now in sorted(by_cell(measured, DELIVERED).items()):
        print(f"  {describe_cell(cell, now)}")
    for r in measured:
        if r.by_list[DELIVERED] < 1.0:
            print(f"  delivered miss {r.query_id} ({r.category.value}): {r.by_list[DELIVERED]:.3f}")
    sizes = [r.delivered_clauses for r in measured]
    print(f"  delivered clauses per query: mean {sum(sizes) / len(sizes):.1f}, max {max(sizes)}")
    return measured


def test_reranked_recall_holds_its_floor_in_every_cell(
    results: tuple[QueryRecall, ...],
) -> None:
    """The retrieval metric, the ranker's own top 10: a ranking regression
    fails here even where delivering whole would mask it."""
    assert below_floor(results, RERANKED) == {}


def test_delivered_recall_holds_its_floor_in_every_cell(
    results: tuple[QueryRecall, ...],
) -> None:
    """Delivered-recall, what generation is given (ADR-0045): its own floors,
    no target."""
    assert below_floor(results, DELIVERED) == {}


@pytest.mark.parametrize(
    "cell",
    [
        pytest.param(
            cell,
            id=f"{cell.category.value}-{cell.document_class.value}",
            marks=pytest.mark.xfail(
                strict=True,
                raises=AssertionError,
                reason="an open target (ADR-0043): when this passes, raise the cell's floor",
            ),
        )
        for cell in open_targets_on(RERANKED)
    ],
)
def test_an_open_target_is_met(results: tuple[QueryRecall, ...], cell: Cell) -> None:
    """Expected to fail until the work is done; `raises=AssertionError`, so a
    broken measurement errors instead of passing as an expected failure."""
    assert short_of_target(results, cell) is None, f"{cell}: {by_cell(results, RERANKED)[cell]:.3f}"
