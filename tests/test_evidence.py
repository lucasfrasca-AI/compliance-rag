"""Profile facts as dated evidence (issue #114): periods, financial years,
and what the evaluator reads — current evidence that can settle a
condition, with disagreement reported rather than resolved by guessing.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from compliance_rag.assessment.evidence import (
    Evidence,
    EvidenceProvenance,
    Facts,
    Period,
    current_evidence,
)

FY25 = Period.financial_year(2025)


def _fact(
    evidence_id: str,
    value: object,
    *,
    field: str = "annual_turnover_aud",
    period: Period = FY25,
    provenance: EvidenceProvenance = EvidenceProvenance.ASSERTED,
    supersedes: str | None = None,
) -> Evidence:
    return Evidence(
        tenant_id="acme",
        evidence_id=evidence_id,
        field=field,
        value=value,  # pyright: ignore[reportArgumentType] — test values are FactValues
        period=period,
        provenance=provenance,
        source="test",
        asserted_by="cfo@acme.example" if provenance is EvidenceProvenance.ASSERTED else None,
        supersedes=supersedes,
    )


@pytest.mark.parametrize(
    ("day", "starting"),
    [
        (dt.date(2026, 10, 12), 2026),
        (dt.date(2026, 7, 1), 2026),
        (dt.date(2026, 6, 30), 2025),
        (dt.date(2026, 1, 1), 2025),
    ],
)
def test_the_australian_financial_year_runs_july_to_june(day: dt.date, starting: int) -> None:
    year = Period.financial_year_of(day)
    assert year == Period(dt.date(starting, 7, 1), dt.date(starting + 1, 7, 1))
    assert year.contains(day)


def test_s6d_1_reads_the_year_before_the_test_time() -> None:
    """As of 12 Oct 2026 the current year is FY2026-27; s6D(1) reads FY2025-26."""
    current = Period.financial_year_of(dt.date(2026, 10, 12))
    assert current.label == "FY2026-27"
    assert current.previous_financial_year().label == "FY2025-26"


def test_a_period_ends_after_it_starts() -> None:
    with pytest.raises(ValueError, match="not after"):
        Period(dt.date(2026, 1, 1), dt.date(2026, 1, 1))


def test_documents_inform_humans_assert() -> None:
    """An inferred claim never settles a condition (CONTEXT.md: Evidence)."""
    facts = Facts.of([_fact("e1", 4_000_000, provenance=EvidenceProvenance.INFERRED)])
    assert facts.for_period("annual_turnover_aud", FY25).missing


def test_a_correction_supersedes_and_the_original_is_kept() -> None:
    rows = [_fact("e1", 2_400_000), _fact("e2", 2_500_000, supersedes="e1")]
    reading = Facts.of(rows).for_period("annual_turnover_aud", FY25)
    assert reading.value == 2_500_000
    assert [e.evidence_id for e in reading.evidence] == ["e2"]
    assert [e.evidence_id for e in current_evidence(rows)] == ["e2"]


def test_two_current_facts_that_disagree_are_a_conflict_not_a_guess() -> None:
    reading = Facts.of([_fact("e1", 2_400_000), _fact("e2", 3_100_000)]).for_period(
        "annual_turnover_aud", FY25
    )
    assert reading.conflict and reading.value is None
    assert {e.evidence_id for e in reading.evidence} == {"e1", "e2"}


def test_a_state_is_read_on_a_date() -> None:
    since_2019 = Period(dt.date(2019, 3, 1))
    facts = Facts.of([_fact("e1", "body_corporate", field="legal_form", period=since_2019)])
    assert facts.on("legal_form", dt.date(2026, 10, 12)).value == "body_corporate"
    assert facts.on("legal_form", dt.date(2018, 1, 1)).missing


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"field": "Annual Turnover"}, "snake_case"),
        ({"source": " "}, "source"),
        ({"asserted_by": None}, "asserted"),
        ({"supersedes": "e1"}, "itself"),
        ({"tenant_id": "_public"}, "_public"),
    ],
)
def test_evidence_says_what_who_and_from_where(change: dict[str, object], message: str) -> None:
    fields: dict[str, object] = {
        "tenant_id": "acme",
        "evidence_id": "e1",
        "field": "annual_turnover_aud",
        "value": 1,
        "period": FY25,
        "provenance": EvidenceProvenance.ASSERTED,
        "source": "CFO",
        "asserted_by": "cfo@acme.example",
    } | change
    with pytest.raises(ValueError, match=message):
        Evidence(**fields)  # pyright: ignore[reportArgumentType] — deliberately varied


# ---- review of #124 ------------------------------------------------------------


def test_an_inferred_row_cannot_unsettle_an_asserted_fact() -> None:
    rows = [
        _fact("e1", 2_500_000),
        _fact("e2", 4_000_000, provenance=EvidenceProvenance.INFERRED, supersedes="e1"),
    ]
    assert Facts.of(rows).for_period("annual_turnover_aud", FY25).value == 2_500_000


def test_evidence_for_part_of_the_period_is_a_conflict_not_a_value() -> None:
    """A part year (s6DA(2) annualises it; this model doesn't guess) or a
    period straddling the one asked about is reported, never ignored."""
    part = Period(dt.date(2025, 3, 1), dt.date(2025, 7, 1))
    reading = Facts.of([_fact("e1", 900_000, period=part)]).for_period(
        "annual_turnover_aud", Period.financial_year(2024)
    )
    assert reading.conflict and not reading.missing


def test_overlapping_facts_that_disagree_conflict() -> None:
    straddling = Period(dt.date(2025, 1, 1), dt.date(2026, 1, 1))
    reading = Facts.of(
        [_fact("e1", 2_500_000), _fact("e2", 3_500_000, period=straddling)]
    ).for_period("annual_turnover_aud", FY25)
    assert reading.conflict


def test_values_of_different_types_disagree() -> None:
    since = Period(dt.date(2020, 1, 1))
    reading = Facts.of(
        [_fact("e1", 1, field="flag", period=since), _fact("e2", True, field="flag", period=since)]
    ).on("flag", dt.date(2026, 1, 1))
    assert reading.conflict


def test_evidence_can_be_reread_as_it_stood() -> None:
    """What an earlier assessment read: evidence recorded after it is left out."""
    then = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    rows = [
        replace(_fact("e1", 2_400_000), recorded_at=then - dt.timedelta(days=1)),
        replace(_fact("e2", 2_500_000, supersedes="e1"), recorded_at=then + dt.timedelta(days=1)),
    ]
    assert (
        Facts.of(rows, recorded_by=then).for_period("annual_turnover_aud", FY25).value == 2_400_000
    )
    assert Facts.of(rows).for_period("annual_turnover_aud", FY25).value == 2_500_000


def test_a_fact_is_a_date_never_a_datetime() -> None:
    with pytest.raises(ValueError, match="datetime"):
        _fact("e1", dt.datetime(2026, 1, 1), field="business_started_on")
