"""`retrieve()` with its fakes replaced (issue #25), built by
`RetrievalPipeline.from_settings` exactly as production builds it: the real
Qdrant store on a real Qdrant server, the real router, and the real reranker
client talking HTTP over TCP to the real reranker service — the int8 ONNX
cross-encoder behind the FastAPI app, started on a local port for the test.

Dense vectors come from `TieFreeEmbedder` (patched in for `VertexEmbedder`):
embedding quality isn't this ticket's subject, and the real embedder needs
cloud credentials this test must not hold. The recall gate (#28) measures
real embeddings. Tie-free matters: `FakeEmbedder`'s bag-of-buckets vectors
give every chunk sharing no token with the query the same score, Qdrant
breaks such ties differently run to run, so the 50 candidates handed to the
reranker changed between runs — and the int8 cross-encoder's scores depend on
its batch (BUILD_LOG 2026-10-08), which flipped the top hit on one CI run.

Runs where a Qdrant server and the converted model both exist — locally, and
in CI's `reranker-fidelity` job, which sets `REQUIRE_REAL_RETRIEVE` so a
missing prerequisite fails there instead of skipping.
"""

from __future__ import annotations

import datetime as dt
import os
import random
import socket
import threading
import time
import uuid
import zlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import uvicorn
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.fakes import FakeEmbedder, FakeLedgerStore
from compliance_rag.adapters.interfaces import Channel
from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
from compliance_rag.adapters.vertex_embedder import VertexEmbedder
from compliance_rag.corpus.chunker import chunk_clauses
from compliance_rag.corpus.models import PUBLIC_TENANT_ID, Scope
from compliance_rag.corpus.parser import parse_compilation
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.reranker_service.app import create_app
from compliance_rag.reranker_service.model import DEFAULT_ONNX_MODEL_PATH, RerankerModel
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import Settings
from tests.conftest import qdrant_reachable

_QDRANT_URL = os.environ.get("QDRANT_URL", "")
_REQUIRED = bool(os.environ.get("REQUIRE_REAL_RETRIEVE"))
FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "docs/research/fixtures/privacy-act-1988-C2026C00227.excerpt.html"
)
AS_OF = dt.date(2026, 10, 1)
APP11_2 = "privacy-act-1988:C2026C00227:sch1/app11/2"
QUERY = "What must an entity do with personal information it no longer needs?"

pytestmark = pytest.mark.skipif(
    not _REQUIRED and not (qdrant_reachable(_QDRANT_URL) and DEFAULT_ONNX_MODEL_PATH.exists()),
    reason="needs a real Qdrant server (QDRANT_URL) and the converted reranker model",
)


class TieFreeEmbedder(FakeEmbedder):
    """`FakeEmbedder` plus a small fixed per-text component, seeded from the
    text's crc32, so no two chunks score the same against a query and the
    candidate set is identical on every run and every platform."""

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        base = await super().embed(texts)
        out: list[tuple[float, ...]] = []
        for text, vector in zip(texts, base, strict=True):
            rng = random.Random(zlib.crc32(text.encode()))
            mixed = [v + 0.05 * rng.uniform(-1.0, 1.0) for v in vector]
            norm = sum(v * v for v in mixed) ** 0.5
            out.append(tuple(v / norm for v in mixed))
        return tuple(out)


@pytest.fixture(scope="module")
def reranker_url() -> Iterator[str]:
    """The real reranker service, listening on a free local port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(RerankerModel()), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("reranker service did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
async def pipeline(
    reranker_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[RetrievalPipeline]:
    def fake_embedder(cls: type[VertexEmbedder], settings: Settings) -> FakeEmbedder:
        return TieFreeEmbedder(dimensions=64)

    monkeypatch.setattr(VertexEmbedder, "from_settings", classmethod(fake_embedder))
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg
        qdrant_url=_QDRANT_URL,
        qdrant_collection=f"clauses-retrieve-{uuid.uuid4().hex[:8]}",
        reranker_url=reranker_url,
    )
    built = RetrievalPipeline.from_settings(settings, ledger=FakeLedgerStore())
    store = built.retrieval_store
    assert isinstance(store, QdrantRetrievalStore)
    await store.ensure_ready()
    clauses = parse_compilation(
        FIXTURE.read_text(encoding="utf-8"),
        instrument_id="privacy-act-1988",
        version_id="C2026C00227",
        tenant_id=PUBLIC_TENANT_ID,
        scope=Scope.PUBLIC,
    ).clauses
    await store.upsert_chunks(chunk_clauses(clauses, count_tokens=count_reranker_tokens))
    yield built


async def test_retrieve_runs_both_channels_fusion_and_the_real_cross_encoder(
    pipeline: RetrievalPipeline,
) -> None:
    trace = await pipeline.retrieve(QUERY, PUBLIC_TENANT_ID, AS_OF)
    assert trace.per_channel[Channel.DENSE]
    assert trace.per_channel[Channel.SPARSE]
    assert trace.fused
    assert trace.reranked
    # The cross-encoder, not fusion order, decides the order: it reorders the
    # fused list, and puts 11.2 — the destroy-or-de-identify duty or its "no
    # longer needs" condition, both honest answers — in its top three. Not
    # top-1: on this fixed batch the int8 model ranks APP 10.2 first, 11.2(b)
    # and 11.2 second and third (BUILD_LOG 2026-10-08). Whether the reranker
    # ranks well is #32's measurement on real embeddings, not this wiring test's.
    assert [d.index for d in trace.reranked] != list(range(len(trace.reranked)))
    top_three = trace.ranked_candidates()[:3]
    assert any(c.clause_id.startswith(APP11_2) for c in top_three)


async def test_the_retrieve_span_carries_every_stage(
    pipeline: RetrievalPipeline, span_exporter: InMemorySpanExporter
) -> None:
    await pipeline.retrieve(QUERY, PUBLIC_TENANT_ID, AS_OF)
    spans = {s.name: s for s in span_exporter.get_finished_spans()}
    attributes = dict(spans["retrieve"].attributes or {})
    assert attributes["query"] == QUERY
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
