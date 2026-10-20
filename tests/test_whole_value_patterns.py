"""A pattern that validates a whole value ends at the string, not before a
trailing newline (#236, the same class as #234's id patterns).

`$` also matches just before a final newline, so `"abc\\n"` passed `^[a-z]+$`.
Each pattern here ends in `\\Z`; every one is checked with a trailing newline,
through the model or function that uses it, and with the clean value as the
control, so a refusal is the newline's and not something else's.
"""

from __future__ import annotations

import datetime as dt

import pytest

from compliance_rag.assessment.evidence import FIELD, Evidence, EvidenceProvenance, Period
from compliance_rag.corpus.models import (
    PARAGRAPH_SEGMENT_RE,
    Incompleteness,
    IncompleteReason,
)
from compliance_rag.eval.applicability import GoldenFact
from compliance_rag.graph.mappings import Comparison, Dated, Leaf

DETAIL = "the formula is not available in text form"


def test_an_incompleteness_detail_ending_in_a_newline_is_refused() -> None:
    """_DETAIL_RE: the detail is placed in the prompt's fence marker."""
    Incompleteness(reason=IncompleteReason.IMAGE_NOT_TEXT, detail=DETAIL)
    with pytest.raises(ValueError, match="lowercase words"):
        Incompleteness(reason=IncompleteReason.IMAGE_NOT_TEXT, detail=DETAIL + "\n")


@pytest.mark.parametrize("segment", ["1", "1a", "a", "ca", "iii", "iia"])
def test_a_paragraph_segment_ending_in_a_newline_is_not_a_paragraph(segment: str) -> None:
    """PARAGRAPH_SEGMENT_RE: tells a paragraph from a defined term's slug."""
    assert PARAGRAPH_SEGMENT_RE.match(segment)
    assert not PARAGRAPH_SEGMENT_RE.match(segment + "\n")


def test_a_mapping_id_ending_in_a_newline_is_refused() -> None:
    """mappings._ID."""
    Dated(id="test-act:v1:s1/1", valid_from=dt.date(2026, 1, 1))
    with pytest.raises(ValueError, match="not a valid id"):
        Dated(id="test-act:v1:s1/1\n", valid_from=dt.date(2026, 1, 1))


def test_a_mapping_leaf_field_ending_in_a_newline_is_refused() -> None:
    """mappings._FIELD, on both `field` and `since_field`."""
    Leaf(field="annual_turnover_aud", test=Comparison.GT, value=1, since_field="founded_on")
    with pytest.raises(ValueError, match="not snake_case"):
        Leaf(field="annual_turnover_aud\n", test=Comparison.GT, value=1)
    with pytest.raises(ValueError, match="not snake_case"):
        Leaf(field="annual_turnover_aud", test=Comparison.GT, value=1, since_field="founded_on\n")


def test_an_evidence_field_ending_in_a_newline_is_refused() -> None:
    """evidence.FIELD, through Evidence; the ledger's CHECK and a golden fact's
    field use the same rule."""

    def evidence(field: str) -> Evidence:
        return Evidence(
            tenant_id="acme",
            evidence_id="e1",
            field=field,
            value=1,
            period=Period(dt.date(2020, 1, 1)),
            provenance=EvidenceProvenance.ASSERTED,
            source="test",
            asserted_by="cfo@acme.example",
        )

    evidence("annual_turnover_aud")
    assert FIELD.match("annual_turnover_aud") and not FIELD.match("annual_turnover_aud\n")
    with pytest.raises(ValueError, match="not snake_case"):
        evidence("annual_turnover_aud\n")


def test_a_golden_fact_field_ending_in_a_newline_is_refused() -> None:
    """applicability's GoldenFact reads evidence.FIELD."""
    GoldenFact(field="annual_turnover_aud", value=1, financial_year=2025)
    with pytest.raises(ValueError, match="not snake_case"):
        GoldenFact(field="annual_turnover_aud\n", value=1, financial_year=2025)
