"""Clause paths for s6's flat, unnumbered Definition paragraphs (`CONTEXT.md`:
Definition, issue #9 follow-up).

s6 ("Interpretation") has no subsection/paragraph structure below its single
`(1)` — just ~hundreds of flat `Definition` paragraphs, alphabetically
ordered. A positional index (`s6/1/1`, `s6/1/2`, ...) would silently
repoint an already-issued citation whenever a later compilation inserts a
new term alphabetically before an existing one — exactly the failure mode a
stable ID exists to prevent. Each definition's `clause_path` segment is
instead a slug of the term itself (`s6/1/organisation`), which only changes
if the term is renamed.

Real terms below are read from the checked-in spike fixture
(docs/research/fixtures/privacy-act-1988-C2026C00227.excerpt.html), not
hand-picked strings, so this test exercises the algorithm against actual
legislative text.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from compliance_rag.corpus.models import (
    assert_unique_definition_slugs,
    definition_slug,
    parse_stable_id,
    stable_id,
)

FIXTURE = (
    Path(__file__).parent.parent
    / "docs"
    / "research"
    / "fixtures"
    / "privacy-act-1988-C2026C00227.excerpt.html"
)

_DEFINITION_TERM_RE = re.compile(
    r'<p class="Definition"><span style="font-weight:bold; font-style:italic">([^<]+?)\s*</span>'
    # Scaffolding to get real terms into this test, not a reference
    # implementation for issue #17's parser: it only demands the
    # bold-italic term span sit first inside <p class="Definition">, which
    # holds for the ten terms below but isn't validated against every case
    # the parser will meet. definition_slug()'s own contract is the thing
    # #17 must satisfy — not agreement with this regex.
)


def _real_s6_terms() -> list[str]:
    """The defined terms in the checked-in fixture's s6 excerpt, in
    document order."""
    html = FIXTURE.read_text(encoding="utf-8")
    return _DEFINITION_TERM_RE.findall(html)


REAL_TERM_CASES: list[tuple[str, str]] = [
    ("ACC", "acc"),
    ("access seeker", "access-seeker"),
    ("ACT enactment", "act-enactment"),
    ("advice related functions", "advice-related-functions"),
    ("affected information recipient", "affected-information-recipient"),
    ("agency", "agency"),
    ("alternative complaint body", "alternative-complaint-body"),
    ("amount of credit", "amount-of-credit"),
    ("annual turnover", "annual-turnover"),
    ("APP code", "app-code"),
]


def test_fixture_actually_contains_these_terms() -> None:
    """Guards the table above against the fixture changing under it."""
    found = _real_s6_terms()
    for term, _ in REAL_TERM_CASES:
        assert term in found, f"{term!r} not found in fixture; found {found}"


@pytest.mark.parametrize("term,expected_slug", REAL_TERM_CASES, ids=[c[0] for c in REAL_TERM_CASES])
def test_real_s6_term_slugs(term: str, expected_slug: str) -> None:
    assert definition_slug(term) == expected_slug


def test_slugging_is_deterministic_and_pure() -> None:
    """Same term, same slug — no counters, no dependence on what else has
    been slugged."""
    assert definition_slug("organisation") == definition_slug("organisation")
    assert_unique_definition_slugs(["organisation"])  # does not affect the above
    assert definition_slug("organisation") == definition_slug("organisation")


def test_all_real_s6_terms_slug_without_collision() -> None:
    assert_unique_definition_slugs(_real_s6_terms())  # must not raise


def test_duplicate_slug_is_a_hard_parse_failure() -> None:
    """Two distinct terms slugging to the same value stops the build — never
    a silently suffixed '-2', which would be assigned by parse order and
    reintroduce the exact instability the slug scheme exists to avoid."""
    with pytest.raises(ValueError, match="duplicate"):
        assert_unique_definition_slugs(["annual turnover", "Annual-Turnover"])


def test_a_definition_clause_path_round_trips() -> None:
    """`s6/1/organisation`: the section identifier is the first clause_path
    segment, exactly as it already is for `s6c/1/a` (CONTEXT.md: Clause
    path) — a definition's clause_path is the same shape, not a new one."""
    slug = definition_slug("organisation")
    clause_path = f"s6/1/{slug}"
    built = stable_id("privacy-act-1988", "TEST-FIXTURE-001", clause_path)
    assert built == "privacy-act-1988:TEST-FIXTURE-001:s6/1/organisation"
    assert parse_stable_id(built) == ("privacy-act-1988", "TEST-FIXTURE-001", "s6/1/organisation")
