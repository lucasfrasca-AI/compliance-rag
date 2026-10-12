"""The parser on the whole Privacy Act compilation, not only the excerpt
(issue #97).

`privacy-act-1988-C2026C00227.constructs.html` is cut verbatim from the full
C2026C00227 compilation (sha256 below, fetched 2026-10-11 from the Register's
unpacked ePub): every section whose markup broke the excerpt-trained parser,
plus s6D/s6DA/s6E/s6EA — the small business exemption #93 is built on. Each
test names the construct it pins.

`test_the_whole_compilation_parses` runs over the full 2.3 MB file when
`PRIVACY_ACT_COMPILATION_HTML` points at a copy whose sha256 matches; CI
doesn't download it.
"""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from pathlib import Path

import pytest

from compliance_rag.corpus.models import ClauseRecord
from compliance_rag.corpus.parser import parse_compilation

FIXTURES = Path(__file__).resolve().parents[1] / "docs" / "research" / "fixtures"
CONSTRUCTS = FIXTURES / "privacy-act-1988-C2026C00227.constructs.html"
FULL_SHA256 = "6d0a43e5da103d1eeb5317ae9ac213c5b51acab2357dc01b981a91e1052c5c3b"


def _parse(html: str) -> dict[str, ClauseRecord]:
    parsed = parse_compilation(html, instrument_id="privacy-act-1988", version_id="C2026C00227")
    return {c.clause_path: c for c in parsed.clauses}


@pytest.fixture(scope="module")
def clauses() -> dict[str, ClauseRecord]:
    return _parse(CONSTRUCTS.read_text(encoding="utf-8"))


def test_an_unnumbered_section_is_its_own_clause(clauses: dict[str, ClauseRecord]) -> None:
    """s1 and s15 are single sentences with no (1): their path is the section."""
    assert clauses["s1"].text == "This Act may be cited as the Privacy Act 1988."
    assert clauses["s15"].text.startswith("An APP entity must not do an act")


def test_a_section_that_mentions_the_apps_is_not_an_app(
    clauses: dict[str, ClauseRecord],
) -> None:
    """Only "N Australian Privacy Principle N—..." is an APP. s6A, s14 and
    s15 mention the APPs and stay in the main body."""
    assert {"s6a/1", "s14/1", "s15"} <= set(clauses)
    apps = {p.split("/")[1] for p in clauses if p.startswith("sch1/")}
    assert apps == {"app11"}


VERBATIM = {
    "s6d/4/a": (
        "carries on a business that has had an annual turnover of more than $3,000,000 for "
        "a financial year that has ended after the later of the following:"
    ),
    "s6d/4/b": (
        "provides a health service to another individual and holds any health information "
        "except in an employee record; or"
    ),
    "s6d/4/c": (
        "discloses personal information about another individual to anyone else for a "
        "benefit, service or advantage; or"
    ),
    "s6d/4/d": (
        "provides a benefit, service or advantage to collect personal information about "
        "another individual from anyone else; or"
    ),
    "s6d/4/e": (
        "is a contracted service provider for a Commonwealth contract (whether or not a "
        "party to the contract); or"
    ),
    "s6d/4/f": "is a credit reporting body.",
    "s6e/1b": (
        "If a small business operator is the protected action ballot agent for a protected "
        "action ballot conducted under Part 3\u20113 of the Fair Work Act 2009, this Act "
        "applies, with the prescribed modifications (if any), in relation to the activities "
        "carried on by the small business operator for the purpose of, or in connection "
        "with, the conduct of the protected action ballot, as if the small business operator "
        "were an organisation."
    ),
    "s6e/1c": (
        "If a small business operator is an association of employees that is registered or "
        "recognised under the Fair Work (Registered Organisations) Act 2009, this Act "
        "applies, with the prescribed modifications (if any), in relation to the activities "
        "carried on by the small business operator, as if the small business operator were "
        "an organisation (within the meaning of this Act)."
    ),
}


@pytest.mark.parametrize("path", sorted(VERBATIM))
def test_inserted_and_exemption_provisions_round_trip_verbatim(
    clauses: dict[str, ClauseRecord], path: str
) -> None:
    """s6D(4)(a)-(f) and s6E(1B)/(1C): the words #93's model cites, exactly."""
    assert clauses[path].text == VERBATIM[path]


