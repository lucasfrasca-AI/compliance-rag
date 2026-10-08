"""How `retrieve()` is wired (issue #25), checked without a server: the
residency of the reranker endpoint is enforced before any cloud client is
built, and nothing on the retrieval path can reach a text-generating model.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from compliance_rag.adapters.fakes import FakeLedgerStore
from compliance_rag.adapters.reranker_http_client import RerankerResidencyError
from compliance_rag.adapters.vertex_embedder import VertexEmbedder
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import Settings


def test_a_reranker_outside_sydney_is_refused_before_any_cloud_client_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_run(cls: type[VertexEmbedder], settings: Settings) -> VertexEmbedder:
        raise AssertionError("a cloud client was built before residency was checked")

    monkeypatch.setattr(VertexEmbedder, "from_settings", classmethod(must_not_run))
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg
        reranker_url="https://reranker-abc-us-central1.a.run.app",
    )
    with pytest.raises(RerankerResidencyError):
        RetrievalPipeline.from_settings(settings, ledger=FakeLedgerStore())


def test_the_retrieval_path_cannot_reach_a_language_model() -> None:
    """D-12 / issue #25. Imports the pipeline and every adapter
    `from_settings` composes, then runs `retrieve()` and `route()`, in a
    fresh interpreter — so another test having imported an LLM SDK can't
    mask it — and asserts no LLM SDK or generator module was ever loaded."""
    program = """
import asyncio, datetime as dt, sys
from compliance_rag.retrieval.pipeline import RetrievalPipeline
import compliance_rag.adapters.qdrant_retrieval_store
import compliance_rag.adapters.reranker_client
import compliance_rag.adapters.reranker_http_client
import compliance_rag.adapters.vertex_embedder
from compliance_rag.adapters.fakes import FakeLedgerStore, FakeReranker, FakeRetrievalStore
from compliance_rag.corpus.models import Chunk

chunk = Chunk(chunk_id="c1", clause_id="privacy-act-1988:V1:sch1/app11/1", text="protect it")
store = FakeRetrievalStore((chunk,), ledger=FakeLedgerStore())
pipeline = RetrievalPipeline(store, FakeReranker())
asyncio.run(pipeline.retrieve("protect it", "_public", dt.date(2026, 1, 1)))
asyncio.run(pipeline.route(chunk.clause_id, "_public", dt.date(2026, 1, 1)))
llm = ("anthropic", "anthropic.", "vertexai.generative")
loaded = [
    m for m in sys.modules
    if m.startswith(llm) or (m.startswith("compliance_rag") and "generator" in m)
]
print(loaded)
assert not loaded, loaded
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
