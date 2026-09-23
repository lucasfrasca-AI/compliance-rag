"""Canonicalisation version (`CONTEXT.md` **Canonicalisation version**, issue #2).

Every citation records the version it was verified under, so changing
`canonicalise()` can never silently invalidate citations already issued
(D-02). `CANONICALISATION_VERSION` is the current version; `ClauseRecord` and
`Citation` each carry the version their text was canonicalised under — a
plain field, not forced to equal the current constant, since reconstructing
a stored record must be able to carry an older version untouched.
"""

from __future__ import annotations

from compliance_rag.corpus.models import CANONICALISATION_VERSION, Citation, ClauseRecord

CLAUSE_ID = "privacy-act-1988:TEST-FIXTURE-001:sch1/app11/1"


def test_canonicalisation_version_is_two() -> None:
    """Bumped from 1 to 2 when canonicalise() gained the non-breaking-hyphen
    fold (issue #9's ePub format spike, docs/research/legislation-gov-au-format.md).
    A future bump updates this assertion too — that's the point of pinning
    an exact number rather than a range.
    """
    assert CANONICALISATION_VERSION == 2


def test_clause_record_build_stamps_the_current_version() -> None:
    record = ClauseRecord.build(
        instrument_id="privacy-act-1988",
        version_id="TEST-FIXTURE-001",
        clause_path="sch1/app11/1",
        text="Text.",
    )
    assert record.canonicalisation_version == CANONICALISATION_VERSION


def test_clause_record_can_carry_a_historical_version() -> None:
    """Reconstructing a stored record must not silently promote it to the
    current version — this is what lets an old citation be told apart from
    one broken by a real canonicalisation change."""
    record = ClauseRecord(
        instrument_id="privacy-act-1988",
        version_id="TEST-FIXTURE-001",
        clause_path="sch1/app11/1",
        text="Text.",
        canonical_text="Text.",
        canonicalisation_version=1,
    )
    assert record.canonicalisation_version == 1


def test_citation_defaults_to_the_current_version() -> None:
    citation = Citation(clause_id=CLAUSE_ID, quoted_text="Text.")
    assert citation.canonicalisation_version == CANONICALISATION_VERSION


def test_citation_can_carry_a_historical_version() -> None:
    citation = Citation(clause_id=CLAUSE_ID, quoted_text="Text.", canonicalisation_version=1)
    assert citation.canonicalisation_version == 1
