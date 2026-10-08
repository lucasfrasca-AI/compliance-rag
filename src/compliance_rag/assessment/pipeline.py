"""`answer()`: retrieve, generate, verify — composed, not reimplemented
(issues #16, #29, #30).

The generator never authors a citation (D-02). It returns a schema-validated
draft whose citations are claims — a clause id and a quote — and every claim
is checked by the verifier (`verifier/citations.py`) before anything reaches a
user. What a user is shown for a citation is the clause's canonical text from
`ClauseStore`, never the model's words. A quote that didn't match is repaired
on its own — one targeted regeneration of that citation, never the whole
answer — and anything still failing is blocked.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from compliance_rag.adapters.interfaces import Generation, Generator
from compliance_rag.assessment.draft import (
    DraftAnswer,
    MalformedGeneration,
    parse_draft,
    parse_repair,
)
from compliance_rag.assessment.prompt import (
    REPAIR_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    SourceClause,
    policy_prefix,
    repair_prompt,
    user_prompt,
)
from compliance_rag.retrieval.pipeline import RetrievalPipeline, RetrievalTrace
from compliance_rag.telemetry import span
from compliance_rag.verifier.citations import CheckedCitation, Verifier

REPAIR_MAX_TOKENS = 512


@dataclass(frozen=True, slots=True)
class Answer:
    retrieval: RetrievalTrace
    generation: Generation
    repairs: tuple[Generation, ...]
    """One per repaired citation — cost per query sums these with `generation`."""
    draft: DraftAnswer
    """The model's schema-validated draft. Its citations are claims; only
    `citations` below have been verified."""
    citations: tuple[CheckedCitation, ...]
    """Verified citations, each carrying the canonical clause to show."""
    blocked: tuple[CheckedCitation, ...]
    """Citations that failed verification (after any repair) — never shown."""

    @property
    def verified(self) -> bool:
        """At least one citation, and every citation verified."""
        return bool(self.citations) and not self.blocked

    @property
    def cost_usd(self) -> float:
        return self.generation.cost_usd + sum(r.cost_usd for r in self.repairs)


class AssessmentPipeline:
    """Composes one `RetrievalPipeline`, one `Generator` and one `Verifier`
    (D-08)."""

    def __init__(
        self, retrieval: RetrievalPipeline, generator: Generator, verifier: Verifier
    ) -> None:
        self.retrieval = retrieval
        self.generator = generator
        self.verifier = verifier

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
        flows back into it (#29's injection control) — repair included, which
        re-reads one clause and never retrieves. The system prompt is fixed
        (`assessment/prompt.SYSTEM_PROMPT`), not a parameter: it carries the
        rule that fenced text is data, which no caller gets to drop."""
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
            try:
                draft = parse_draft(generation.text)
            except MalformedGeneration:
                current.set_attribute("generation.schema_valid", False)
                current.set_attribute("verifier.result", False)
                raise
            current.set_attribute("generation.schema_valid", True)

            # The context is what this request showed the model: the retrieved
            # set plus the cached policy set. Nothing else is citable.
            context = frozenset(c.clause_id for c in retrieved) | frozenset(
                p.clause_id for p in policies
            )
            checked = await self.verifier.check_all(draft.citations, context=context, as_of=as_of)
            repairs: list[Generation] = []
            final: list[CheckedCitation] = []
            for check in checked:
                if check.outcome.repairable and check.clause is not None:
                    check, repair = await self._repair(check, context=context, as_of=as_of)
                    repairs.append(repair)
                final.append(check)

            result = Answer(
                retrieval=trace,
                generation=generation,
                repairs=tuple(repairs),
                draft=draft,
                citations=tuple(c for c in final if c.verified),
                blocked=tuple(c for c in final if not c.verified),
            )
            current.set_attribute("verifier.result", result.verified)
            current.set_attribute(
                "verifier.outcomes", [f"{c.citation.clause_id}:{c.outcome.value}" for c in final]
            )
            current.set_attribute("verifier.repairs", len(repairs))
            current.set_attribute("generation.cost_usd", result.cost_usd)
            return result

    async def _repair(
        self, failed: CheckedCitation, *, context: frozenset[str], as_of: dt.date
    ) -> tuple[CheckedCitation, Generation]:
        """One targeted regeneration of one citation, re-verified. A repair
        that changes the clause id, or isn't valid JSON, leaves the original
        failure standing."""
        if failed.clause is None:
            raise ValueError("only a citation whose clause was read can be repaired")
        clause = SourceClause(failed.clause.clause_id, failed.clause.canonical_text)
        generation = await self.generator.generate(
            system=REPAIR_SYSTEM_PROMPT,
            cached_prefix=None,
            user=repair_prompt(clause, failed.citation.quote),
            max_tokens=REPAIR_MAX_TOKENS,
        )
        try:
            repaired = parse_repair(generation.text)
        except MalformedGeneration:
            return failed, generation
        if repaired.clause_id != failed.citation.clause_id:
            return failed, generation
        return await self.verifier.check(repaired, context=context, as_of=as_of), generation
