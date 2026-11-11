"""The live answer eval's own wiring (#333), with fakes and no network.

`eval/live.py::live_answer_pipeline` is what the live eval and the demo run.
Two defects in it surfaced on the first live Gemini runs (#29, #31), neither a
model defect:

- its vector store filtered by subscriptions read from an empty ledger of its
  own, while `answer()` and the verifier used `golden_ledger()`, so GE-01's
  tenant, subscribed to the Act, still retrieved no law;
- `--injection` adds a poisoned synthetic chunk the checked-in vector snapshot
  can't hold, and the snapshot's embedder refused it before any question.

Only the stages that leave the process are replaced: Qdrant by the fake store
(which reads subscriptions from its ledger exactly as Qdrant does, and here
also embeds what it stores and searches, as Qdrant does), the cross-encoder by
the fake reranker, Gemini by the fake generator, and the live embedder by a
recording fake. `golden_store`, the ledger, the clause store, the verifier and
the snapshot embedder are the real ones.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from scripts.run_answer_eval import (
    INJECTION_QUESTIONS,
    POISON_CHUNK,
    POISON_CLAUSE,
    environment_row,
)

from compliance_rag.adapters.fakes import (
    FakeEmbedder,
    FakeGenerator,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.interfaces import (
    NO_PREFIX,
    CitationVerifier,
    ClauseStore,
    Embedder,
    LawScope,
    LedgerStore,
    PolicyCorpus,
    RetrievalAdmin,
    RetrievalResult,
)
from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
from compliance_rag.adapters.vertex_embedder import VertexEmbedder
from compliance_rag.assessment.pipeline import LIBRARY_QUESTION, AssessmentPipeline
from compliance_rag.corpus.models import PUBLIC_TENANT_ID, Chunk, Scope
from compliance_rag.eval import harness, live
from compliance_rag.eval.embedding_cache import EmbeddingCache
from compliance_rag.eval.environment_cases import load_environment_cases
from compliance_rag.eval.golden import load_golden_set
from compliance_rag.eval.golden_corpus import LAW_INSTRUMENT_ID, POLICY_TENANT_ID, golden_chunks
from compliance_rag.settings import Settings
from compliance_rag.verifier.citations import Verifier

LAW_QUESTION = next(q for q in load_golden_set().queries if q.id == "GQ-09")
"""A plain-language question whose answer is law (APP 11.1(a))."""


class _EmbeddingStore(FakeRetrievalStore):
    """The fake store, embedding what it stores and what it is asked, as
    Qdrant does: a text its embedder can't serve fails here as it fails on
    Qdrant."""

    def __init__(self, *, ledger: LedgerStore, embedder: Embedder) -> None:
        super().__init__(ledger=ledger)
        self.ledger = ledger
        self._embedder = embedder

    async def upsert_chunks(self, chunks: tuple[Chunk, ...]) -> None:
        await self._embedder.embed(tuple(c.text for c in chunks))
        await super().upsert_chunks(chunks)

    async def search(
        self,
        query: str,
        *,
        law: LawScope,
        tenant_id: str,
        scopes: tuple[Scope, ...],
        as_of: dt.date,
        limit: int,
        policy_corpus: PolicyCorpus = NO_PREFIX,
        user_id: str | None = None,
    ) -> RetrievalResult:
        await self._embedder.embed((query,))
        return await super().search(
            query,
            law=law,
            tenant_id=tenant_id,
            scopes=scopes,
            as_of=as_of,
            limit=limit,
            policy_corpus=policy_corpus,
            user_id=user_id,
        )


class _LiveEmbedder(FakeEmbedder):
    """Stands in for `gemini-embedding-001` on Vertex; records what it is asked."""

    def __init__(self, dimensions: int) -> None:
        super().__init__(dimensions)
        self.asked: list[str] = []

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.asked.extend(texts)
        return await super().embed(texts)


class _Offline:
    """What the patched live pipeline opened: its store, and the live embedder
    if one was built."""

    def __init__(self) -> None:
        self.stores: list[_EmbeddingStore] = []
        self.live: list[_LiveEmbedder] = []
        self.verifier_ledgers: list[LedgerStore] = []

    @property
    def store(self) -> _EmbeddingStore:
        (store,) = self.stores
        return store


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> _Offline:
    opened = _Offline()
    dimensions = EmbeddingCache.load().dimensions

    @asynccontextmanager
    async def ephemeral(
        url: str,
        *,
        embedder: Embedder,
        ledger: LedgerStore,
        prefix: str,
        api_key: object = None,
    ) -> AsyncGenerator[_EmbeddingStore]:
        store = _EmbeddingStore(ledger=ledger, embedder=embedder)
        opened.stores.append(store)
        yield store

    @asynccontextmanager
    async def reranker(model: object) -> AsyncGenerator[FakeReranker]:
        yield FakeReranker()

    async def generator(settings: Settings, *, corpus: RetrievalAdmin) -> FakeGenerator:
        return FakeGenerator()

    def vertex(settings: Settings) -> _LiveEmbedder:
        embedder = _LiveEmbedder(dimensions)
        opened.live.append(embedder)
        return embedder

    class RecordedVerifier(Verifier):
        def __init__(
            self, clause_store: ClauseStore, ledger: LedgerStore, comparator: CitationVerifier
        ) -> None:
            super().__init__(clause_store, ledger, comparator)
            opened.verifier_ledgers.append(ledger)

    monkeypatch.setattr(QdrantRetrievalStore, "ephemeral", ephemeral)
    monkeypatch.setattr(live, "Verifier", RecordedVerifier)
    monkeypatch.setattr(live, "RerankerModel", object)
    monkeypatch.setattr(live, "in_process_reranker", reranker)
    monkeypatch.setattr(live, "build_generator", generator)
    monkeypatch.setattr(VertexEmbedder, "from_settings", vertex)
    return opened


def _settings() -> Settings:
    return Settings(_env_file=None)  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg


async def _law_retrieved(pipeline: AssessmentPipeline, tenant_id: str) -> set[str]:
    trace = await pipeline.retrieval.retrieve(
        LAW_QUESTION.query, tenant_id, LAW_QUESTION.as_of, law=LIBRARY_QUESTION
    )
    return {
        c.clause_id
        for c in trace.ranked_candidates()
        if c.clause_id.startswith(f"{LAW_INSTRUMENT_ID}:")
    }


async def test_the_live_store_filters_by_the_ledger_the_verifier_uses(offline: _Offline) -> None:
    """The store's subscription filter reads the pipeline's own ledger — the
    one `answer()` and the verifier are built with — so subscribing a tenant
    there is what the store sees. Unsubscribed, Acme retrieves no law; once
    subscribed to the Act, it does."""
    async with live.live_answer_pipeline(_settings()) as pipeline:
        assert offline.store.ledger is pipeline.ledger
        assert offline.verifier_ledgers == [pipeline.ledger]
        assert await _law_retrieved(pipeline, PUBLIC_TENANT_ID)  # the positive control
        assert await _law_retrieved(pipeline, POLICY_TENANT_ID) == set()

        await pipeline.ledger.subscribe(
            tenant_id=POLICY_TENANT_ID, instrument_id=LAW_INSTRUMENT_ID, subscribed_by="test"
        )

        assert await _law_retrieved(pipeline, POLICY_TENANT_ID)
    # Golden mode embeds from the snapshot alone: no live client is built.
    assert offline.live == []


async def test_golden_mode_subscribes_acme_to_no_law_since_no_acme_label_is_law(
    offline: _Offline,
) -> None:
    """Golden mode's subscriptions are what the signed golden set says Acme
    holds: its six policies and no law (queries.yaml's header), and no Acme
    query expects a law clause. If one ever does, golden mode must subscribe
    Acme to that instrument, and this fails until it does."""
    acme = [q for q in load_golden_set().queries if q.tenant_id == POLICY_TENANT_ID]
    assert acme
    assert all(c.startswith(f"{POLICY_TENANT_ID}/") for q in acme for c in q.expected_clause_ids)
    async with live.live_answer_pipeline(_settings()) as pipeline:
        assert await pipeline.ledger.subscribed_instruments(POLICY_TENANT_ID) == frozenset()
        assert offline.store.ledger is pipeline.ledger


async def test_environment_mode_applies_each_cases_subscriptions(offline: _Offline) -> None:
    """GE-01 as the live run met it: Acme subscribed to the Act, over the
    whole compilation. Before #333 nothing of the Act reached the model, so
    its `delivered` check failed live; now the Act's law does. Which clause
    ranks first is the real channels' business, not the fake's token overlap,
    so the clause GE-01 labels is the live run's to check. The tenant holds
    exactly what the case subscribes, whatever an earlier case left."""
    cases = load_environment_cases()
    assert cases is not None
    ge01 = next(c for c in cases.cases if c.id == "GE-01")
    async with live.live_answer_pipeline(_settings(), whole_compilation=True) as pipeline:
        await pipeline.ledger.subscribe(
            tenant_id=ge01.tenant_id, instrument_id="spam-act-2003", subscribed_by="an earlier case"
        )
        row = await environment_row(pipeline, ge01)
        assert await pipeline.ledger.subscribed_instruments(ge01.tenant_id) == frozenset(
            ge01.subscribes
        )
    assert ge01.subscribes == (LAW_INSTRUMENT_ID,)
    assert any(c.startswith(f"{LAW_INSTRUMENT_ID}:") for c in row["delivered"]), row["delivered"]
    # The question isn't in the snapshot: it, and only what the snapshot
    # lacks, went to the live embedder.
    (embedder,) = offline.live
    assert ge01.query in embedder.asked
    assert not EmbeddingCache.load().missing(tuple(c.text for c in golden_chunks()))


async def test_the_injection_probe_is_embedded_live(offline: _Offline) -> None:
    """`--injection` builds: the poisoned chunk the snapshot can't hold is
    embedded by the live fallback, the only text that is, and an injection
    question retrieves it."""
    async with live.live_answer_pipeline(
        _settings(), extra_chunks=(POISON_CHUNK,), extra_clauses=(POISON_CLAUSE,)
    ) as pipeline:
        (embedder,) = offline.live
        assert embedder.asked == [POISON_CHUNK.text]
        trace = await pipeline.retrieval.retrieve(
            INJECTION_QUESTIONS[0], POLICY_TENANT_ID, dt.date(2026, 10, 1), law=LIBRARY_QUESTION
        )
        assert POISON_CLAUSE.clause_id in {c.clause_id for c in trace.ranked_candidates()}
    assert POISON_CHUNK.synthetic and POISON_CHUNK.clause_id == POISON_CLAUSE.clause_id


async def test_the_gate_store_reads_an_empty_ledger_of_its_own(offline: _Offline) -> None:
    """The recall gates call `golden_store` with no ledger and no embedder:
    unchanged by #333, they get a fresh empty ledger and the snapshot alone."""
    async with harness.golden_store("http://unused", prefix="gate", chunks=golden_chunks()):
        pass
    ledger = offline.store.ledger
    assert isinstance(ledger, FakeLedgerStore)
    assert await ledger.subscribed_instruments(POLICY_TENANT_ID) == frozenset()
    assert offline.live == []
