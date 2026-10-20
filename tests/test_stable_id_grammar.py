"""Stable-ID grammar assertions (`CONTEXT.md` **Clause path**, issue #4).

`CLAUSE_PATH_RE` and `STABLE_ID_RE` encode a grammar, not an accident of what
happens to match — these tests pin the actual shape down (round-trips, and
everything that must be rejected) instead of trusting the regex to behave by
luck.
"""

from __future__ import annotations

import pytest

from compliance_rag.corpus.models import (
    CLAUSE_PATH_RE,
    INSTRUMENT_ID_RE,
    STABLE_ID_RE,
    VERSION_ID_RE,
    parse_stable_id,
    stable_id,
)

INSTRUMENT_ID = "privacy-act-1988"
VERSION_ID = "TEST-FIXTURE-001"

# One table for the clause-path grammar: the real forms it must accept, and
# the display form it must reject (parentheses and uppercase — CONTEXT.md:
# Display form is derived from the path, never stored as one).
CLAUSE_PATH_CASES: list[tuple[str, bool]] = [
    ("sch1/app11/1", True),
    ("s6c/1/a", True),
    ("art9/2/b", True),
    ("s6C(1)", False),
]


@pytest.mark.parametrize(
    "clause_path,is_valid", CLAUSE_PATH_CASES, ids=[case[0] for case in CLAUSE_PATH_CASES]
)
def test_clause_path_grammar(clause_path: str, is_valid: bool) -> None:
    if is_valid:
        built = stable_id(INSTRUMENT_ID, VERSION_ID, clause_path)
        assert parse_stable_id(built) == (INSTRUMENT_ID, VERSION_ID, clause_path)
    else:
        with pytest.raises(ValueError, match="invalid clause_path"):
            stable_id(INSTRUMENT_ID, VERSION_ID, clause_path)


# ':' is the only stable-ID delimiter (CONTEXT.md: Clause) — a stray one
# inside any segment must be rejected there, not silently absorbed into the
# wrong part.
COLON_IN_SEGMENT_CASES: list[tuple[str, str, str]] = [
    ("acme:corp", VERSION_ID, "sec2/1"),
    (INSTRUMENT_ID, "v:1", "sec2/1"),
    (INSTRUMENT_ID, VERSION_ID, "sec:2/1"),
]


@pytest.mark.parametrize(
    "instrument_id,version_id,clause_path",
    COLON_IN_SEGMENT_CASES,
    ids=["instrument_id", "version_id", "clause_path"],
)
def test_colon_is_rejected_in_every_segment(
    instrument_id: str, version_id: str, clause_path: str
) -> None:
    with pytest.raises(ValueError):
        stable_id(instrument_id, version_id, clause_path)


# parse_stable_id splits on ':' into exactly three parts (CONTEXT.md: Clause).
@pytest.mark.parametrize(
    "malformed",
    ["acme:v1", "acme:v1:sec1:extra"],
    ids=["two_parts", "four_parts"],
)
def test_parse_stable_id_rejects_the_wrong_part_count(malformed: str) -> None:
    with pytest.raises(ValueError, match="not a stable clause ID"):
        parse_stable_id(malformed)


# `$` matches before a trailing newline; the grammar ends at the string (#234).
@pytest.mark.parametrize("suffix", ["\n", "\r\n", "\nx"], ids=["lf", "crlf", "lf-then-text"])
def test_a_line_break_after_the_id_is_not_part_of_the_grammar(suffix: str) -> None:
    built = stable_id(INSTRUMENT_ID, VERSION_ID, "s6da/2")
    with pytest.raises(ValueError, match="not a stable clause ID"):
        parse_stable_id(built + suffix)
    for pattern, whole in (
        (STABLE_ID_RE, built),
        (INSTRUMENT_ID_RE, INSTRUMENT_ID),
        (VERSION_ID_RE, VERSION_ID),
        (CLAUSE_PATH_RE, "s6da/2"),
    ):
        assert pattern.match(whole) is not None
        assert pattern.match(whole + suffix) is None
    with pytest.raises(ValueError):
        stable_id(INSTRUMENT_ID, VERSION_ID, "s6da/2" + suffix)
    with pytest.raises(ValueError):
        stable_id(INSTRUMENT_ID, VERSION_ID + suffix, "s6da/2")
    with pytest.raises(ValueError):
        stable_id(INSTRUMENT_ID + suffix, VERSION_ID, "s6da/2")
