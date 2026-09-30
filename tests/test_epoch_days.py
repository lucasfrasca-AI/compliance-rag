"""`epoch_days()` and the bitemporal sentinels at their own boundaries
(issue #22), not just at a mid-range date every other test already covers.

`date.toordinal()` (proleptic Gregorian, day 1 = 0001-01-01) is always a
small, non-negative integer — `dt.date.min` is *not* a large negative
number, despite looking like one intuitively ("the earliest possible
date"): Python's `date` has no year-zero or BC support, so its ordinal
starts at 1, not at some negative offset from an arbitrary epoch. That
matters here specifically because a Qdrant `Range` filter on a signed
integer field would behave very differently if the lower sentinel were
large-and-negative versus small-and-positive, and this is the test that
would have caught it being wrong.
"""

from __future__ import annotations

import datetime as dt

from compliance_rag.corpus.models import NO_END_DATE, Chunk, epoch_days

# Both sentinels' expected ordinals, pinned as literal ints rather than
# re-derived via `.toordinal()` in the assertion — a regression that broke
# `date.toordinal()` itself, or swapped it for something else, must not be
# able to mark its own homework.
EARLIEST_SENTINEL_EPOCH_DAYS = 1  # dt.date.min = 0001-01-01
NO_END_DATE_EPOCH_DAYS = 3_652_059  # dt.date.max = 9999-12-31


def test_earliest_sentinel_is_a_small_positive_integer_not_a_large_negative_one() -> None:
    assert dt.date.min == dt.date(1, 1, 1)
    assert epoch_days(dt.date.min) == EARLIEST_SENTINEL_EPOCH_DAYS
    assert epoch_days(dt.date.min) > 0


def test_no_end_date_sentinel_epoch_days() -> None:
    assert NO_END_DATE == dt.date.max == dt.date(9999, 12, 31)
    assert epoch_days(NO_END_DATE) == NO_END_DATE_EPOCH_DAYS


def test_sentinels_bracket_every_real_date_not_just_mid_range() -> None:
    """The whole point of a sentinel is that ordinary dates sort strictly
    between the two of them — checked at both ends, not assumed from one
    date picked in the middle."""
    real_dates = (dt.date(1, 1, 2), dt.date(2026, 1, 1), dt.date(9999, 12, 30))
    for d in real_dates:
        assert epoch_days(dt.date.min) < epoch_days(d) < epoch_days(dt.date.max)


def test_chunk_defaults_are_the_sentinels_not_none() -> None:
    chunk = Chunk(chunk_id="c1", clause_id="privacy-act-1988:V1:s1", text="text")
    assert chunk.valid_from == dt.date.min
    assert chunk.valid_to == NO_END_DATE
    assert epoch_days(chunk.valid_from) == EARLIEST_SENTINEL_EPOCH_DAYS
    assert epoch_days(chunk.valid_to) == NO_END_DATE_EPOCH_DAYS


def test_as_of_at_the_earliest_possible_date_is_within_an_open_ended_chunks_window() -> None:
    """`as_of == dt.date.min` is the earliest date that can ever be asked
    for — an open-ended chunk (both sentinel defaults) must still cover
    it: `valid_from <= as_of` at the lower bound, same as any other date."""
    chunk = Chunk(chunk_id="c1", clause_id="privacy-act-1988:V1:s1", text="text")
    assert epoch_days(chunk.valid_from) <= epoch_days(dt.date.min)


def test_as_of_at_the_sentinel_itself_is_the_one_date_an_open_ended_chunk_excludes() -> None:
    """Known, accepted boundary quirk, not a bug: the upper bound is
    exclusive (`as_of < valid_to`, matching how a *real* `valid_to` — an
    amendment's effective date — already worked before this session's
    sentinel change), so an open-ended chunk is technically "not valid" at
    `as_of == NO_END_DATE` itself (year 9999-12-31). Every real `as_of`
    this system will ever be asked about is centuries earlier, so this
    never fires in practice — recorded here so it's a documented,
    deliberate choice rather than a silent gap discovered later."""
    chunk = Chunk(chunk_id="c1", clause_id="privacy-act-1988:V1:s1", text="text")
    assert not (epoch_days(dt.date.max) < epoch_days(chunk.valid_to))
