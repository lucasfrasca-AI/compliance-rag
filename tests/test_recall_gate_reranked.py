"""The exit bars on what `retrieve()` actually delivers (issue #32): recall@10
of the cross-encoder's ranking over the top `RERANK_CANDIDATES` fused
candidates, per golden-set category.

The fused gate (`test_recall_gate.py`) runs on every build with no model; this
one needs the int8 cross-encoder, so it runs in CI's `reranker-fidelity` job
(`REQUIRE_REAL_RETRIEVE` turns a missing model or Qdrant into a failure). The
reranker sits behind its own FastAPI app and the real `RerankerClient`, over
in-process ASGI — the production code path, no network.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from compliance_rag.adapters.fakes import FakeLedgerStore
from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
from compliance_rag.adapters.reranker_client import RerankerClient
from compliance_rag.adapters.reranker_http_client import build_reranker_http_client
from compliance_rag.eval.embedding_cache import CachedEmbedder, EmbeddingCache
from compliance_rag.eval.golden import Category, load_golden_set
from compliance_rag.eval.golden_corpus import golden_chunks
from compliance_rag.eval.recall import EXIT_BARS, RERANKED, QueryRecall, by_category, measure
from compliance_rag.reranker_service.app import create_app
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
    from qdrant_client import AsyncQdrantClient

    client = AsyncQdrantClient(url=_QDRANT_URL)
    collection = f"recall-reranked-{uuid.uuid4().hex[:8]}"
    store = QdrantRetrievalStore(
        client,
        collection_name=collection,
        embedder=CachedEmbedder(EmbeddingCache.load()),
        ledger=FakeLedgerStore(),
    )
    http = build_reranker_http_client(
        "http://localhost",
        timeout_seconds=600,
        transport=httpx.ASGITransport(app=create_app(RerankerModel())),
    )
    try:
        await store.ensure_ready()
        await store.upsert_chunks(golden_chunks())
        yield await measure(RetrievalPipeline(store, RerankerClient(http)), load_golden_set())
    finally:
        await http.aclose()
        await client.delete_collection(collection)
        await client.close()


async def test_reranked_recall_at_10_meets_every_phase_1_exit_bar(
    results: tuple[QueryRecall, ...],
) -> None:
    summary = by_category(results)
    print("\nreranked recall@10 by category")
    for category, lists in sorted(summary.items()):
        print(f"  {category.value:<13} bar={EXIT_BARS[category]:.1f}  {lists[RERANKED]:.3f}")
    below = {c: summary[c][RERANKED] for c in Category if summary[c][RERANKED] < EXIT_BARS[c]}
    assert below == {}
