"""The tenant's own endpoints (#147): its profile as dated evidence (#114),
Track A's coverage of it (D-06), and its library subscriptions (ADR-0020).
All of it is over adapters already built and tested; the API adds no rule of
its own.

- **Profile.** Every evidence row, current or superseded, and what the
  current evidence says about each field on `as_of`. Disagreeing facts are
  reported as a conflict, never resolved. A member records a fact as an
  *asserted* one in their own name. Nothing here infers a fact or accepts
  another provenance: the request body can't name one.
- **Coverage.** Track A over the tenant's evidence on `as_of`: every obligation
  applicable / not applicable / unassessable, each unassessable one with what
  would settle it. A GET computes it; a POST also records it as an assessment.
  - Its `corpus_version` is the compilations its clauses come from.
  - Its `mapping_version` fingerprints the obligation, role and condition ids
    it evaluated. A mapping's meaning is immutable per id (ADR-0028,
    ADR-0031), so those ids identify exactly what was applied.
- **Subscriptions.** Subscribe and unsubscribe; unsubscribing deletes the
  subscription row only (D-17).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from compliance_rag.api.deps import Caller, current_caller, services_of
from compliance_rag.assessment.coverage import (
    Coverage,
    CoverageRow,
    Outcome,
    Question,
    question_json,
)
from compliance_rag.assessment.coverage_report import cite
from compliance_rag.assessment.evidence import (
    Evidence,
    EvidenceProvenance,
    Facts,
    FactValue,
    Period,
    current_evidence,
)
from compliance_rag.corpus.models import NO_END_DATE, parse_stable_id
from compliance_rag.telemetry import current_trace_id

router = APIRouter(prefix="/v1")
SYDNEY = ZoneInfo("Australia/Sydney")


def today() -> dt.date:
    """The default `as_of`: today where the law is (D-07)."""
    return dt.datetime.now(SYDNEY).date()


AsOf = Annotated[
    dt.date | None, Query(description="The date to read on; today in Sydney if omitted")
]
Signed = Annotated[Caller, Depends(current_caller)]


# ---- shapes -----------------------------------------------------------------------


def _json_value(value: FactValue | None) -> object:
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    return value


class PeriodIn(BaseModel):
    """Exactly one of: a financial year (`2025` is FY2025-26), a date the fact
    holds from (open-ended), or a start and an end (`[start, end)`)."""

    model_config = ConfigDict(extra="forbid")
    financial_year: int | None = Field(default=None, ge=1901, le=2200)
    since: dt.date | None = None
    start: dt.date | None = None
    end: dt.date | None = None

    @model_validator(mode="after")
    def _one_form(self) -> PeriodIn:
        forms = [self.financial_year is not None, self.since is not None, self.start is not None]
        if sum(forms) != 1 or (self.end is not None and self.start is None):
            raise ValueError("give one of financial_year, since, or start (with an optional end)")
        return self

    def period(self) -> Period:
        if self.financial_year is not None:
            return Period.financial_year(self.financial_year)
        if self.since is not None:
            return Period(self.since)
        assert self.start is not None  # _one_form
        return Period(self.start, self.end or NO_END_DATE)


class EvidenceIn(BaseModel):
    """A fact a member asserts. Exactly one of `value` and `value_date`: a
    date travels as `value_date`, so a string that looks like a date is never
    silently read as one. No provenance, tenant or author fields: the API
    sets them."""

    model_config = ConfigDict(extra="forbid")
    field: str
    value: bool | int | float | str | list[str] | None = None
    value_date: dt.date | None = None
    period: PeriodIn
    source: str = Field(min_length=1, description="Where the member got it from")
    supersedes: str | None = None

    @model_validator(mode="after")
    def _one_value(self) -> EvidenceIn:
        if (self.value is None) == (self.value_date is None):
            raise ValueError("give exactly one of value and value_date")
        return self

    def fact(self) -> FactValue:
        if self.value_date is not None:
            return self.value_date
        assert self.value is not None  # _one_value
        return tuple(self.value) if isinstance(self.value, list) else self.value


def _evidence_json(e: Evidence, *, current: bool) -> dict[str, object]:
    return {
        "evidence_id": e.evidence_id,
        "field": e.field,
        "value": _json_value(e.value),
        "period": {
            "start": e.period.start.isoformat(),
            "end": None if e.period.end == NO_END_DATE else e.period.end.isoformat(),
            "label": e.period.label,
        },
        "provenance": e.provenance.value,
        "source": e.source,
        "asserted_by": e.asserted_by,
        "supersedes": e.supersedes,
        "recorded_at": e.recorded_at.isoformat() if e.recorded_at else None,
        "current": current,
    }


def _citations(clause_ids: tuple[str, ...]) -> list[dict[str, str]]:
    return [{"clause_id": c, "cite": cite(c)} for c in clause_ids]


def _question_json(q: Question) -> dict[str, object]:
    return {**question_json(q), "cites": [cite(c) for c in q.clause_ids]}


def _coverage_json(coverage: Coverage) -> dict[str, object]:
    return {
        "tenant_id": coverage.tenant_id,
        "as_of": coverage.as_of.isoformat(),
        "counts": {o.value: len(coverage.with_outcome(o)) for o in Outcome},
        "obligations": [
            {
                "obligation_id": o.obligation_id,
                "instrument_id": o.instrument_id,
                "summary": o.summary,
                "outcome": o.outcome.value,
                "reason": o.reason,
                "citations": _citations(o.citations),
                "questions": [_question_json(q) for q in o.questions],
            }
            for o in coverage.obligations
        ],
        "roles": [
            {"actor_id": a.actor_id, "name": a.name, "truth": a.truth.value}
            for a in coverage.actors
        ],
        "questions": [_question_json(q) for q in coverage.questions],
    }


def corpus_version(coverage: Coverage) -> str:
    """The compilations the coverage's clauses come from."""
    versions = {
        ":".join(parse_stable_id(c)[:2])
        for o in coverage.obligations
        for c in (*o.clause_ids, *o.citations)
    }
    return ",".join(sorted(versions)) or "none"


