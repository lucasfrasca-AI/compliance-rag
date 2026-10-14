"""The exit bars on what `retrieve()` actually delivers (issue #32): recall@10
of the cross-encoder's ranking over the top `RERANK_CANDIDATES` fused
candidates, per golden-set category.

The fused gate (`test_recall_gate.py`) runs on every build with no model; this
one needs the int8 cross-encoder, so it runs in CI's `reranker-fidelity` job
(`REQUIRE_REAL_RETRIEVE` turns a missing model or Qdrant into a failure). The
reranker sits behind its own FastAPI app and the real `RerankerClient`, over
in-process ASGI — the production code path, no network.

Gated on category means only, with no per-query baseline: CI's x86 int8
numerics needn't reproduce a locally recorded per-query ranking exactly, and
the bars carry the margin (cross-clause 0.896 against 0.8). Per-query drops
are held on the fused gate, which is platform-exact.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from compliance_rag.eval.golden import Category, load_golden_set
from compliance_rag.eval.golden_corpus import gate_chunks
from compliance_rag.eval.harness import golden_store, in_process_reranker
from compliance_rag.eval.recall import EXIT_BARS, RERANKED, QueryRecall, by_category, measure
from compliance_rag.reranker_service.model import DEFAULT_ONNX_MODEL_PATH, RerankerModel
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from tests.conftest import qdrant_reachable

_QDRANT_URL = os.environ.get("QDRANT_URL", "")
_REQUIRED = bool(os.environ.get("REQUIRE_REAL_RETRIEVE"))

pytestmark = pytest.mark.skipif(
    not _REQUIRED and not (qdrant_reachable(_QDRANT_URL) and DEFAULT_ONNX_MODEL_PATH.exists()),
    reason="needs a real Qdrant server (QDRANT_URL) and the converted reranker model",
)


@pytest.fixture
async def results() -> AsyncIterator[tuple[QueryRecall, ...]]:
    async with (
        golden_store(_QDRANT_URL, prefix="recall-reranked", chunks=gate_chunks()) as store,
        in_process_reranker(RerankerModel()) as reranker,
    ):
        yield await measure(RetrievalPipeline(store, reranker), load_golden_set())


async def test_reranked_recall_at_10_meets_every_phase_1_exit_bar(
    results: tuple[QueryRecall, ...],
) -> None:
    summary = by_category(results)
    print("\nreranked recall@10 by category")
    for category, lists in sorted(summary.items()):
        print(f"  {category.value:<13} bar={EXIT_BARS[category]:.1f}  {lists[RERANKED]:.3f}")
    below = {c: summary[c][RERANKED] for c in Category if summary[c][RERANKED] < EXIT_BARS[c]}
    assert below == {}
