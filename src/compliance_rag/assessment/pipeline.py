"""`answer()`: retrieve, generate, verify — composed, not reimplemented
(issues #16, #29, #30).

The generator never authors a citation (D-02). It returns a schema-validated
draft whose citations are claims — a clause id and a quote — and every claim
is checked by the verifier (`verifier/citations.py`) before anything reaches a
user. What a user is shown for a citation is the clause's canonical text from
`ClauseStore`, never the model's words. A quote that didn't match is repaired
on its own — one targeted regeneration of that citation, never the whole
answer — and anything still failing is blocked.

An obligation's clause never reaches the model without its exceptions (#164,
ADR-0037) or its qualifiers (#182, ADR-0044), nor without the clauses inside
each: whatever the ranking returned, every one Track A names for a clause in
the context is delivered with it, fetched by id if need be, and marked as
what it is. An exception says when the obligation doesn't apply; a qualifier
says how it is satisfied. The marker and the system prompt keep them apart.

Every clause in the fences carries the form a reader cites it by
(`display_form.cite`, ADR-0049), so the model names a clause as the member is
shown it. A policy's form names its document by the title the ledger records
with the version: read once per answer, for every policy version in the
context, never once per clause or per citation. The answer carries what was
read, so the API shows the same form.

An incomplete clause (ADR-0047) is marked so in its fence, and an answer that
cites one carries it (`Answer.incomplete`): the verifier still checks the
quote against the stored text, and the flag says that text isn't all the
clause holds.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace

from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.interfaces import (
    Generation,
    Generator,
    LawScope,
    LedgerStore,
    Owed,
    PolicyCorpus,
    UsageRecord,
)
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
from compliance_rag.corpus.display_form import NO_TITLES, Titles, cite, policy_versions
from compliance_rag.corpus.models import Incomplete, Scope
from compliance_rag.retrieval.pipeline import RetrievalPipeline, RetrievalTrace
from compliance_rag.telemetry import current_trace_id, span
from compliance_rag.verifier.citations import CheckedCitation, Verifier

REPAIR_MAX_TOKENS = 512


ANSWER_SCOPES = (Scope.PUBLIC, Scope.TENANT, Scope.USER)
"""What a member's question may be answered from: library law, the tenant's
documents, and the member's own private additions. User scope is listed
because the filter, not this list, keeps it private: the store admits a
user-scoped chunk to its owner alone, and to no one when no member asks
(ADR-0036)."""


LIBRARY_QUESTION = LawScope.unscoped(
    "a question about the library, asked with no tenant profile (Phase 1 answer(); "
    "an assessment passes Track A's scope, LawScope.of(coverage))"
)


def _kind(owed: Owed) -> str:
    """What an owed clause is, in the words of a refusal."""
    kinds = [k for k, v in (("an exception", owed.excepts), ("a qualifier", owed.qualifies)) if v]
    return " and ".join(kinds)


class OwedClauseUndelivered(RuntimeError):
    """An exception or qualifier Track A names for a clause in the context
    couldn't be fetched: the graph names a clause the retrieval store doesn't
    hold on `as_of`. Raised before generation, so no answer states the rule
    without it (ADR-0037, ADR-0044)."""


@dataclass(frozen=True, slots=True)
class IncompleteCitation:
    """A cited clause that is incomplete, and why (ADR-0047): what an answer
    citing it carries, so a reader can't take the clause for all of the law."""

    clause_id: str
    incomplete: Incomplete


