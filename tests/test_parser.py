"""The law parser: compilation markup to clause records (issue #17).

Pure function over markup — no network, no storage (`PHASES.md`: parser
deliverable). Tested against the real, byte-verbatim fixture from issue #9's
ePub format spike (`docs/research/fixtures/privacy-act-1988-C2026C00227.excerpt.html`),
which carries three real excerpts: section 6C (main-body pattern — headings,
subsections, a single Definition, lettered/roman-numeral paragraphs,
subsection2), APP 10-11 (Schedule 1 pattern — dot-numbered subclauses), and
the first ten Definition paragraphs of s6 (the flat, unnumbered pattern that
needs `definition_slug`).

The spike's central finding — `CharSectno="6"` repeats across main-body s6,
Schedule 1's APP 6, and a hypothetical Schedule 2's own s6, so the parser
needs container tracking, not a flat class -> clause_path mapping — can't be
exercised against Schedule 2 with real bytes: the fixture (deliberately, to
stay byte-verbatim) never includes a second Schedule. `test_schedule_2...`
below uses a small synthetic snippet, shaped per the spike's documented
class table (`ActHead1`/`CharChapNo`), to prove the container-tracking
mechanism itself; every other test in this file is real fixture bytes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from compliance_rag.corpus.models import (
    CANONICALISATION_VERSION,
    ClauseRecord,
    canonicalise,
    clause_anchor_id,
)
from compliance_rag.corpus.parser import ParsedCompilation, parse_compilation

FIXTURE = (
    Path(__file__).parent.parent
    / "docs"
    / "research"
    / "fixtures"
    / "privacy-act-1988-C2026C00227.excerpt.html"
)
INSTRUMENT_ID = "privacy-act-1988"
VERSION_ID = "C2026C00227"


@pytest.fixture(scope="module")
def fixture_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def parsed(fixture_html: str) -> ParsedCompilation:
    return parse_compilation(fixture_html, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID)


def _by_path(result: ParsedCompilation, clause_path: str) -> ClauseRecord:
    matches = [c for c in result.clauses if c.clause_path == clause_path]
    assert len(matches) == 1, f"expected exactly one clause at {clause_path!r}, found {matches}"
    return matches[0]


# --------------------------------------------------------------------------
# Pure function, no network/storage — importing and calling it is the proof;
# nothing in `parser.py` touches an adapter or the filesystem.
# --------------------------------------------------------------------------


def test_is_a_pure_function_of_its_html_argument(fixture_html: str) -> None:
    first = parse_compilation(fixture_html, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID)
    second = parse_compilation(fixture_html, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID)
    assert first.clauses == second.clauses
    assert first.annotated_html == second.annotated_html


# --------------------------------------------------------------------------
# s6C: main-body pattern — ActHead5 / subsection / a single Definition /
# paragraph / paragraphsub / subsection2.
# --------------------------------------------------------------------------


def test_s6c_subsection_1_merges_its_singleton_definition(parsed: ParsedCompilation) -> None:
    """Only one Definition ('organisation') sits under s6C's subsection (1),
    so per CONTEXT.md's pre-existing `s6c/1/a` example, it does not get its
    own clause_path segment: subsection (1) is itself the clause, and its
    text folds in the definition's own lead-in plus the subsection2 tail
    that grammatically completes the same sentence."""
    clause = _by_path(parsed, "s6c/1")
    assert "In this Act" in clause.text
    assert "organisation means" in clause.text
    assert "that is not a small business operator" in clause.text
    # Order, not just presence: the source reads chapeau -> "organisation
    # means:" -> (a)-(e), separate clauses -> the subsection2 tail. A merge
    # that appends parts in the wrong order (e.g. the tail landing before
    # "organisation means:") would still pass substring-only assertions
    # while silently breaking verbatim-ness.
    assert clause.text.index("In this Act") < clause.text.index("organisation means")
    assert clause.text.index("organisation means") < clause.text.index(
        "that is not a small business operator"
    )
    assert clause.canonicalisation_version == CANONICALISATION_VERSION
    assert clause.canonical_text == canonicalise(clause.text)


@pytest.mark.parametrize(
    "clause_path,expected_substring",
    [
        ("s6c/1/a", "an individual"),
        ("s6c/1/b", "a body corporate"),
        ("s6c/1/c", "a partnership"),
        ("s6c/1/d", "any other unincorporated association"),
        ("s6c/1/e", "a trust"),
    ],
)
def test_s6c_subsection_1_paragraphs_attach_directly_to_the_subsection(
    parsed: ParsedCompilation, clause_path: str, expected_substring: str
) -> None:
    """No 's6c/1/organisation/a' — the singleton definition contributes no
    path segment, so its paragraphs are children of the subsection."""
    assert expected_substring in _by_path(parsed, clause_path).text


def test_s6c_subsection_3_has_its_own_singleton_definition(parsed: ParsedCompilation) -> None:
    """'State or Territory authority', subsection (3) — the same singleton
    pattern recurring later in the same section, proving it's not special-
    cased to subsection (1)."""
    clause = _by_path(parsed, "s6c/3")
    assert "State or Territory authority means" in clause.text
    assert _by_path(parsed, "s6c/3/a").text.strip().startswith("a State or Territory Minister")


def test_paragraphsub_nests_under_its_own_paragraph_not_the_subsection(
    parsed: ParsedCompilation,
) -> None:
    """s6C/3(c)'s roman-numeral carve-outs (i)/(ii) are children of
    paragraph (c) specifically, not of subsection (3) directly — proven by
    paragraph (d), a sibling of (c), existing at the same depth as (c)
    rather than nested under it."""
    assert "incorporated company" in _by_path(parsed, "s6c/3/c/i").text
    assert "association of employers" in _by_path(parsed, "s6c/3/c/ii").text
    assert _by_path(parsed, "s6c/3/d").text.strip().startswith("a body established")


def test_subsection_2_own_text(parsed: ParsedCompilation) -> None:
    clause = _by_path(parsed, "s6c/2")
    assert "A legal person can have a number of different capacities" in clause.text


def test_subsection_4_and_5_are_not_dropped(parsed: ParsedCompilation) -> None:
    """The section keeps going after (3)'s nested paragraphs close — proves
    the stack correctly pops back to the subsection level rather than
    getting stuck inside (3)'s children."""
    assert "Before the Governor" in _by_path(parsed, "s6c/4").text
    assert "does not include the Australian Capital Territory" in _by_path(parsed, "s6c/5").text


