"""The live answer pipeline (#31): `answer()` with live Gemini.

Kept apart from `eval/harness.py` on purpose: the recall gates import the
harness, and they must never load an LLM SDK
(`test_the_gate_cannot_reach_a_language_model`). Only the live eval and the
demo import this module.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.generator_factory import build_generator
from compliance_rag.adapters.interfaces import Embedder
from compliance_rag.adapters.vertex_embedder import VertexEmbedder
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.corpus.models import Chunk, ClauseRecord
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.eval.embedding_cache import EmbeddingCache, text_key
from compliance_rag.eval.golden_corpus import gate_chunks, golden_chunks
from compliance_rag.eval.harness import (
    golden_clause_store,
    golden_ledger,
    golden_store,
    in_process_reranker,
)
from compliance_rag.reranker_service.model import RerankerModel
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import Settings
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier


class LiveFallbackEmbedder(Embedder):
    """The checked-in snapshot's vectors, and the live model's for a text the
    snapshot lacks: a question of the out-of-environment cases (#252) is not
    in the snapshot the recall gate reads. Only a live run builds one."""

    def __init__(self, cache: EmbeddingCache, live: Embedder) -> None:
        self._cache = cache
        self._live = live

    @property
    def dimensions(self) -> int:
        return self._cache.dimensions

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        missing = tuple(dict.fromkeys(self._cache.missing(texts)))
        fresh = dict(zip(missing, await self._live.embed(missing), strict=True)) if missing else {}
        return tuple(fresh.get(t) or self._cache.vectors[text_key(t)] for t in texts)


@asynccontextmanager
async def live_answer_pipeline(
    settings: Settings,
    *,
    extra_chunks: tuple[Chunk, ...] = (),
    extra_clauses: tuple[ClauseRecord, ...] = (),
    whole_compilation: bool = False,
) -> AsyncGenerator[AssessmentPipeline]:
    """`answer()` on the golden corpus with every real stage: Qdrant, cached
    real embeddings, the int8 cross-encoder, **live Gemini 3.5 Flash**
    (built only through `build_generator`, so the residency gate runs first —
    ADR-0023, ADR-0057), and the real verifier against golden canonical text
    and a ledger with every golden version recorded. Shared by the live eval
    and the demo.

    `whole_compilation` puts it in place of the excerpt, in Qdrant and
    in the clause store alike, so the verifier can verify whatever is
    retrieved; and embeds a question the snapshot lacks with the live model.
    The out-of-environment cases need it: APP 7 isn't in the excerpt.

    One ledger for every stage (#333): the store's subscription filter reads
    the ledger `answer()` and the verifier use, so a tenant subscribed there
    retrieves the law it subscribes to. It holds no subscription to start
    with: the golden set's Acme holds its own policies and no law, and no Acme
    golden query expects a law clause (`corpus/golden/queries.yaml`). The
    environment mode subscribes each case's tenant as the case says.

    `extra_chunks` (the injection probe) are embedded live too: the snapshot
    can't hold a chunk invented for one run."""
    clauses = await golden_clause_store(whole_compilation=whole_compilation)
    for clause in extra_clauses:
        await clauses.put_clause(clause)
    ledger = await golden_ledger()
    embedder = (
        LiveFallbackEmbedder(EmbeddingCache.load(), VertexEmbedder.from_settings(settings))
        if whole_compilation or extra_chunks
        else None
    )
    async with (
        # The excerpt, not the gate's whole compilation, unless asked: the
        # verifier holds the same canonical text, so it can verify whatever
        # this retrieves.
        golden_store(
            settings.qdrant_url or "http://localhost:6333",
            prefix="answer",
            chunks=gate_chunks() if whole_compilation else golden_chunks(),
            embedder=embedder,
            ledger=ledger,
        ) as store,
        in_process_reranker(RerankerModel()) as reranker,
    ):
        if extra_chunks:
            await store.upsert_chunks(extra_chunks)
        generator = await build_generator(settings, corpus=store)
        # The injection probe is invented by this operator script, so it is
        # registered here, in memory, for this run only (ADR-0026's operator
        # path); every other tenant text must already be registered.
        registry = SyntheticRegistry.load()
        probe = SyntheticRegistry.of(extra_chunks, extra_clauses, source="the injection probe")
        yield AssessmentPipeline(
            RetrievalPipeline(store, reranker),
            generator,
            Verifier(clauses, ledger, CanonicalCitationVerifier()),
            ledger,
            tenant_text=TenantTextGate(
                settings.generation_region,
                SyntheticRegistry(registry.keys | probe.keys, f"{registry.source}; {probe.source}"),
            ),
        )