@dataclass(frozen=True, slots=True)
class Answer:
    retrieval: RetrievalTrace
    generation: Generation
    repairs: tuple[Generation, ...]
    """One per repaired citation — cost per query sums these with `generation`."""
    unverified_draft: DraftAnswer
    """The model's schema-validated draft — **unverified**: its citations are
    claims, and its prose is not tied to them. Only `citations` below have
    been verified. Its prose is shown only when the whole answer is
    `verified` (ADR-0003: a bad citation blocks the answer), never beside a
    blocked citation it may quote."""
    citations: tuple[CheckedCitation, ...]
    """Verified citations, each carrying the canonical clause to show."""
    blocked: tuple[CheckedCitation, ...]
    """Citations that failed verification (after any repair) — never shown."""
    trace_id: str
    """The request's trace: on every ledger row it produced (#31)."""
    exceptions: tuple[SourceClause, ...] = ()
    """The exceptions the model was given with an obligation's clause, each
    naming the clauses it excepts — retrieved, or fetched by id where the
    ranking left them out (#164, ADR-0037). Citable like what was retrieved."""
    qualifiers: tuple[SourceClause, ...] = ()
    """The qualifiers the model was given with an obligation's clause, each
    naming the clauses it qualifies, delivered the same way (#182,
    ADR-0044). Kept apart from `exceptions`, so a client can tell "when it
    doesn't apply" from "how it is satisfied"."""
    titles: Titles = NO_TITLES
    """The recorded title of every policy version the model was given, read
    once for this answer (ADR-0049): what the API cites a policy clause by,
    so the member is shown the form the model was."""

    @property
    def incomplete(self) -> tuple[IncompleteCitation, ...]:
        """Each verified citation of an incomplete clause, with why (ADR-0047),
        read from the clause the verifier checked the quote against — the
        stored corpus, not retrieval's copy. The quote still matched; this
        says the clause it matched is not all of what the law sets there."""
        found: dict[str, IncompleteCitation] = {}
        for check in self.citations:
            clause = check.clause
            if clause is not None and clause.incomplete:
                found.setdefault(
                    clause.clause_id, IncompleteCitation(clause.clause_id, clause.incomplete)
                )
        return tuple(found.values())

    @property
    def verified(self) -> bool:
        """At least one citation, and every citation verified. An answer with
        no citations — "the clauses don't cover this" — is never verified:
        there is nothing to have checked."""
        return bool(self.citations) and not self.blocked

    @property
    def cost_usd(self) -> float:
        return self.generation.cost_usd + sum(r.cost_usd for r in self.repairs)