def test_subsection_head_and_notetext_are_skipped(parsed: ParsedCompilation) -> None:
    """'SubsectionHead' (informal prose sub-heading) and 'notetext' (notes,
    not operative text) never become clauses and never leak into a clause's
    text."""
    all_text = " ".join(c.text for c in parsed.clauses)
    assert "What is an" not in all_text  # SubsectionHead prose
    assert "187LA" not in all_text  # notetext content


def test_no_clause_is_emitted_for_the_section_heading_itself(parsed: ParsedCompilation) -> None:
    """'s6c' alone (no subsection) is a container, not the smallest citable
    unit (`CONTEXT.md`: Clause) — every clause in this file has a subsection
    segment or deeper."""
    assert all(c.clause_path != "s6c" for c in parsed.clauses)


# --------------------------------------------------------------------------
# APP 10-11: Schedule 1 pattern — dot-numbered subclauses, container
# tracking distinguishing "app" from "s".
# --------------------------------------------------------------------------


def test_app_subclauses_use_the_schedule_1_app_convention(parsed: ParsedCompilation) -> None:
    """`CONTEXT.md`'s own example: `sch1/app11/1`. The dot-number's segment
    is just the part after the dot — the APP number is already in `app11`."""
    clause = _by_path(parsed, "sch1/app11/1")
    assert "take such steps as are reasonable" in clause.text
    assert _by_path(parsed, "sch1/app10/1").text.startswith("An APP entity")


