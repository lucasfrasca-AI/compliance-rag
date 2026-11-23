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
  tenant's facts as the ledger holds them now (so an answer given to the
  questionnaire since the claims were extracted is read), and **the facts it read
  are recorded with the assessment** (ADR-0034): every evidence row with its value,
  and their hash, so a stored assessment shows exactly what it was decided on. That
  records what was read; reading facts as of a past date waits for #123. The
  earlier assessment of another check is unchanged;
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
`settle_by` as `{"text": ..., "source": "model"}`: the model's own words, labelled as
such. A finding the model returned and a check refused is reported as `malformed_finding`
and nothing more (which check, and so which verdict class, is on the span). Every finding is
`inferred` (F5, ADR-0059 C1). `policy_disagreements` is the banner's data (Q6),
derived from the recorded findings.

An error event names our word for what went wrong, never the model's or the
provider's: `residency`, `generation_unavailable`, `policy_corpus_too_large`,
`graph_changed` or `internal`. The streaming and reservation skeleton is
`check_runs.py`'s, shared with `/claims`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Final, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from compliance_rag.adapters.interfaces import (
    CheckNotExtracted,
    FailureReason,
    FindingOutcome,
    FindingRecord,
    FindingsAlreadyReserved,
    FindingsRecord,
    WithheldReason,
)
from compliance_rag.api.answers import citation_json
from compliance_rag.api.check_runs import CheckRun, ErrorBody, event, run_stream
from compliance_rag.api.checks import NOT_FOUND, read_passages, visible_check
from compliance_rag.api.deps import (
    ApiServices,
    CurrentCaller,
    counts,
    outcome_counts,
    services_of,
)
from compliance_rag.api.source_panel import canonical_texts
from compliance_rag.api.sources import source_href
from compliance_rag.api.tenant import held_sources, version_json
from compliance_rag.assessment.coverage import CoverageRow, question_json
from compliance_rag.assessment.decisions import Decisions
from compliance_rag.assessment.evidence import Facts, FactsSnapshot
from compliance_rag.assessment.findings import policy_disagreements
from compliance_rag.assessment.policies import tenant_policies
from compliance_rag.corpus.display_form import Titles, cite, cited_policy_versions
from compliance_rag.telemetry import current_trace_id

router = APIRouter(prefix="/v1")
log = logging.getLogger(__name__)

NOT_EXTRACTED: Final = "this check's claims have not been extracted, so there is nothing to check"
ALREADY_FOUND: Final = "this check's findings were already made"
NOT_RUN: Final = "this check has no findings yet"
MALFORMED_FINDING: Final = "malformed_finding"
"""The one reason a refused finding is reported outward. Which check refused it is on
the span: a reason that named it ("ambiguous with no settle-by") would reveal the
verdict class the model chose, which a withheld finding must not."""

REASONS: Final[dict[str, str]] = {
    WithheldReason.CITATION_BLOCKED.value: "a citation didn't match its clause, so the finding "
    "is withheld (ADR-0003)",
    WithheldReason.UNCITED.value: "the finding cited no clause, so nothing in it could be verified",
    FailureReason.MALFORMED_GENERATION.value: "the model's reply wasn't a valid set of findings; "
    "nothing in it is used",
    FailureReason.NO_FINDING_RETURNED.value: "the model returned no finding for it",
    FailureReason.MALFORMED_FINDING.value: "the model's finding for it was malformed, so nothing "
    "in it is used",
    FailureReason.GENERATION_UNAVAILABLE.value: "the model wasn't taking requests, so nothing was "
    "generated for it",
    FailureReason.CLAUSE_UNAVAILABLE.value: "a clause it rests on, or one it owes, couldn't be "
    "delivered, so the model was not asked",
    FailureReason.BOUNDARY_COLLISION.value: "the claim contains the text that fences it, so it "
    "was not sent to the model",
}
"""Why a withheld or failed finding is, in the words a member is shown: one for every
`WithheldReason` and every `FailureReason`, held to that by a test."""


class GraphChanged(RuntimeError):
    """A graph load began or ended while the coverage was computed, so it may mix
    two versions and the assessment could not honestly name one."""


