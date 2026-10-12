"""Status is derived from valid time, never stored (ADR-0029, D-05).

`status_on` is the one place a status comes from: before `valid_from` a
record has not commenced, from `valid_to` it has ended for its `end_reason`,
in between it is in force. An end date and its reason come together or not
at all — the mapping model, the graph's `Bitemporal` and the ledger's
`instrument_version` all refuse one without the other.
"""

from __future__ import annotations

import datetime as dt

import pytest

from compliance_rag.adapters.graph_schema import Bitemporal, GraphWriteRefused
from compliance_rag.corpus.models import (
    NO_END_DATE,
    EndReason,
    InstrumentVersion,
    Status,
    status_on,
)

JAN, JUN, DEC = dt.date(2026, 1, 1), dt.date(2026, 6, 1), dt.date(2026, 12, 1)


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        (dt.date(2025, 12, 31), Status.NOT_YET_COMMENCED),
        (JAN, Status.IN_FORCE),
        (dt.date(2026, 5, 31), Status.IN_FORCE),
        (JUN, Status.REPEALED),
        (DEC, Status.REPEALED),
    ],
)
def test_status_is_derived_from_the_dates(as_of: dt.date, expected: Status) -> None:
    assert (
        status_on(as_of, valid_from=JAN, valid_to=JUN, end_reason=EndReason.REPEALED) is expected
    )


@pytest.mark.parametrize("reason", list(EndReason))
def test_an_ended_record_reports_why_it_ended(reason: EndReason) -> None:
    assert status_on(DEC, valid_from=JAN, valid_to=JUN, end_reason=reason).value == reason.value


def test_an_open_record_is_in_force_forever_after_it_commences() -> None:
    assert (
        status_on(DEC, valid_from=JAN, valid_to=NO_END_DATE, end_reason=None) is Status.IN_FORCE
    )


def test_a_passed_but_not_commenced_provision_can_be_loaded_ahead_of_time() -> None:
    """Q7's example: loaded with its commencement as `valid_from`, an earlier
    `as_of` sees it as not yet commenced, with nothing stored to keep in step."""
    commencement = dt.date(2026, 12, 10)
    assert (
        status_on(JUN, valid_from=commencement, valid_to=NO_END_DATE, end_reason=None)
        is Status.NOT_YET_COMMENCED
    )


def test_bitemporal_needs_a_reason_with_an_end_date_and_none_without() -> None:
    with pytest.raises(GraphWriteRefused, match="together"):
        Bitemporal(valid_from=JAN, valid_to=JUN)
    with pytest.raises(GraphWriteRefused, match="together"):
        Bitemporal(valid_from=JAN, end_reason=EndReason.EXPIRED)
    assert Bitemporal(valid_from=JAN, valid_to=JUN, end_reason=EndReason.EXPIRED).end_reason


def test_an_instrument_version_derives_its_status() -> None:
    version = InstrumentVersion(
        instrument_id="privacy-act-1988",
        version_id="C2026C00227",
        title="Privacy Act 1988",
        valid_from=JUN,
        recorded_at=dt.datetime(2026, 10, 12, tzinfo=dt.UTC),
    )
    assert version.status_on(JAN) is Status.NOT_YET_COMMENCED
    assert version.is_current(DEC)
    with pytest.raises(ValueError, match="together"):
        InstrumentVersion(
            instrument_id="privacy-act-1988",
            version_id="C2025C00001",
            title="Privacy Act 1988",
            valid_from=JAN,
            valid_to=JUN,
            recorded_at=dt.datetime(2026, 10, 12, tzinfo=dt.UTC),
        )
