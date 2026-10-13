"""The live answer pipeline (#31): `answer()` with live Claude.

Kept apart from `eval/harness.py` on purpose: the recall gates import the
harness, and they must never load an LLM SDK
(`test_the_gate_cannot_reach_a_language_model`). Only the live eval and the
demo import this module.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.vertex_claude_generator import VertexClaudeGenerator
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.corpus.models import Chunk, ClauseRecord
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
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


@asynccontextmanager
async def live_answer_pipeline(
    settings: Settings,
    *,
    extra_chunks: tuple[Chunk, ...] = (),
    extra_clauses: tuple[ClauseRecord, ...] = (),
) -> AsyncGenerator[AssessmentPipeline]:
    """`answer()` on the golden corpus with every real stage: Qdrant, cached
    real embeddings, the int8 cross-encoder, **live Claude** (built only
    through `VertexClaudeGenerator.create`, so the residency gate runs first —
    ADR-0023/0024), and the real verifier against golden canonical text and a
    ledger with every golden version recorded. Shared by the live eval and
    the demo."""
    clauses = await golden_clause_store()
    for clause in extra_clauses:
        await clauses.put_clause(clause)
    ledger = await golden_ledger()
    async with (
        golden_store(settings.qdrant_url or "http://localhost:6333", prefix="answer") as store,
        in_process_reranker(RerankerModel()) as reranker,
    ):
        if extra_chunks:
            await store.upsert_chunks(extra_chunks)
        generator = await VertexClaudeGenerator.create(settings, corpus=store)
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
                settings.vertex_claude_region,
                SyntheticRegistry(registry.keys | probe.keys, f"{registry.source}; {probe.source}"),
            ),
        )