def _error(failed: Exception) -> ErrorBody | None:
    """Our word for a failure that is the findings' own (the rest are
    `check_runs.common_error`'s)."""
    if isinstance(failed, GraphChanged):
        return {"error": "graph_changed", "message": str(failed)}, False
    return None


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
    a withheld or failed finding stored none, so there is none to show. What would
    settle an `ambiguous` one is the model's own words, and says so."""
    shown: dict[str, object] = {
        "finding_id": finding.finding_id,
        "claim_index": finding.claim_index,
        **_target_json(finding, titles),
        "outcome": finding.outcome.value,
        "verdict": None if finding.verdict is None else finding.verdict.value,
        "settle_by": None
        if finding.settle_by is None
        else {"text": finding.settle_by, "source": "model"},
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


def facts_json(facts: FactsSnapshot | None) -> dict[str, object] | None:
    """The facts an assessment was decided on: every evidence row it read, with its
    value, and their hash (ADR-0034)."""
    if facts is None:
        return None
    return {"sha256": facts.sha256, "evidence": facts.json()}


async def findings_json(
    services: ApiServices,
    tenant_id: str,
    check_id: str,
    record: FindingsRecord,
    decisions: Decisions | None = None,
) -> dict[str, object]:
    """A recorded findings run as a member is shown it: one builder for the
    stream's `findings` event and for `GET`, so the two cannot drift.

    This is the run **as recorded**: each finding's `provenance` is what the run stored
    (`inferred`) and nothing here changes it. What members decided since is said beside it:
    every finding, and every policy disagreement, says whether its claim now stands
    rejected (`claim_rejected`, #301: always false in the stream's event, made before
    anyone has decided, and read from the check's `decisions` in `GET`), so a finding the
    report leaves out is marked here, not returned unmarked. The tier now is the report's
    (`check_report`), computed when read."""
    payload = (await findings_with_titles(services, tenant_id, check_id, record))[0]
    standing = Decisions.of(()) if decisions is None else decisions
    for item in cast("list[dict[str, Any]]", payload["findings"]):
        item["claim_rejected"] = standing.rejected(int(item["claim_index"])) is not None
    for item in cast("list[dict[str, Any]]", payload["policy_disagreements"]):
        item["claim_rejected"] = standing.rejected(int(item["claim_index"])) is not None
    return payload


async def findings_with_titles(
    services: ApiServices, tenant_id: str, check_id: str, record: FindingsRecord
) -> tuple[dict[str, object], Titles]:
    """`findings_json`, and the policy titles it read (one read per render), for a caller
    that names the same policies again (the report's banner and coverage statement)."""
    assessment = record.assessment
    rows = {r.obligation_id: r for r in record.coverage}
    cited = {c.clause_id for f in record.findings for c in f.citations}
    named = {
        *cited,
        *(f.policy_clause_id for f in record.findings if f.policy_clause_id is not None),
        *(b.clause_id for f in record.findings for b in f.blocked),
    }
    titles = await services.ledger.version_titles(tenant_id, cited_policy_versions(named))
    texts = await canonical_texts(services, cited)
    tally = {o.value: sum(f.outcome is o for f in record.findings) for o in FindingOutcome}
    payload: dict[str, object] = {
        "check_id": check_id,
        "assessment_id": assessment.assessment_id,
        "as_of": assessment.as_of.isoformat(),
        "trace_id": assessment.trace_id,
        "corpus_version": version_json(assessment.corpus),
        "mapping_version": version_json(assessment.mapping),
        "facts": facts_json(assessment.facts),
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
    return payload, titles


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
    held_by = current_trace_id()
    try:
        await services.ledger.reserve_findings(who.tenant_id, check_id, trace_id=held_by)
    except CheckNotExtracted:
        raise HTTPException(409, {"error": "not_extracted", "message": NOT_EXTRACTED}) from None
    except FindingsAlreadyReserved:
        raise HTTPException(409, {"error": "already_found", "message": ALREADY_FOUND}) from None

    async def release() -> None:
        # Only this request's own reservation: a run that outlived it must not release
        # the request that has since taken it over.
        await services.ledger.fail_findings(who.tenant_id, check_id, trace_id=held_by)

    async def body(run: CheckRun) -> AsyncIterator[str]:
        # On the date the picks were made, so the digest the claims chose from is the
        # digest the findings are held to; over the facts as the ledger holds them now,
        # which the assessment records (ADR-0034).
        evidence = await services.ledger.evidence(who.tenant_id)
        coverage = await services.graph.coverage(
            Facts.of(evidence), tenant_id=who.tenant_id, as_of=extraction.as_of
        )
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
        made = await run.spawn(
            lambda: assessor.run(
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
                evidence=evidence,
            )
        )
        stored = await services.ledger.get_findings(who.tenant_id, check_id)
        if stored is None:  # recorded by `run`, so this is the ledger lying
            raise LookupError(f"check {check_id}: the findings were not recorded")
        yield event(
            "findings",
            {
                **await findings_json(services, who.tenant_id, check_id, stored),
                # Outward, every refusal is one reason: which check refused it is on the
                # span, and would reveal the verdict class the model chose.
                "refused": [
                    {
                        "claim_index": r.claim_index,
                        "position": r.position,
                        "reason": MALFORMED_FINDING,
                    }
                    for r in made.refused
                ],
                "skipped": list(made.skipped),
                "calls": len(made.generations),
                "generator": services.settings.generator,
                "cost_usd": made.cost_usd,
            },
        )

    return StreamingResponse(
        run_stream(
            services=services,
            who=who,
            subject={"check_id": check_id},
            release=release,
            error_of=_error,
            failure="making a check's findings",
            body=body,
        ),
        media_type="text/event-stream",
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
    decisions = Decisions.of(await services.ledger.check_decisions(who.tenant_id, check_id))
    return await findings_json(services, who.tenant_id, check_id, record, decisions)
