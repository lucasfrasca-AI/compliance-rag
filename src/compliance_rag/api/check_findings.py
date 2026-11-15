"""Findings for a check (#299, ADR-0053 F1-F6, ADR-0059 Q4 and Q6):
`POST /v1/checks/{check_id}/findings`, streamed as server-sent events as
`/v1/checks/{id}/claims` is, and `GET` of what it recorded.

**Where it runs, and why.** Findings are a run endpoint of their own, after the
claims (#298), not a longer `/claims`. Extraction records the claims a member can
already read, costs one call and is complete without findings; findings cost one
call per claim and need claims to exist, so each is its own reservation, and a
member (or #301's confirmation) can see the claims before the findings are
made. The one-run-per-check pattern is #348's: **reserved atomically before any
model is called** (`LedgerStore.reserve_findings`), so of any number of
concurrent requests exactly one holds it and the rest are a 409 that calls
nothing, and released as failed by every path that ends without recording, so
the run can be made again.

The stream carries stages and a result, never anything unvalidated:
- `coverage`: Track A on the date the claims' picks were made on, over the
  tenant's facts now (so an answer given to the questionnaire since the claims
  were extracted is read: ADR-0034, the earlier assessment is unchanged);
- `findings`: what was recorded, as `GET` shows it;
- or `error`.

There is no `retrieval` stage: a check never calls `RetrievalStore.search`,
`route` or `retrieve`, and `fetch` only with ids Track A named.

**Gates, in order, each before the next costs anything.** The sign-in; the
check, which is not found if it is not theirs (`checks.visible_check`,
ADR-0059 Q5); the claims, which must exist (409 `not_extracted`); the graph's
loads, which must hold a finished corpus and mapping version for the assessment
to name (503, as `POST /v1/coverage`'s); the reservation (409); then, in the
stream, offshore, only a registered build-phase tester may send a document
(`assert_submission_allowed`, ADR-0032), and the synthetic-text gates run in
`FindingsAssessor.run` before a prompt is built.

**What is shown of a finding.** A verdict only if every citation verified
(ADR-0003). A withheld finding shows our word for why and the clauses whose
citations blocked it, never a verdict, a quote or the model's prose; a failed
finding shows our word for why; a conditional one shows Track A's reason and its
questions, which the questionnaire answers (#304). An `ambiguous` verdict carries
`settle_by`, the model's own words, labelled as such. Every finding is
`inferred` (F5, ADR-0059 C1). `policy_disagreements` is the banner's data (Q6),
derived from the recorded findings.

An error event names our word for what went wrong, never the model's or the
provider's: `residency`, `generation_unavailable`, `policy_corpus_too_large`,
`graph_changed` or `internal`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from compliance_rag.adapters.generation_residency import (
    GenerationResidencyError,
    assert_submission_allowed,
)
from compliance_rag.adapters.interfaces import (
    CheckNotExtracted,
    FindingOutcome,
    FindingRecord,
    FindingsAlreadyReserved,
    FindingsRecord,
    GenerationUnavailable,
    LedgerStore,
)
from compliance_rag.api.answers import citation_json
from compliance_rag.api.checks import NOT_FOUND, read_passages, visible_check
from compliance_rag.api.deps import (
    ApiServices,
    CurrentCaller,
    counts,
    coverage_on,
    outcome_counts,
    services_of,
)
from compliance_rag.api.sources import source_href
from compliance_rag.api.tenant import held_sources, version_json
from compliance_rag.assessment.coverage import CoverageRow, question_json
from compliance_rag.assessment.findings import FindingsRun, policy_disagreements
from compliance_rag.assessment.policies import tenant_policies
from compliance_rag.assessment.prefix_cap import PolicyCorpusTooLarge
from compliance_rag.corpus.display_form import Titles, cite, cited_policy_versions
from compliance_rag.corpus.models import instrument_owner, parse_stable_id
from compliance_rag.telemetry import current_trace_id

router = APIRouter(prefix="/v1")
log = logging.getLogger(__name__)

NOT_EXTRACTED: Final = "this check's claims have not been extracted, so there is nothing to check"
ALREADY_FOUND: Final = "this check's findings were already made"
NOT_RUN: Final = "this check has no findings yet"

REASONS: Final = {
    "citation_blocked": "a citation didn't match its clause, so the finding is withheld (ADR-0003)",
    "uncited": "the finding cited no clause, so nothing in it could be verified",
    "malformed_generation": "the model's reply wasn't a valid set of findings; nothing in it "
    "is used",
    "no_finding_returned": "the model returned no finding for it",
    "refused_reply": "the model's finding for it was refused by a check and nothing in it is used",
    "generation_unavailable": "the model wasn't taking requests, so nothing was generated for it",
    "clause_unavailable": "a clause it rests on, or one it owes, couldn't be delivered, so the "
    "model was not asked",
    "boundary_collision": "the claim contains the text that fences it, so it was not sent to the "
    "model",
}
"""Why a withheld or failed finding is, in the words a member is shown."""

_running: set[asyncio.Task[FindingsRun]] = set()
"""Runs still going for a client that left: kept until they finish, so the tokens
they spend are recorded, and so are the findings."""


def event(name: str, data: dict[str, object]) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _error(failed: Exception) -> tuple[dict[str, object], bool]:
    """What an `error` event carries, and whether the operator should see it in the
    log (an unexpected one)."""
    if isinstance(failed, GenerationResidencyError):
        return {"error": "residency", "message": str(failed)}, False
    if isinstance(failed, GenerationUnavailable):
        return {
            "error": "generation_unavailable",
            "message": "the model isn't taking requests, so nothing was generated",
            "reason": failed.reason.value,
            "trace_id": current_trace_id(),
        }, False
    if isinstance(failed, PolicyCorpusTooLarge):
        return {
            "error": "policy_corpus_too_large",
            "message": str(failed),
            "estimated_tokens": failed.estimated_tokens,
            "cap_tokens": failed.cap_tokens,
            "trace_id": current_trace_id(),
        }, False
    if isinstance(failed, GraphChanged):
        return {"error": "graph_changed", "message": str(failed)}, False
    return {"error": "internal", "message": "the findings couldn't be made"}, True


class GraphChanged(RuntimeError):
    """A graph load began or ended while the coverage was computed, so it may mix
    two versions and the assessment could not honestly name one."""


async def _canonical_texts(services: ApiServices, clause_ids: set[str]) -> dict[str, str]:
    """The stored canonical text of each cited clause, read once each: what a reader
    is shown for a citation (D-02), never the model's quote. A clause the store no
    longer holds (purged) is left out."""

    async def read(clause_id: str) -> tuple[str, str | None]:
        owner = instrument_owner(parse_stable_id(clause_id)[0])
        clause = await services.clauses.get_clause(clause_id, tenant_id=owner)
        return clause_id, None if clause is None else clause.canonical_text

    return {c: t for c, t in await asyncio.gather(*(read(i) for i in sorted(clause_ids))) if t}


def _target_json(finding: FindingRecord, titles: Titles) -> dict[str, object]:
    if finding.obligation_id is not None:
        return {"kind": "law", "obligation_id": finding.obligation_id}
    assert finding.policy_clause_id is not None  # a finding is against one or the other
    return {
        "kind": "policy",
        "policy_clause_id": finding.policy_clause_id,
        "cite": cite(finding.policy_clause_id, titles),
        "href": source_href(finding.policy_clause_id),
    }


def _finding_json(
    finding: FindingRecord,
    rows: dict[str, CoverageRow],
    titles: Titles,
    texts: dict[str, str],
) -> dict[str, object]:
    """One finding as a member is shown it. A verdict only for a `verdict` row:
    a withheld or failed finding stored none, so there is none to show."""
    shown: dict[str, object] = {
        "finding_id": finding.finding_id,
        "claim_index": finding.claim_index,
        **_target_json(finding, titles),
        "outcome": finding.outcome.value,
        "verdict": None if finding.verdict is None else finding.verdict.value,
        "settle_by": finding.settle_by,
        "provenance": finding.provenance.value,
        "derived_from": finding.derived_from,
        "citations": [
            {
                **citation_json(c.clause_id, c.version_id, c.incomplete, titles),
                "quote": c.quoted_text,
                "canonical_text": texts.get(c.clause_id),
            }
            for c in finding.citations
        ],
        "withheld": None,
        "failed": None,
        "blocked": [],
        "conditional": None,
    }
    if finding.detail is not None:
        shown["withheld" if finding.outcome is FindingOutcome.WITHHELD else "failed"] = REASONS[
            finding.detail.value
        ]
        shown["blocked"] = [
            {"clause_id": b.clause_id, "cite": cite(b.clause_id, titles), "outcome": b.outcome}
            for b in finding.blocked
        ]
    if finding.conditional:
        assert finding.obligation_id is not None  # a conditional finding is against an obligation
        row = rows[finding.obligation_id]
        shown["conditional"] = {
            "reason": finding.unassessable_reason,
            "questions": [question_json(q) for q in row.questions],
        }
    return shown


async def findings_json(
    services: ApiServices, tenant_id: str, check_id: str, record: FindingsRecord
) -> dict[str, object]:
    """A recorded findings run as a member is shown it: one builder for the
    stream's `findings` event and for `GET`, so the two cannot drift."""
    assessment = record.assessment
    rows = {r.obligation_id: r for r in record.coverage}
    cited = {c.clause_id for f in record.findings for c in f.citations}
    named = {
        *cited,
        *(f.policy_clause_id for f in record.findings if f.policy_clause_id is not None),
        *(b.clause_id for f in record.findings for b in f.blocked),
    }
    titles = await services.ledger.version_titles(tenant_id, cited_policy_versions(named))
    texts = await _canonical_texts(services, cited)
    tally = {o.value: sum(f.outcome is o for f in record.findings) for o in FindingOutcome}
    return {
        "check_id": check_id,
        "assessment_id": assessment.assessment_id,
        "as_of": assessment.as_of.isoformat(),
        "trace_id": assessment.trace_id,
        "corpus_version": version_json(assessment.corpus),
        "mapping_version": version_json(assessment.mapping),
        "provenance": "inferred",
        "coverage_counts": outcome_counts(r.outcome for r in record.coverage),
        "counts": tally,
        "findings": [_finding_json(f, rows, titles, texts) for f in record.findings],
        "policy_disagreements": [
            {
                "claim_index": d.claim_index,
                "conflicts": {
                    "policy_clause_id": d.conflicts,
                    "cite": cite(d.conflicts, titles),
                    "href": source_href(d.conflicts),
                },
                "with": {
                    "policy_clause_id": d.other,
                    "cite": cite(d.other, titles),
                    "href": source_href(d.other),
                    "verdict": d.other_verdict.value,
                },
            }
            for d in policy_disagreements(record.findings)
        ],
    }