def mapping_version(coverage: Coverage) -> str:
    """A fingerprint of every obligation, role and condition it evaluated."""
    ids = sorted(
        {o.obligation_id for o in coverage.obligations}
        | {a.actor_id for a in coverage.actors}
        | {n.condition_id for o in coverage.obligations for n in o.explanation.walk()}
        | {n.condition_id for a in coverage.actors for n in a.explanation.walk()}
    )
    return "sha256:" + hashlib.sha256("\n".join(ids).encode()).hexdigest()[:16]


# ---- profile ----------------------------------------------------------------------


@router.get("/profile")
async def get_profile(request: Request, who: Signed, as_of: AsOf = None) -> dict[str, object]:
    day = as_of or today()
    evidence = await services_of(request).ledger.evidence(who.tenant_id)
    current = {e.evidence_id for e in current_evidence(evidence)}
    facts = Facts.of(evidence)
    readings: dict[str, object] = {}
    for field in sorted({e.field for e in facts.current}):
        reading = facts.on(field, day)
        readings[field] = {
            "value": _json_value(reading.value),
            "conflict": reading.conflict,
            "missing": reading.missing,
            "evidence_ids": [e.evidence_id for e in reading.evidence],
        }
    return {
        "tenant_id": who.tenant_id,
        "as_of": day.isoformat(),
        "evidence": [_evidence_json(e, current=e.evidence_id in current) for e in evidence],
        "readings": readings,
    }


