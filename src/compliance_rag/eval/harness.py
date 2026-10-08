"""The shared setup for measuring retrieval on the golden set (#28, #32): the
golden corpus loaded into a throwaway Qdrant collection with real cached
vectors, and the reranker service run in-process behind the real client.

One place, so the recall gates and the benchmark scripts can't drift apart.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx

from compliance_rag.adapters.fakes import FakeLedgerStore
from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
from compliance_rag.adapters.reranker_client import RerankerClient
from compliance_rag.adapters.reranker_http_client import build_reranker_http_client
from compliance_rag.eval.embedding_cache import CachedEmbedder, EmbeddingCache
from compliance_rag.eval.golden_corpus import golden_chunks
from compliance_rag.reranker_service.app import create_app
from compliance_rag.reranker_service.model import RerankerModel


@asynccontextmanager
async def golden_store(qdrant_url: str, *, prefix: str) -> AsyncGenerator[QdrantRetrievalStore]:
    """The golden corpus on a throwaway collection, with the real
    `gemini-embedding-001` snapshot as its embedder."""
    async with QdrantRetrievalStore.ephemeral(
        qdrant_url,
        embedder=CachedEmbedder(EmbeddingCache.load()),
        ledger=FakeLedgerStore(),
        prefix=prefix,
    ) as store:
        await store.upsert_chunks(golden_chunks())
        yield store


@asynccontextmanager
async def in_process_reranker(model: RerankerModel) -> AsyncGenerator[RerankerClient]:
    """The reranker service's own FastAPI app behind the real
    `RerankerClient`, over in-process ASGI: the production code path, no
    network."""
    http = build_reranker_http_client(
        "http://localhost",
        timeout_seconds=600,
        transport=httpx.ASGITransport(app=create_app(model)),
    )
    try:
        yield RerankerClient(http)
    finally:
        await http.aclose()
