"""`retrieve()` with its fakes replaced (issue #25): the real Qdrant store on a
real Qdrant server, the real router, and the real reranker — the actual int8
ONNX cross-encoder behind the actual FastAPI service, reached through
`RerankerClient` over HTTP (in-process via `httpx.ASGITransport`, the same
request/response path a container would serve, minus the TCP socket).

Dense vectors come from `FakeEmbedder`: embedding quality isn't this ticket's
subject, and the real embedder needs cloud credentials this test shouldn't
hold. The recall gate (#28) measures real embeddings.

Runs where both a Qdrant server and the converted model exist — locally, and
in CI's `reranker-fidelity` job, which has both.
"""

from __future__ import annotations

import datetime as dt
import os
import socket
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.fakes import FakeEmbedder, FakeLedgerStore
from compliance_rag.adapters.interfaces import Channel
from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
from compliance_rag.adapters.reranker_client import RerankerClient
from compliance_rag.corpus.chunker import chunk_clauses
from compliance_rag.corpus.models import PUBLIC_TENANT_ID, Scope
from compliance_rag.corpus.parser import parse_compilation
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.reranker_service.app import create_app
from compliance_rag.reranker_service.model import DEFAULT_ONNX_MODEL_PATH, RerankerModel
from compliance_rag.retrieval import pipeline as pipeline_module
from compliance_rag.retrieval.pipeline import RetrievalPipeline

_QDRANT_URL = os.environ.get("QDRANT_URL", "")
FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "docs/research/fixtures/privacy-act-1988-C2026C00227.excerpt.html"
)
AS_OF = dt.date(2026, 10, 1)
APP11_2 = "privacy-act-1988:C2026C00227:sch1/app11/2"


def _qdrant_reachable() -> bool:
    if not _QDRANT_URL:
        return False
    parsed = urlparse(_QDRANT_URL)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 6333), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not (_qdrant_reachable() and DEFAULT_ONNX_MODEL_PATH.exists()),
    reason="needs a real Qdrant server (QDRANT_URL) and the converted reranker model",
)


@pytest.fixture(scope="module")
def reranker_model() -> RerankerModel:
    return RerankerModel()


@pytest.fixture
async def pipeline(reranker_model: RerankerModel) -> AsyncIterator[RetrievalPipeline]:
    from qdrant_client import AsyncQdrantClient

    store = QdrantRetrievalStore(
        AsyncQdrantClient(url=_QDRANT_URL),
        collection_name=f"clauses-retrieve-{uuid.uuid4().hex[:8]}",
        embedder=FakeEmbedder(dimensions=8),
        ledger=FakeLedgerStore(),
    )
    await store.ensure_ready()
    clauses = parse_compilation(
        FIXTURE.read_text(encoding="utf-8"),
        instrument_id="privacy-act-1988",
        version_id="C2026C00227",
        tenant_id=PUBLIC_TENANT_ID,
        scope=Scope.PUBLIC,
    ).clauses
    await store.upsert_chunks(chunk_clauses(clauses, count_tokens=count_reranker_tokens))
    transport = httpx.ASGITransport(app=create_app(reranker_model))
    async with httpx.AsyncClient(transport=transport, base_url="http://reranker") as http:
        yield RetrievalPipeline(store, RerankerClient(http))


async def test_retrieve_runs_both_channels_fusion_and_the_real_cross_encoder(
    pipeline: RetrievalPipeline,
) -> None:
    trace = await pipeline.retrieve(
        "What must an entity do with personal information it no longer needs?",
        PUBLIC_TENANT_ID,
        AS_OF,
    )
    assert trace.per_channel[Channel.DENSE]
    assert trace.per_channel[Channel.SPARSE]
    assert trace.fused
    assert trace.reranked
    # The cross-encoder, not fusion order, decides the answer: it reorders
    # the fused list, and lands in 11.2 — the destroy-or-de-identify duty or
    # its "no longer needs" condition, both honest answers to this wording.
    assert [d.index for d in trace.reranked] != list(range(len(trace.reranked)))
    top = trace.top_candidate()
    assert top is not None
    assert top.clause_id.startswith(APP11_2)


async def test_the_retrieve_span_carries_every_stage(
    pipeline: RetrievalPipeline, span_exporter: InMemorySpanExporter
) -> None:
    query = "What must an entity do with personal information it no longer needs?"
    await pipeline.retrieve(query, PUBLIC_TENANT_ID, AS_OF)
    spans = {s.name: s for s in span_exporter.get_finished_spans()}
    attributes = dict(spans["retrieve"].attributes or {})
    assert attributes["query"] == query
    for key in (
        "retrieval.channel.dense",
        "retrieval.channel.sparse",
        "retrieval.fused",
        "retrieval.rerank_scores",
    ):
        assert attributes[key], key


async def test_a_clause_id_query_routes_to_the_keyword_channel_on_real_qdrant(
    pipeline: RetrievalPipeline,
) -> None:
    trace = await pipeline.route(APP11_2, PUBLIC_TENANT_ID, AS_OF)
    assert set(trace.per_channel) == {Channel.KEYWORD}
    assert [c.clause_id for c in trace.fused] == [APP11_2]


def test_no_llm_is_reachable_from_the_retrieval_path() -> None:
    """D-12 / issue #25: retrieval never calls a model that writes text."""
    source = Path(pipeline_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("Generator", "anthropic", "vertex_claude"):
        assert forbidden not in source, forbidden