def test_app_11_paragraphs_and_the_subsection2_tail(parsed: ParsedCompilation) -> None:
    assert "misuse, interference and loss" in _by_path(parsed, "sch1/app11/1/a").text
    clause = _by_path(parsed, "sch1/app11/2")
    assert clause.text.strip().startswith("If:")
    assert "destroy the information" in clause.text  # subsection2 tail merged in
    assert "such steps include technical" in _by_path(parsed, "sch1/app11/3").text


def test_app_11_2_tail_follows_its_chapeau_in_canonical_text(parsed: ParsedCompilation) -> None:
    """11.2 has 6C(1)'s shape — chapeau, lettered conditions, subsection2 tail
    — the shape the word-order bug lived in. `startswith`/`in` can't tell a
    tail placed before the chapeau from one after it; the exact canonical
    text can. The conditions are their own clauses, not part of 11.2's text."""
    clause = _by_path(parsed, "sch1/app11/2")
    assert clause.canonical_text == (
        "If: the entity must take such steps as are reasonable in the circumstances "
        "to destroy the information or to ensure that the information is de-identified."
    )
    for letter, opening in (
        ("a", "an APP entity holds personal information"),
        ("b", "the entity no longer needs the information"),
        ("c", "the information is not contained in a Commonwealth record"),
        ("d", "the entity is not required by or under an Australian law"),
    ):
        assert _by_path(parsed, f"sch1/app11/2/{letter}").canonical_text.startswith(opening)


def test_part_heading_after_app_11_closes_it_without_becoming_a_clause(
    parsed: ParsedCompilation,
) -> None:
    """`ActHead2` (Part 5) follows APP 11 in the fixture — proves a Part
    boundary correctly closes the open subsection rather than swallowing it,
    and Part itself never appears in a clause_path (`CONTEXT.md`: Clause
    path lists no Part segment in any of its examples)."""
    assert all("part" not in c.clause_path.lower() for c in parsed.clauses)
    assert all("Access to, and correction of" not in c.text for c in parsed.clauses)


# --------------------------------------------------------------------------
# s6: the flat Definition list — the reason definition_slug exists.
# --------------------------------------------------------------------------