@router.post("/checks/{check_id}/findings")
async def run_findings(request: Request, who: CurrentCaller, check_id: str) -> StreamingResponse:
    services = services_of(request)
    # Before anything is read, sent or written: a check that isn't theirs is not
    # found, whoever's it is (ADR-0059 Q5).
    check = await visible_check(services, who, check_id)
    if check is None:
        raise HTTPException(404, NOT_FOUND)
    extraction = await services.ledger.get_claims(who.tenant_id, check_id)
    if extraction is None:
        raise HTTPException(409, {"error": "not_extracted", "message": NOT_EXTRACTED})
    # The graph's loads, before the reservation, so a refusal that costs nothing
    # leaves the run reservable: an assessment must name what it was computed from.
    before = await services.graph.loaded_sources()
    corpus, mapping = held_sources(before)
    try:
        await services.ledger.reserve_findings(who.tenant_id, check_id, trace_id=current_trace_id())
    except CheckNotExtracted:
        raise HTTPException(409, {"error": "not_extracted", "message": NOT_EXTRACTED}) from None
    except FindingsAlreadyReserved:
        raise HTTPException(409, {"error": "already_found", "message": ALREADY_FOUND}) from None

    async def stages() -> AsyncIterator[str]:
        running: asyncio.Task[FindingsRun] | None = None
        try:
            # First: offshore, only a registered tester's document leaves Australia
            # (ADR-0032), as for a question and for the claims.
            assert_submission_allowed(
                generation_region=services.settings.generation_region,
                submitter=who.member.email,
                testers=services.settings.offshore_testers,
            )
            # On the date the picks were made, so the digest the claims chose from
            # is the digest the findings are held to; over the facts as they are now.
            coverage = await coverage_on(services, who.tenant_id, extraction.as_of)
            if await services.graph.loaded_sources() != before:
                raise GraphChanged(
                    "a load started or finished while the coverage was computed, so it may mix "
                    "two versions: nothing was recorded. Try again"
                )
            yield event(
                "coverage",
                {
                    "check_id": check_id,
                    "as_of": extraction.as_of.isoformat(),
                    "counts": counts(coverage),
                },
            )
            policies = await tenant_policies(
                services.ledger, services.clauses, tenant_id=who.tenant_id, as_of=extraction.as_of
            )
            passages = await read_passages(services.clauses, check)
            source = await services.clauses.get_source_html(
                check.instrument_id, check.version_id, tenant_id=check.tenant_id
            )
            if source is None:
                raise LookupError(f"{check.instrument_id}:{check.version_id}: no stored source")
            assessor = services.findings_assessor()

            async def run() -> FindingsRun:
                """Makes and records the findings, and releases the reservation if it
                does not get as far as recording, whatever stops it."""
                try:
                    return await assessor.run(
                        tenant_id=who.tenant_id,
                        check_id=check_id,
                        asker=who.member.email,
                        member_uid=who.member.uid,
                        extraction=extraction,
                        passages=passages,
                        source=source,
                        coverage=coverage,
                        policies=policies,
                        corpus=corpus,
                        mapping=mapping,
                    )
                except BaseException:
                    await _release(services.ledger, who.tenant_id, check_id)
                    raise

            # A child task carries a copy of this request's context, so its spans and
            # rows carry the request's trace_id, and it isn't cancelled if the client
            # leaves: the tokens it spends and the findings it makes are recorded
            # either way.
            running = asyncio.ensure_future(run())
            _running.add(running)
            running.add_done_callback(_finished)
            made = await asyncio.shield(running)
            stored = await services.ledger.get_findings(who.tenant_id, check_id)
            if stored is None:  # recorded by `run`, so this is the ledger lying
                raise LookupError(f"check {check_id}: the findings were not recorded")
            yield event(
                "findings",
                {
                    **await findings_json(services, who.tenant_id, check_id, stored),
                    "refused": [
                        {
                            "claim_index": r.claim_index,
                            "position": r.position,
                            "reason": r.reason.value,
                        }
                        for r in made.refused
                    ],
                    "skipped": list(made.skipped),
                    "calls": len(made.generations),
                    "generator": services.settings.generator,
                    "cost_usd": made.cost_usd,
                },
            )
        except Exception as failed:
            if running is None:  # nothing was handed the reservation: release it here
                await _release(services.ledger, who.tenant_id, check_id)
            body, unexpected = _error(failed)
            if unexpected:
                log.exception("making a check's findings failed")
            else:
                log.warning("a check's findings were not made: %s", body["error"])
            yield event("error", {"check_id": check_id, **body})
        except BaseException:  # the client left before the model was called
            if running is None:
                await _release(services.ledger, who.tenant_id, check_id)
            raise

    return StreamingResponse(stages(), media_type="text/event-stream")


async def _release(ledger: LedgerStore, tenant_id: str, check_id: str) -> None:
    """Release a reservation as failed. A ledger that cannot take it does not hide
    the error being reported: the reservation then lapses after
    `FINDINGS_RESERVATION_TTL`."""
    try:
        await ledger.fail_findings(tenant_id, check_id)
    except Exception:
        log.exception("a check's findings reservation could not be released")


def _finished(task: asyncio.Task[FindingsRun]) -> None:
    _running.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.warning(
            "a findings run finished with an error after its client left",
            exc_info=task.exception(),
        )


@router.get("/checks/{check_id}/findings")
async def get_findings(request: Request, who: CurrentCaller, check_id: str) -> dict[str, object]:
    """What was recorded for a check the caller may see: 404 for one that isn't
    theirs or doesn't exist (the one answer for both), and for one whose findings
    have not been made."""
    services = services_of(request)
    check = await visible_check(services, who, check_id)
    if check is None:
        raise HTTPException(404, NOT_FOUND)
    record = await services.ledger.get_findings(who.tenant_id, check_id)
    if record is None:
        raise HTTPException(404, NOT_RUN)
    return await findings_json(services, who.tenant_id, check_id, record)
