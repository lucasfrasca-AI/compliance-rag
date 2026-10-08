"""`answer()`: retrieve, generate, verify — composed, not reimplemented (issue #16).

The generator never authors a citation (D-02, CLAUDE.md), so this tracer
bullet verifies the top reranked candidate's own chunk text against its
clause's canonical text — a real, deterministic check this pipeline can make
today, rather than inventing a stand-in for the structured-citation
extraction a future ticket adds once the generator's output is schema-
validated. `generation` is still produced and returned in full: `answer()`
proves the shape of the whole loop, not only the part it can verify yet.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from compliance_rag.adapters.interfaces import CitationVerifier, ClauseStore, Generation, Generator
from compliance_rag.assessment.draft import DraftAnswer, MalformedGeneration, parse_draft
from compliance_rag.assessment.prompt import (
    SYSTEM_PROMPT,
    SourceClause,
    policy_prefix,
    user_prompt,
)
from compliance_rag.retrieval.pipeline import RetrievalPipeline, RetrievalTrace
from compliance_rag.telemetry import span


@dataclass(frozen=True, slots=True)
class Answer:
    """`retrieve()` and `answer()` end to end, on one hand-written clause
    (issue #16)."""

    retrieval: RetrievalTrace
    generation: Generation
    draft: DraftAnswer
    """The generation, schema-validated (#29). Its citations are claims the
    verifier checks, not text shown to anyone."""
    verified: bool


class AssessmentPipeline:
    """Composes one `RetrievalPipeline`, one `Generator`, one `ClauseStore`
    and one `CitationVerifier` (D-08)."""

    def __init__(
        self,
        retrieval: RetrievalPipeline,
        generator: Generator,
        clause_store: ClauseStore,
        citation_verifier: CitationVerifier,
    ) -> None:
        self.retrieval = retrieval
        self.generator = generator
        self.clause_store = clause_store
        self.citation_verifier = citation_verifier

    async def answer(
        self,
        query: str,
        tenant_id: str,
        as_of: dt.date,
        *,
        policies: Sequence[SourceClause] = (),
        max_tokens: int = 1024,
    ) -> Answer:
        """`policies` is the tenant's policy corpus, sent as the cached prefix
        (D-11). The retriever's query is `query` and only `query`: it is fixed
        here, before any text is retrieved, and nothing retrieved or generated
        flows back into it (#29's injection control)."""
        with span("answer", query=query, tenant_id=tenant_id, as_of=as_of.isoformat()) as current:
            trace = await self.retrieval.retrieve(query, tenant_id, as_of)

            retrieved = [SourceClause(c.clause_id, c.text) for c in trace.ranked_candidates()]
            generation = await self.generator.generate(
                system=SYSTEM_PROMPT,
                cached_prefix=policy_prefix(policies),
                user=user_prompt(query, retrieved),
                max_tokens=max_tokens,
            )
            current.set_attribute("generation.input_tokens", generation.input_tokens)
            current.set_attribute("generation.output_tokens", generation.output_tokens)
            current.set_attribute("generation.cached_prefix_hit", generation.cached_prefix_hit)
            current.set_attribute(
                "generation.cache_read_input_tokens", generation.cache_read_input_tokens
            )
            current.set_attribute("generation.cost_usd", generation.cost_usd)
            try:
                draft = parse_draft(generation.text)
            except MalformedGeneration:
                current.set_attribute("generation.schema_valid", False)
                raise
            current.set_attribute("generation.schema_valid", True)

            verified = await self._verify_top_candidate(trace, tenant_id)
            current.set_attribute("verifier.result", verified)

            return Answer(retrieval=trace, generation=generation, draft=draft, verified=verified)

    async def _verify_top_candidate(self, trace: RetrievalTrace, tenant_id: str) -> bool:
        top = trace.top_candidate()
        if top is None:
            return False
        clause = await self.clause_store.get_clause(top.clause_id, tenant_id=tenant_id)
        if clause is None:
            return False
        return await self.citation_verifier.verify(
            top.clause_id,
            top.text,
            clauses=(clause,),
            canonicalisation_version=clause.canonicalisation_version,
        )