REAL_S6_TERM_SLUGS: list[tuple[str, str]] = [
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


@pytest.mark.parametrize("term,slug", REAL_S6_TERM_SLUGS, ids=[t for t, _ in REAL_S6_TERM_SLUGS])
def test_s6_definitions_get_a_term_slug_path(
    parsed: ParsedCompilation, term: str, slug: str
) -> None:
    """s6/1 has ten-plus flat Definition siblings under the same bare '(1)'
    — per `CONTEXT.md`'s Definition entry, each needs its own term-slug
    segment, unlike s6C's singleton case."""
    clause_path = f"s6/1/{slug}"
    clause = _by_path(parsed, clause_path)
    assert clause.text  # every real term produced non-empty text


def test_s6_definition_with_its_own_paragraph_list(parsed: ParsedCompilation) -> None:
    """'affected information recipient' has its own lettered breakdown
    (a)-(e) — these must nest under ITS term-slug segment, not directly
    under 's6/1', since a sibling definition ('agency') also has an (a)."""
    clause = _by_path(parsed, "s6/1/affected-information-recipient/a")
    assert "mortgage insurer" in clause.text
    agency_a = _by_path(parsed, "s6/1/agency/a")
    assert "a Minister" in agency_a.text
    assert clause.clause_id != agency_a.clause_id  # the exact collision definition_slug prevents


def test_s6_subsection_1_chapeau_is_its_own_clause_not_merged_into_any_definition(
    parsed: ParsedCompilation,
) -> None:
    """Unlike s6C's singleton case, s6/1 has many Definition siblings, so
    the bare chapeau ('In this Act, unless the contrary intention
    appears:') stays its own clause rather than merging into one of them."""
    clause = _by_path(parsed, "s6/1")
    assert "unless the contrary intention appears" in clause.text
    assert "means the Australian Crime Commission" not in clause.text


def test_a_duplicate_definition_slug_in_one_section_is_a_hard_parse_failure() -> None:
    """`assert_unique_definition_slugs` (issue #9 follow-up): a real
    collision stops the parse rather than silently colliding two clauses on
    one clause_path."""
    html = """<html><body>
<p class="ActHead5"><span class="CharSectno">6</span><span> Interpretation</span></p>
<p class="subsection"><span>(1)</span><span> In this Act:</span></p>
<p class="Definition"><span style="font-weight:bold">annual turnover</span>
<span> means X.</span></p>
<p class="Definition"><span style="font-weight:bold">Annual-Turnover</span>
<span> means Y.</span></p>
</body></html>"""
    with pytest.raises(ValueError, match="duplicate"):
        parse_compilation(html, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID)


# --------------------------------------------------------------------------
# Words after a Definition's paragraphs (issue #178, ADR-0039). The real
# shapes (record, credit reporting body, ...) are pinned on the constructs
# fixture in test_parser_full_compilation.py; these are the edges no real
# fixture holds, shaped per the same class table, so they are synthetic.
# --------------------------------------------------------------------------


def test_words_inside_a_definition_never_reach_the_next_definition_or_the_chapeau() -> None:
    """A connector before a sub-paragraph leads it, closing words after the
    sub-list close the definition, and the definition after it (and the
    chapeau) are untouched."""
    html = """<html><body>
<p class="ActHead5"><span class="CharSectno">6</span><span> Interpretation</span></p>
<p class="subsection"><span>(1)</span><span> In this Act:</span></p>
<p class="Definition"><span style="font-weight:bold">alpha</span><span> means:</span></p>
<p class="paragraph"><span>(a)</span><span> one;</span></p>
<p class="paragraph"><span>(b)</span><span> two:</span></p>
<p class="paragraphsub"><span>(i)</span><span> red; or</span></p>
<p class="subsection2"><span>but not:</span></p>
<p class="paragraphsub"><span>(ii)</span><span> blue;</span></p>
<p class="subsection2"><span>in the case of a person.</span></p>
<p class="Definition"><span style="font-weight:bold">beta</span><span> means a thing.</span></p>
</body></html>"""
    result = parse_compilation(html, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID)
    texts = {c.clause_path: c.text for c in result.clauses}
    assert texts == {
        "s6/1": "In this Act:",
        "s6/1/alpha": "alpha means: in the case of a person.",
        "s6/1/alpha/a": "one;",
        "s6/1/alpha/b": "two:",
        "s6/1/alpha/b/i": "red; or",
        "s6/1/alpha/b/ii": "but not: blue;",
        "s6/1/beta": "beta means a thing.",
    }


def test_a_connector_in_a_singleton_definition_leads_its_paragraph_too() -> None:
    """A subsection with one Definition folds it into the subsection's own
    clause (s6C(1)); a connector inside it still leads the paragraph after it,
    and the paragraphs are children of the subsection."""
    html = """<html><body>
<p class="ActHead5"><span class="CharSectno">6</span><span> Interpretation</span></p>
<p class="subsection"><span>(2)</span><span> In this section:</span></p>
<p class="Definition"><span style="font-weight:bold">gamma</span><span> includes:</span></p>
<p class="paragraph"><span>(a)</span><span> x;</span></p>
<p class="subsection2"><span>but does not include:</span></p>
<p class="paragraph"><span>(c)</span><span> y.</span></p>
</body></html>"""
    result = parse_compilation(html, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID)
    assert {c.clause_path: c.text for c in result.clauses} == {
        "s6/2": "In this section: gamma includes:",
        "s6/2/a": "x;",
        "s6/2/c": "but does not include: y.",
    }


# --------------------------------------------------------------------------
# Words between and after the paragraphs of an ordinary subsection (issue
# #186, ADR-0039). The 21 real connectors are pinned on the constructs fixture
# in test_parser_full_compilation.py; these are the edges no real fixture
# holds, shaped per the same class table, so they are synthetic.
# --------------------------------------------------------------------------


def _texts(body: str) -> dict[str, str]:
    html = f"""<html><body>
<p class="ActHead5"><span class="CharSectno">6</span><span> Interpretation</span></p>
{body}
</body></html>"""
    result = parse_compilation(html, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID)
    return {c.clause_path: c.text for c in result.clauses}


def test_a_connector_leads_a_subparagraph_and_closing_words_close_the_subsection() -> None:
    """A connector between two sub-paragraphs leads the second, and the words
    after the last close the subsection, ahead of its paragraphs (as s8(2)'s do)."""
    assert _texts(
        """<p class="subsection"><span>(1)</span><span> If:</span></p>
<p class="paragraph"><span>(a)</span><span> one:</span></p>
<p class="paragraphsub"><span>(i)</span><span> red; or</span></p>
<p class="subsection2"><span>but not:</span></p>
<p class="paragraphsub"><span>(ii)</span><span> blue;</span></p>
<p class="subsection2"><span>then it applies.</span></p>"""
    ) == {
        "s6/1": "If: then it applies.",
        "s6/1/a": "one:",
        "s6/1/a/i": "red; or",
        "s6/1/a/ii": "but not: blue;",
    }


def test_words_waiting_for_a_paragraph_never_reach_the_next_subsection() -> None:
    """Closing words wait to see whether a paragraph follows. The next
    subsection's first paragraph is not that paragraph."""
    assert _texts(
        """<p class="subsection"><span>(1)</span><span> If:</span></p>
<p class="paragraph"><span>(a)</span><span> one;</span></p>
<p class="subsection2"><span>then X applies.</span></p>
<p class="subsection"><span>(2)</span><span> If:</span></p>
<p class="paragraph"><span>(a)</span><span> two;</span></p>"""
    ) == {
        "s6/1": "If: then X applies.",
        "s6/1/a": "one;",
        "s6/2": "If:",
        "s6/2/a": "two;",
    }


def test_closing_words_stay_ahead_of_a_definition_that_follows_them() -> None:
    """Outside a Definition nothing changes (ADR-0039): words that closed a list
    before a singleton Definition still come before it, in the subsection's text."""
    assert _texts(
        """<p class="subsection"><span>(2)</span><span> If:</span></p>
<p class="paragraph"><span>(a)</span><span> x;</span></p>
<p class="subsection2"><span>then:</span></p>
<p class="Definition"><span style="font-weight:bold">gamma</span><span> means a thing.</span></p>"""
    ) == {"s6/2": "If: then: gamma means a thing.", "s6/2/a": "x;"}


def test_a_connector_held_across_a_tokenless_paragraph_is_not_lost() -> None:
    """Not modelled (ADR-0039): a tokenless paragraph continues the one above,
    and the connector waiting before it leads the next paragraph that opens. No
    real fixture has the shape; what must hold is that the words are stored once."""
    texts = _texts(
        """<p class="subsection"><span>(1)</span><span> If:</span></p>
<p class="paragraph"><span>(a)</span><span> one;</span></p>
<p class="subsection2"><span>then:</span></p>
<p class="paragraph"><span>continues one;</span></p>
<p class="paragraph"><span>(b)</span><span> two.</span></p>"""
    )
    assert sorted(" ".join(texts.values()).split()) == sorted(
        ["If:", "one;", "then:", "continues", "one;", "two."]
    )


# --------------------------------------------------------------------------
# Container tracking: the spike's central finding. Synthetic snippet, since
# the real fixture (deliberately byte-verbatim) never includes a second
# Schedule — shaped per the spike's documented ActHead1/CharChapNo pattern.
# --------------------------------------------------------------------------


def test_schedule_2_own_s6_does_not_collide_with_main_body_s6() -> None:
    """`CharSectno="6"` repeats across main-body s6 and a Schedule 2 with
    its own independent numbering (spike finding, docs/research/
    legislation-gov-au-format.md). The container stack must produce two
    distinct clause paths, not one clause silently overwriting the other."""
    html = """<html><body>
<p class="ActHead5"><span class="CharSectno">6</span><span> Interpretation</span></p>
<p class="subsection"><span>(1)</span><span> Main body meaning.</span></p>
<p class="ActHead1"><span class="CharChapNo">2</span>
<span class="CharChapText">Schedule text</span></p>
<p class="ActHead5"><span class="CharSectno">6</span><span> Interpretation</span></p>
<p class="subsection"><span>(1)</span><span> Schedule 2 meaning.</span></p>
</body></html>"""
    result = parse_compilation(html, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID)
    main_body = _by_path(result, "s6/1")
    schedule_2 = _by_path(result, "sch2/s6/1")
    assert main_body.text.strip() == "Main body meaning."
    assert schedule_2.text.strip() == "Schedule 2 meaning."
    assert main_body.clause_id != schedule_2.clause_id


def test_charsectno_6_three_way_collision_from_the_spike_report() -> None:
    """The exact collision issue #17's re-planning note names: `CharSectno`
    `"6"` appears in the main body, inside Schedule 1's own APP numbering
    (APP 6, not just any APP), and in a Schedule 2 with its own independent
    section 6 — three clauses that would collide to the same path under a
    flat class -> clause_path mapping, and must not collide here."""
    html = """<html><body>
<p class="ActHead5"><span class="CharSectno">6</span><span> Interpretation</span></p>
<p class="subsection"><span>(1)</span><span> Main body s6.</span></p>
<p class="ActHead1"><span class="CharChapNo">1</span>
<span class="CharChapText">Australian Privacy Principles</span></p>
<p class="ActHead5"><span class="CharSectno">6</span>
<span> Australian Privacy Principle 6&#8212;use or disclosure</span></p>
<p class="subsection"><span>6.1</span><span> APP 6.</span></p>
<p class="ActHead1"><span class="CharChapNo">2</span>
<span class="CharChapText">Schedule 2</span></p>
<p class="ActHead5"><span class="CharSectno">6</span><span> Interpretation</span></p>
<p class="subsection"><span>(1)</span><span> Schedule 2 s6.</span></p>
</body></html>"""
    result = parse_compilation(html, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID)
    main_body = _by_path(result, "s6/1")
    app_6 = _by_path(result, "sch1/app6/1")
    schedule_2 = _by_path(result, "sch2/s6/1")
    assert {main_body.text.strip(), app_6.text.strip(), schedule_2.text.strip()} == {
        "Main body s6.",
        "APP 6.",
        "Schedule 2 s6.",
    }
    assert len({main_body.clause_id, app_6.clause_id, schedule_2.clause_id}) == 3


# --------------------------------------------------------------------------
# Click-to-clause: the anchor is written into the returned HTML, not
# invented at render time (ADR-0019).
# --------------------------------------------------------------------------


def test_every_clause_anchor_is_injected_into_the_returned_html(parsed: ParsedCompilation) -> None:
    for clause in parsed.clauses:
        expected_id = clause_anchor_id(clause.clause_path)
        assert f'id="{expected_id}"' in parsed.annotated_html, (
            f"no injected anchor for {clause.clause_path!r}"
        )


def test_annotated_html_still_contains_the_original_text(
    parsed: ParsedCompilation, fixture_html: str
) -> None:
    """Injection adds an attribute; it must not otherwise rewrite the
    document's own text content."""
    assert "an individual; or" in parsed.annotated_html
    assert len(parsed.annotated_html) >= len(fixture_html)


# --------------------------------------------------------------------------
# Canonicalisation
# --------------------------------------------------------------------------


def test_every_clause_carries_the_current_canonicalisation_version(
    parsed: ParsedCompilation,
) -> None:
    assert parsed.clauses
    for clause in parsed.clauses:
        assert clause.canonicalisation_version == CANONICALISATION_VERSION
        assert clause.canonical_text == canonicalise(clause.text)


def test_every_clause_has_the_given_instrument_and_version(parsed: ParsedCompilation) -> None:
    for clause in parsed.clauses:
        assert clause.instrument_id == INSTRUMENT_ID
        assert clause.version_id == VERSION_ID