class AssessmentPipeline:
    """Composes one `RetrievalPipeline`, one `Generator` and one `Verifier`
    (D-08). `tenant_text` is required, like the generator's own store gate:
    no way of building the pipeline sends unregistered tenant text from the
    bucket offshore (ADR-0032)."""

    def __init__(
        self,
        retrieval: RetrievalPipeline,
        generator: Generator,
        verifier: Verifier,
        ledger: LedgerStore,
        *,
        tenant_text: TenantTextGate,
    ) -> None:
        self.retrieval = retrieval
        self.generator = generator
        self.verifier = verifier
        self.ledger = ledger
        self.tenant_text = tenant_text

    async def _record_usage(self, tenant_id: str, generation: Generation) -> None:
        """One ledger `usage` row per generation call — the answer and each
        repair — from real token counts, keyed to the request's trace."""
        await self.ledger.record_usage(
            UsageRecord(
                tenant_id=tenant_id,
                usage_id=uuid.uuid4().hex,
                trace_id=current_trace_id(),
                model=generation.model,
                input_tokens=generation.input_tokens,
                output_tokens=generation.output_tokens,
                cached_prefix_hit=generation.cached_prefix_hit,
                cost_usd=generation.cost_usd,
            )
        )

    async def answer(
        self,
        query: str,
        tenant_id: str,
        as_of: dt.date,
        *,
        law: LawScope,
        policies: Sequence[SourceClause] = (),
        max_tokens: int = 1024,
        on_retrieved: Callable[[RetrievalTrace], Awaitable[None]] | None = None,
        user_id: str | None = None,
    ) -> Answer:
        """`law` is Track B's scope (D-03, ADR-0030): `LawScope.of(coverage)`
        for an assessment, `LIBRARY_QUESTION` for a question about the library
        with no tenant profile — required, so no call skips Track A unseen.
        `policies` is the tenant's policy corpus, sent as the cached prefix
        (D-11) and never retrieved as well (ADR-0033). The retriever's query
        is `query` and only `query`: it is fixed here, before any text is
        retrieved, and nothing retrieved or generated
        flows back into it (#29's injection control) — repair included, which
        re-reads one clause and never retrieves. The system prompt is fixed
        (`assessment/prompt.SYSTEM_PROMPT`), not a parameter: it carries the
        rule that fenced text is data, which no caller gets to drop.
        `on_retrieved` is told what retrieval found before generation starts
        (the API's stream reports it as it happens, #149); it sees, never
        changes, what the model is given. `user_id` is the signed-in member
        asking (`Member.uid`): their own private additions are retrievable and
        citable by them, and no one else's are, by retrieval or by the
        verifier's read of canonical text (ADR-0036). Without one, user scope
        yields nothing. Then every exception and qualifier `law` names for a
        clause in it, and every clause inside one, is added, fetched by id if
        the ranking left it out (#164, #182, ADR-0037, ADR-0044): the ids come
        from Track A, never from retrieved text."""
        with span("answer", query=query, tenant_id=tenant_id, as_of=as_of.isoformat()) as current:
            # Before anything is retrieved or generated: offshore, the prefix
            # must be registered synthetic (ADR-0032).
            self.tenant_text.check(
                tenant_id,
                ((p.clause_id, p.text) for p in policies),
                what="clause(s) of the CAG prefix",
            )
            # The prefix is the policies' authoritative copy, so retrieval
            # leaves them out and their slots go to law (ADR-0033).
            sent = PolicyCorpus(frozenset(p.clause_id for p in policies))
            # Through the router (D-12, #159): a question that is a clause id
            # is an exact lookup that skips the reranker; anything else runs
            # the full retrieve().
            trace = await self.retrieval.route(
                query,
                tenant_id,
                as_of,
                law=law,
                scopes=ANSWER_SCOPES,
                policy_corpus=sent,
                user_id=user_id,
            )
            if on_retrieved is not None:
                await on_retrieved(trace)

            # The ranked clauses, each whole: with its chapeaux and its
            # paragraphs, never a fragment of the law (ADR-0045).
            # Each marked incomplete where it is (ADR-0047).
            retrieved = [
                SourceClause(c.clause_id, c.text, incomplete=c.incomplete)
                for c in trace.delivered()
            ]
            retrieved, fetched = await self._with_owed(
                retrieved, tenant_id, as_of, law=law, policy_corpus=sent
            )
            # One read for every policy version in the context, prefix and
            # retrieved alike (ADR-0049): each clause is then marked with the
            # form the member is shown for it.
            titles = await self.ledger.version_titles(
                tenant_id,
                policy_versions(
                    [*(p.clause_id for p in policies), *(c.clause_id for c in retrieved)]
                ),
            )
            policies = [replace(p, cited_as=cite(p.clause_id, titles)) for p in policies]
            retrieved = [replace(c, cited_as=cite(c.clause_id, titles)) for c in retrieved]
            current.set_attribute("answer.titles_read", len(titles))
            exceptions = tuple(c for c in retrieved if c.excepts)
            qualifiers = tuple(c for c in retrieved if c.qualifies)
            current.set_attribute(
                "answer.exceptions",
                [f"{c.clause_id} excepts {' '.join(c.excepts)}" for c in exceptions],
            )
            current.set_attribute(
                "answer.qualifiers",
                [f"{c.clause_id} qualifies {' '.join(c.qualifies)}" for c in qualifiers],
            )
            current.set_attribute(
                "answer.exceptions_fetched",
                [c.clause_id for c in exceptions if c.clause_id in fetched],
            )
            current.set_attribute(
                "answer.qualifiers_fetched",
                [c.clause_id for c in qualifiers if c.clause_id in fetched],
            )
            generation = await self.generator.generate(
                system=SYSTEM_PROMPT,
                cached_prefix=policy_prefix(policies),
                user=user_prompt(query, retrieved),
                max_tokens=max_tokens,
            )
            await self._record_usage(tenant_id, generation)
            current.set_attribute("generation.input_tokens", generation.input_tokens)
            current.set_attribute("generation.output_tokens", generation.output_tokens)
            current.set_attribute("generation.cached_prefix_hit", generation.cached_prefix_hit)
            current.set_attribute(
                "generation.cache_read_input_tokens", generation.cache_read_input_tokens
            )
            # verifier.result is on the span for every request: False unless
            # verification completes and passes, whatever raises on the way.
            current.set_attribute("verifier.result", False)
            try:
                draft = parse_draft(generation.text)
            except MalformedGeneration:
                current.set_attribute("generation.schema_valid", False)
                raise
            current.set_attribute("generation.schema_valid", True)

            # The context is what this request showed the model: the retrieved
            # set (with the exceptions and qualifiers delivered beside it)
            # plus the cached policy set. Nothing else is citable.
            context = frozenset(c.clause_id for c in retrieved) | sent.clause_ids
            checked = await self.verifier.check_all(
                draft.citations, context=context, as_of=as_of, user_id=user_id
            )
            repairs: list[Generation] = []
            final: list[CheckedCitation] = []
            for check in checked:
                if check.outcome.repairable and check.clause is not None:
                    check, repair = await self._repair(
                        check, context=context, as_of=as_of, user_id=user_id
                    )
                    await self._record_usage(tenant_id, repair)
                    repairs.append(repair)
                final.append(check)

            result = Answer(
                retrieval=trace,
                generation=generation,
                repairs=tuple(repairs),
                unverified_draft=draft,
                citations=tuple(c for c in final if c.verified),
                blocked=tuple(c for c in final if not c.verified),
                trace_id=current_trace_id(),
                exceptions=exceptions,
                qualifiers=qualifiers,
                titles=titles,
            )
            current.set_attribute("verifier.result", result.verified)
            current.set_attribute(
                "verifier.outcomes", [f"{c.citation.clause_id}:{c.outcome.value}" for c in final]
            )
            current.set_attribute("verifier.repairs", len(repairs))
            current.set_attribute(
                "answer.incomplete",
                [
                    f"{c.clause_id}: {'; '.join(i.reason.value for i in c.incomplete)}"
                    for c in result.incomplete
                ],
            )
            current.set_attribute("generation.cost_usd", result.cost_usd)
            return result

    async def _with_owed(
        self,
        retrieved: list[SourceClause],
        tenant_id: str,
        as_of: dt.date,
        *,
        law: LawScope,
        policy_corpus: PolicyCorpus,
    ) -> tuple[list[SourceClause], frozenset[str]]:
        """`retrieved` with every exception and qualifier `law` names for a
        clause in it, and every clause inside one (ADR-0037, ADR-0044):
        marked where the ranking returned it, fetched by id where it didn't,
        and again for anything a fetched clause is owed. Returns what the
        model is given and the ids that were fetched. One that can't be
        fetched refuses the answer before generation."""
        delivered = list(retrieved)
        fetched: list[str] = []
        while True:
            owed = law.owed(c.clause_id for c in delivered)
            held = {c.clause_id for c in delivered}
            missing = tuple(e for e in owed if e not in held)
            if not missing:
                break
            found = {
                c.clause_id: c
                for c in await self.retrieval.fetch(
                    missing, tenant_id, as_of, law=law, policy_corpus=policy_corpus
                )
            }
            lost = [e for e in missing if e not in found]
            if lost:
                named = ", ".join(f"{e} ({_kind(owed[e])})" for e in lost)
                raise OwedClauseUndelivered(
                    f"{named}: Track A names it for a clause in the context, but it isn't "
                    f"retrievable on {as_of}, so the answer is refused rather than state the "
                    "rule without it (ADR-0037, ADR-0044)"
                )
            delivered += [
                SourceClause(e, found[e].text, incomplete=found[e].incomplete) for e in missing
            ]
            fetched += missing
        marked = [
            replace(c, excepts=owed[c.clause_id].excepts, qualifies=owed[c.clause_id].qualifies)
            if c.clause_id in owed
            else c
            for c in delivered
        ]
        return marked, frozenset(fetched)

    async def _repair(
        self,
        failed: CheckedCitation,
        *,
        context: frozenset[str],
        as_of: dt.date,
        user_id: str | None,
    ) -> tuple[CheckedCitation, Generation]:
        """One targeted regeneration of one citation, re-verified. A repair
        that changes the clause id, or isn't valid JSON, leaves the original
        failure standing."""
        if failed.clause is None:
            raise ValueError("only a citation whose clause was read can be repaired")
        clause = SourceClause(
            failed.clause.clause_id,
            failed.clause.canonical_text,
            incomplete=failed.clause.incomplete,
        )
        if failed.clause.scope is not Scope.PUBLIC:
            # Read from the bucket, which the generator's store gate never
            # asks (ADR-0032).
            self.tenant_text.check(
                failed.clause.tenant_id, ((clause.clause_id, clause.text),), what="clause to repair"
            )
        with span("answer.repair", **{"repair.clause_id": clause.clause_id}) as current:
            generation = await self.generator.generate(
                system=REPAIR_SYSTEM_PROMPT,
                cached_prefix=None,
                user=repair_prompt(clause, failed.citation.quote),
                max_tokens=REPAIR_MAX_TOKENS,
            )
            current.set_attribute("generation.input_tokens", generation.input_tokens)
            current.set_attribute("generation.output_tokens", generation.output_tokens)
            current.set_attribute("generation.cost_usd", generation.cost_usd)
            outcome = failed
            try:
                repaired = parse_repair(generation.text)
            except MalformedGeneration:
                repaired = None
            if repaired is not None and repaired.clause_id == failed.citation.clause_id:
                outcome = await self.verifier.check(
                    repaired, context=context, as_of=as_of, user_id=user_id
                )
            current.set_attribute("repair.outcome", outcome.outcome.value)
            return outcome, generation
