"""The bars on what `retrieve()` actually delivers (issues #32, #180, #200):
recall@10 of the cross-encoder's ranking over the top `RERANK_CANDIDATES` fused
candidates, and of those ten delivered whole (each library-law clause with the
chapeaux and paragraphs of its provision, ADR-0045), on the gate corpus (the
whole compilation and the Acme policies), per category and document class.

The fused gate (`test_recall_gate.py`) runs on every build with no model; this
one needs the int8 cross-encoder, so it runs in CI's `reranker-fidelity` job
(`REQUIRE_REAL_RETRIEVE` turns a missing model or Qdrant into a failure). The
reranker sits behind its own FastAPI app and the real `RerankerClient`, over
in-process ASGI — the production code path, no network.

Two kinds of bar (ADR-0043):

- **Floors**, enforced: every cell at what it achieves today, so any drop fails.
  No per-query baseline here: the int8 model scores in batches with dynamic
  activation quantisation, so a query's ranking can depend on the platform and
  on the other candidates. Per-query drops are held on the fused gate, which is
  platform-exact.
- **Targets**, what Phase 1 needs (PHASES.md), on the delivered list. A cell
  under its target is expected to fail, strictly: the day it meets the target
  the build fails (XPASS) until its floor is raised to the new value, which
  closes the target. The delivered list holds more than ten clauses; the
  report prints how many, query by query, beside its recall.
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
    OPEN_TARGETS,
    RERANKED,
    Cell,
    QueryRecall,
    below_floor,
    by_cell,
    describe_cell,
    measure,
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
    print("\nrecall@10 by category and document class: reranked, then delivered whole")
    for list_name in (RERANKED, DELIVERED):
        for cell, now in sorted(by_cell(measured, list_name).items()):
            print(f"  {describe_cell(cell, now)}")
    for r in measured:
        if r.by_list[DELIVERED] < 1.0 or r.by_list[RERANKED] < 1.0:
            print(f"  miss {r.query_id} ({r.category.value}): {r.by_list}")
    sizes = [r.delivered_clauses for r in measured]
    print(f"  delivered clauses per query: mean {sum(sizes) / len(sizes):.1f}, max {max(sizes)}")
    return measured


def test_reranked_recall_holds_its_floor_in_every_cell(
    results: tuple[QueryRecall, ...],
) -> None:
    """The ranker's own top 10: a ranking regression fails here even where
    delivering whole would mask it."""
    assert below_floor(results, RERANKED) == {}


def test_delivered_recall_holds_its_floor_in_every_cell(
    results: tuple[QueryRecall, ...],
) -> None:
    """What generation is given (ADR-0045)."""
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
        for cell in OPEN_TARGETS
    ],
)
def test_an_open_target_is_met(results: tuple[QueryRecall, ...], cell: Cell) -> None:
    """Expected to fail until the work is done; `raises=AssertionError`, so a
    broken measurement errors instead of passing as an expected failure."""
    assert short_of_target(results, cell) is None, (
        f"{cell}: {by_cell(results, cell.list_name)[cell]:.3f}"
    )