@pytest.mark.parametrize(
    ("path", "starts", "ends"),
    [
        ("s6e/1a", "If a small business operator is a reporting entity", "were an organisation."),
        ("s6e/1d", "If a small business operator holds an accreditation", "were an organisation."),
        ("s50/2/a/iia", "to the Ombudsman under a law in force in an external Territory", "or"),
        ("s50/2/a/ivaa", "to the National Student Ombudsman", "or"),
    ],
)
def test_inserted_provisions_keep_their_suffix(
    clauses: dict[str, ClauseRecord], path: str, starts: str, ends: str
) -> None:
    """(1A) and (iia) are inserted provisions, not renumbered ones: the
    suffix is part of the path, lowercased. (s6E(1A) and (1D) close with their
    tail line "as if the small business operator were an organisation".)"""
    assert clauses[path].text.startswith(starts) and clauses[path].text.endswith(ends)


def test_the_constructs_section_counts(clauses: dict[str, ClauseRecord]) -> None:
    counts = Counter(p.split("/")[0] for p in clauses)
    assert {s: counts[s] for s in ("s6d", "s6da", "s6e", "s6ea")} == {
        "s6d": 27,
        "s6da": 9,
        "s6e": 14,
        "s6ea": 8,
    }


def test_a_tokenless_paragraph_continues_the_one_above(
    clauses: dict[str, ClauseRecord],
) -> None:
    """s6FA(a)'s closing words, and s26WG(h)'s text between its
    subparagraphs (ii) and (iii) — which keep numbering under (h)."""
    assert clauses["s6fa/a"].text.endswith("that is also personal information;")
    assert "the likelihood that the persons" in clauses["s26wg/h"].text
    assert {"s26wg/h/i", "s26wg/h/ii", "s26wg/h/iii", "s26wg/h/iv"} <= set(clauses)


def test_a_term_split_across_spans_is_read_whole(clauses: dict[str, ClauseRecord]) -> None:
    """Word splits "de-identified" (a non-breaking hyphen in the source) and
    "de facto partner" into several bold spans; both used to slug to `de`."""
    assert clauses["s6/1/de-identified"].text.startswith("de")
    assert clauses["s6/1/de-facto-partner"].text.startswith("de facto partner")


def test_the_small_business_exemption_round_trips_verbatim(
    clauses: dict[str, ClauseRecord],
) -> None:
    """The text #93's design rests on (docs/research/graph-ontology-options.md)."""
    assert clauses["s6d/1"].text == (
        "A business is a small business at a time (the test time) in a financial year "
        "(the current year) if its annual turnover for the previous financial year is "
        "$3,000,000 or less."
    )
    assert [p for p in clauses if p.startswith("s6d/4/") and p.count("/") == 2] == [
        f"s6d/4/{x}" for x in "abcdef"
    ]
    assert clauses["s6d/4/f"].text == "is a credit reporting body."
    assert clauses["s6d/9"].text.startswith("Despite subsection (3), a body corporate")
    assert clauses["s6da/1/g"].text == "other operating income."
    assert clauses["s6ea/1"].text.startswith("This Act applies in relation to a small")


def test_two_provisions_on_one_path_are_refused() -> None:
    """A stable ID is what a citation carries (CONTEXT.md: Instrument id): the
    parser refuses to emit one path twice rather than collide silently."""
    section = (
        '<p class="ActHead5"><span class="CharSectno">99</span> Test</p>'
        '<p class="subsection">One sentence.</p><p class="subsection">Another.</p>'
    )
    with pytest.raises(ValueError, match="produced twice"):
        _parse(section)


@pytest.mark.skipif(
    not os.environ.get("PRIVACY_ACT_COMPILATION_HTML"),
    reason="PRIVACY_ACT_COMPILATION_HTML not set — the full 2.3 MB compilation",
)
def test_the_whole_compilation_parses() -> None:
    raw = Path(os.environ["PRIVACY_ACT_COMPILATION_HTML"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FULL_SHA256, "a different compilation file"
    clauses = _parse(raw.decode("utf-8"))
    assert len(clauses) == 4114
    counts = Counter(p.split("/")[0] for p in clauses)
    assert {s: counts[s] for s in ("s6", "s6c", "s6d", "s6da", "s6e", "s6ea", "sch1")} == {
        "s6": 451,
        "s6c": 33,
        "s6d": 27,
        "s6da": 9,
        "s6e": 14,
        "s6ea": 8,
        "sch1": 236,
    }
    apps = {p.split("/")[1] for p in clauses if p.startswith("sch1/")}
    assert apps == {f"app{n}" for n in range(1, 14)}
    # A heading set in subsection style is not a clause (s80U).
    assert "s80u" not in clauses and "s80u/1" in clauses
    # Term and body joined as the source reads.
    assert clauses["s6/1/breach"].text.startswith("breach:")