@router.post("/profile/evidence", status_code=201)
async def record_evidence(request: Request, who: Signed, body: EvidenceIn) -> dict[str, object]:
    try:
        evidence = Evidence(
            tenant_id=who.tenant_id,
            evidence_id=uuid.uuid4().hex,
            field=body.field,
            value=body.fact(),
            period=body.period.period(),
            provenance=EvidenceProvenance.ASSERTED,
            source=body.source,
            asserted_by=who.member.email,
            supersedes=body.supersedes,
            trace_id=current_trace_id(),
        )
        await services_of(request).ledger.record_evidence(evidence)
    except (ValueError, KeyError) as refused:
        raise HTTPException(422, str(refused)) from None
    return _evidence_json(evidence, current=True)


# ---- coverage ---------------------------------------------------------------------


async def _coverage(request: Request, who: Caller, day: dt.date) -> Coverage:
    services = services_of(request)
    evidence = await services.ledger.evidence(who.tenant_id)
    return await services.graph.coverage(Facts.of(evidence), tenant_id=who.tenant_id, as_of=day)


@router.get("/coverage")
async def get_coverage(request: Request, who: Signed, as_of: AsOf = None) -> dict[str, object]:
    return _coverage_json(await _coverage(request, who, as_of or today()))


@router.post("/coverage", status_code=201)
async def record_coverage(request: Request, who: Signed, as_of: AsOf = None) -> dict[str, object]:
    """Computes the coverage and records it as an assessment: the assessment
    row, then one coverage row per obligation, both with this request's
    `trace_id`."""
    coverage = await _coverage(request, who, as_of or today())
    ledger = services_of(request).ledger
    assessment_id = uuid.uuid4().hex
    trace_id = current_trace_id()
    await ledger.record_assessment(
        tenant_id=who.tenant_id,
        assessment_id=assessment_id,
        corpus_version=corpus_version(coverage),
        mapping_version=mapping_version(coverage),
        as_of=coverage.as_of,
        trace_id=trace_id,
    )
    await ledger.record_coverage(
        tenant_id=who.tenant_id, assessment_id=assessment_id, coverage=coverage, trace_id=trace_id
    )
    return {"assessment_id": assessment_id, **_coverage_json(coverage)}


def _row_json(row: CoverageRow) -> dict[str, object]:
    return {
        "obligation_id": row.obligation_id,
        "outcome": row.outcome.value,
        "reason": row.reason,
        "citations": _citations(row.citations),
        "questions": [_question_json(q) for q in row.questions],
    }


@router.get("/coverage/{assessment_id}")
async def get_recorded_coverage(
    request: Request, who: Signed, assessment_id: str
) -> dict[str, object]:
    ledger = services_of(request).ledger
    assessment = await ledger.get_assessment(who.tenant_id, assessment_id)
    if assessment is None:
        raise HTTPException(404, "no such assessment")
    rows = await ledger.coverage_rows(who.tenant_id, assessment_id)
    return {
        "assessment_id": assessment_id,
        "as_of": str(assessment.get("as_of")),
        "corpus_version": assessment.get("corpus_version"),
        "mapping_version": assessment.get("mapping_version"),
        "obligations": [_row_json(r) for r in rows],
    }


# ---- subscriptions ----------------------------------------------------------------


@router.get("/subscriptions")
async def list_subscriptions(request: Request, who: Signed) -> dict[str, list[str]]:
    subscribed = await services_of(request).ledger.subscribed_instruments(who.tenant_id)
    return {"instruments": sorted(subscribed)}


@router.put("/subscriptions/{instrument_id}", status_code=204)
async def subscribe(request: Request, who: Signed, instrument_id: str) -> Response:
    try:
        await services_of(request).ledger.subscribe(
            tenant_id=who.tenant_id, instrument_id=instrument_id, subscribed_by=who.member.email
        )
    except ValueError as refused:
        raise HTTPException(422, str(refused)) from None
    return Response(status_code=204)


@router.delete("/subscriptions/{instrument_id}", status_code=204)
async def unsubscribe(request: Request, who: Signed, instrument_id: str) -> Response:
    """The subscription row only: nothing is deleted from any store (D-17)."""
    await services_of(request).ledger.unsubscribe(
        tenant_id=who.tenant_id, instrument_id=instrument_id
    )
    return Response(status_code=204)
