"""The tenant's own endpoints (#147): its profile as dated evidence (#114),
Track A's coverage of it (D-06), and its library subscriptions (ADR-0020).
They sit over adapters already built and tested, and decide no applicability
themselves.

- **Profile.** Every evidence row, and what the current settling evidence
  says for each field and period it covers. Disagreeing facts are reported as
  a conflict, never resolved. A member records a fact as an *asserted* one in
  their own name. Nothing here infers a fact or accepts another provenance:
  the request body can't name one.
- **Coverage.** Track A over the tenant's evidence on `as_of`: every obligation
  applicable / not applicable / unassessable, each unassessable one with what
  would settle it. `GET /v1/coverage` computes it; nothing is recorded.
  `POST /v1/coverage` records it as an assessment (#153, ADR-0034): the
  corpus and mapping versions the graph's loads recorded (a tag and the commit
  it pointed at, CONTEXT.md), and every obligation's row, in one ledger
  transaction, in the member's name. It refuses (503) while the graph holds no
  finished load of either kind, while one is unfinished, and if a load
  starts or finishes while the coverage is computed. `GET /v1/coverage/{id}`
  reads a recorded assessment back, row for row.
- **Subscriptions.** Subscribe and unsubscribe; unsubscribing deletes the
  subscription row only (D-17).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from compliance_rag.adapters.interfaces import (
    AssessmentRecord,
    LoadedSources,
    SourceKind,
    SourceVersion,
)
from compliance_rag.api.deps import (
    EARLIEST,
    LATEST,
    CurrentCaller,
    counts,
    coverage_on,
    outcome_counts,
    services_of,
    today,
)
from compliance_rag.assessment.coverage import (
    Coverage,
    CoverageRow,
    ObligationCoverage,
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
)
from compliance_rag.corpus.models import NO_END_DATE
from compliance_rag.telemetry import current_trace_id

router = APIRouter(prefix="/v1")

AsOf = Annotated[
    dt.date | None, Query(description="The date to read on; today in Sydney if omitted")
]

Text = Annotated[str, StringConstraints(max_length=2_000)]
Item = Annotated[str, StringConstraints(min_length=1, max_length=200)]


# ---- shapes -----------------------------------------------------------------------


def _json_value(value: FactValue | None) -> object:
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    return value


def _period_json(period: Period) -> dict[str, object]:
    return {
        "start": period.start.isoformat(),
        "end": None if period.end == NO_END_DATE else period.end.isoformat(),
        "label": period.label,
    }


class PeriodIn(BaseModel):
    """A financial year (`2025` is FY2025-26), or a start with an optional end
    (`[start, end)`; open-ended without one)."""

    model_config = ConfigDict(extra="forbid")
    financial_year: int | None = Field(default=None, ge=EARLIEST.year, le=LATEST.year)
    start: dt.date | None = Field(default=None, ge=EARLIEST, le=LATEST)
    end: dt.date | None = Field(default=None, ge=EARLIEST, le=LATEST)

    @model_validator(mode="after")
    def _one_form(self) -> PeriodIn:
        if (self.financial_year is None) == (self.start is None) or (
            self.end is not None and self.start is None
        ):
            raise ValueError("give financial_year, or start (with an optional end)")
        return self

    def period(self) -> Period:
        if self.financial_year is not None:
            return Period.financial_year(self.financial_year)
        assert self.start is not None  # _one_form
        return Period(self.start, self.end or NO_END_DATE)


class EvidenceIn(BaseModel):
    """A fact a member asserts. Exactly one of `value` and `value_date`: a
    date travels as `value_date`, so a string that looks like a date is never
    silently read as one. Numbers are finite; text is bounded and never holds
    NUL. No provenance, tenant or author fields: the API sets them."""

    model_config = ConfigDict(extra="forbid")
    field: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    value: (
        bool
        | int
        | Annotated[float, Field(allow_inf_nan=False)]
        | Text
        | Annotated[list[Item], Field(max_length=100)]
        | None
    ) = None
    value_date: dt.date | None = Field(default=None, ge=EARLIEST, le=LATEST)
    period: PeriodIn
    source: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    supersedes: Annotated[str, StringConstraints(max_length=64)] | None = None

    @model_validator(mode="after")
    def _one_value_no_nul(self) -> EvidenceIn:
        if (self.value is None) == (self.value_date is None):
            raise ValueError("give exactly one of value and value_date")
        texts = [self.field, self.source, self.supersedes or ""]
        texts += self.value if isinstance(self.value, list) else [str(self.value)]
        if any("\x00" in t for t in texts):
            raise ValueError("text may not contain NUL")
        return self

    def fact(self) -> FactValue:
        if self.value_date is not None:
            return self.value_date
        assert self.value is not None  # _one_value_no_nul
        return tuple(self.value) if isinstance(self.value, list) else self.value


def _evidence_json(e: Evidence, *, current: bool) -> dict[str, object]:
    return {
        "evidence_id": e.evidence_id,
        "field": e.field,
        "value": _json_value(e.value),
        "period": _period_json(e.period),
        "provenance": e.provenance.value,
        "source": e.source,
        "asserted_by": e.asserted_by,
        "supersedes": e.supersedes,
        "recorded_at": e.recorded_at.isoformat() if e.recorded_at else None,
        "current": current,
    }


def _question_json(q: Question) -> dict[str, object]:
    return {**question_json(q), "cites": [cite(c) for c in q.clause_ids]}


def _row_json(row: CoverageRow) -> dict[str, object]:
    """An obligation as the ledger keeps it: the outcome, why, the clauses it
    rests on and, if unassessable, what would settle it."""
    return {
        "obligation_id": row.obligation_id,
        "outcome": row.outcome.value,
        "reason": row.reason,
        "citations": [{"clause_id": c, "cite": cite(c)} for c in row.citations],
        "questions": [_question_json(q) for q in row.questions],
    }


def _obligation_json(o: ObligationCoverage) -> dict[str, object]:
    """Computed coverage also says whose obligation it is and in what words:
    the ledger keeps neither (`coverage` has no such column)."""
    return {
        "obligation_id": o.obligation_id,
        "instrument_id": o.instrument_id,
        "summary": o.summary,
        **_row_json(CoverageRow.of(o)),
    }


def _coverage_json(coverage: Coverage) -> dict[str, object]:
    return {
        "tenant_id": coverage.tenant_id,
        "as_of": coverage.as_of.isoformat(),
        "counts": counts(coverage),
        "obligations": [_obligation_json(o) for o in coverage.obligations],
        "roles": [
            {"actor_id": a.actor_id, "name": a.name, "truth": a.truth.value}
            for a in coverage.actors
        ],
        "questions": [_question_json(q) for q in coverage.questions],
    }


# ---- profile ----------------------------------------------------------------------


@router.get("/profile")
async def get_profile(request: Request, who: CurrentCaller) -> dict[str, object]:
    """`current` is what Track A reads: settling evidence (asserted or
    parsed) nothing settling supersedes. Each reading is what that evidence
    says for one field over one period it covers: a financial year's figure
    for that year, a state from the day it holds."""
    evidence = await services_of(request).ledger.evidence(who.tenant_id)
    facts = Facts.of(evidence)
    current = {e.evidence_id for e in facts.current}
    readings: list[dict[str, object]] = []
    for field, period in sorted(
        {(e.field, e.period) for e in facts.current}, key=lambda fp: (fp[0], fp[1].start)
    ):
        reading = facts.for_period(field, period)
        readings.append(
            {
                "field": field,
                "period": _period_json(period),
                "value": _json_value(reading.value),
                "conflict": reading.conflict,
                "evidence_ids": [e.evidence_id for e in reading.evidence],
            }
        )
    return {
        "tenant_id": who.tenant_id,
        "evidence": [_evidence_json(e, current=e.evidence_id in current) for e in evidence],
        "readings": readings,
    }


@router.post("/profile/evidence", status_code=201)
async def record_evidence(
    request: Request, who: CurrentCaller, body: EvidenceIn
) -> dict[str, object]:
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
    except ValueError as refused:
        raise HTTPException(422, str(refused)) from None
    return _evidence_json(evidence, current=True)


# ---- coverage ---------------------------------------------------------------------


@router.get("/coverage")
async def get_coverage(
    request: Request, who: CurrentCaller, as_of: AsOf = None
) -> dict[str, object]:
    day = as_of or today()
    if not EARLIEST <= day <= LATEST:
        raise HTTPException(422, f"as_of must be between {EARLIEST} and {LATEST}")
    return _coverage_json(await coverage_on(services_of(request), who.tenant_id, day))


# ---- recorded assessments (#153) ---------------------------------------------------

ASSESSMENT_ID = r"^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$"
"""What `uuid.uuid4().hex` makes."""

RETRY_AFTER = "30"
"""Seconds: a guess at how long a load takes, and asking sooner is harmless."""

LOADERS = {
    SourceKind.CORPUS: "scripts.load_graph_structure",
    SourceKind.MAPPING: "scripts.load_mappings",
}


class AssessmentIn(BaseModel):
    """What a member says about an assessment: the date, and nothing else. The
    versions it records are what the graph holds, never what a request names."""

    model_config = ConfigDict(extra="forbid")
    as_of: dt.date | None = Field(default=None, ge=EARLIEST, le=LATEST)


def _version_json(version: SourceVersion) -> dict[str, object]:
    return {"tag": version.tag, "sha": version.sha, "untagged": version.untagged}


def _recorded_json(record: AssessmentRecord, rows: tuple[CoverageRow, ...]) -> dict[str, object]:
    return {
        "assessment_id": record.assessment_id,
        "tenant_id": record.tenant_id,
        "as_of": record.as_of.isoformat(),
        "corpus_version": _version_json(record.corpus),
        "mapping_version": _version_json(record.mapping),
        "recorded_by": record.user_id,
        "counts": outcome_counts(r.outcome for r in rows),
        "obligations": [_row_json(r) for r in rows],
    }


def _held(sources: LoadedSources) -> tuple[SourceVersion, SourceVersion]:
    """The corpus and mapping versions the graph holds, or the 503 saying why
    there is nothing to record an assessment against: a load is open (wait),
    or one was abandoned after it may have written and none has finished since
    (waiting won't help; a load must), or no load has ever finished."""
    unfinished = [k.value for k in SourceKind if sources.of(k).loading]
    if unfinished:
        raise HTTPException(
            503,
            f"a {' and a '.join(unfinished)} load hasn't finished (it is running, or it failed "
            "and nobody has dealt with it): nothing is assessed against a graph that may be "
            "part-written. Try again when it has finished, or, if it is known to be dead, have "
            "the operator abandon it (the loader's --abandon) and load again",
            headers={"Retry-After": RETRY_AFTER},
        )
    abandoned = [k for k in SourceKind if sources.of(k).blocked_by_abandoned]
    if abandoned:
        raise HTTPException(
            503,
            f"a {' and a '.join(k.value for k in abandoned)} load was abandoned after it may "
            "have written, and none has finished since, so the graph may be part-written: "
            "nothing is assessed against it until a load finishes (#153): run "
            + " and ".join(LOADERS[k] for k in abandoned),
        )
    corpus, mapping = sources.corpus.version, sources.mapping.version
    if corpus is None or mapping is None:
        absent = [k for k in SourceKind if sources.of(k).version is None]
        raise HTTPException(
            503,
            "no finished load records which "
            + " and ".join(f"{k.value} version" for k in absent)
            + " the graph holds, so an assessment couldn't name them (#153): run "
            + " and ".join(LOADERS[k] for k in absent),
        )
    return corpus, mapping


@router.post("/coverage", status_code=201)
async def record_coverage(
    request: Request, who: CurrentCaller, body: AssessmentIn | None = None
) -> dict[str, object]:
    """Track A's coverage on `as_of`, recorded as an assessment against the
    corpus and mapping versions the graph holds. The graph's loads are read
    before and after the coverage is computed: if they differ, a load began or
    ended in between and the coverage may mix two versions, so nothing is
    recorded."""
    day = (body.as_of if body else None) or today()
    services = services_of(request)
    before = await services.graph.loaded_sources()
    corpus, mapping = _held(before)
    coverage = await coverage_on(services, who.tenant_id, day)
    if await services.graph.loaded_sources() != before:
        raise HTTPException(
            503,
            "a load started or finished while the coverage was computed, so it may mix two "
            "versions: nothing was recorded. Try again",
            headers={"Retry-After": RETRY_AFTER},
        )
    rows = tuple(
        sorted((CoverageRow.of(o) for o in coverage.obligations), key=lambda r: r.obligation_id)
    )
    record = AssessmentRecord(
        tenant_id=who.tenant_id,
        assessment_id=uuid.uuid4().hex,
        user_id=who.member.email,
        corpus=corpus,
        mapping=mapping,
        as_of=day,
        trace_id=current_trace_id(),
    )
    await services.ledger.record_coverage_assessment(record, rows)
    current = trace.get_current_span()
    current.set_attribute("assessment.id", record.assessment_id)
    current.set_attribute("assessment.corpus_version", corpus.described)
    current.set_attribute("assessment.mapping_version", mapping.described)
    return _recorded_json(record, rows)


@router.get("/coverage/{assessment_id}")
async def get_recorded_coverage(
    request: Request, who: CurrentCaller, assessment_id: Annotated[str, Path(pattern=ASSESSMENT_ID)]
) -> dict[str, object]:
    """A recorded assessment as it was written: its versions, who asked, and
    each obligation's row. Another tenant's is not found, not forbidden."""
    ledger = services_of(request).ledger
    record = await ledger.get_assessment(who.tenant_id, assessment_id)
    if record is None:
        raise HTTPException(404, "no such assessment")
    return _recorded_json(record, await ledger.coverage_rows(who.tenant_id, assessment_id))


# ---- subscriptions ----------------------------------------------------------------


@router.get("/subscriptions")
async def list_subscriptions(request: Request, who: CurrentCaller) -> dict[str, list[str]]:
    subscribed = await services_of(request).ledger.subscribed_instruments(who.tenant_id)
    return {"instruments": sorted(subscribed)}


@router.put("/subscriptions/{instrument_id}", status_code=204)
async def subscribe(request: Request, who: CurrentCaller, instrument_id: str) -> Response:
    try:
        await services_of(request).ledger.subscribe(
            tenant_id=who.tenant_id,
            instrument_id=instrument_id,
            subscribed_by=who.member.email,
            trace_id=current_trace_id(),
        )
    except ValueError as refused:
        raise HTTPException(422, str(refused)) from None
    return Response(status_code=204)


@router.delete("/subscriptions/{instrument_id}", status_code=204)
async def unsubscribe(request: Request, who: CurrentCaller, instrument_id: str) -> Response:
    """The subscription row only: nothing is deleted from any store (D-17)."""
    await services_of(request).ledger.unsubscribe(
        tenant_id=who.tenant_id, instrument_id=instrument_id
    )
    return Response(status_code=204)
