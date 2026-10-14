"""The parser on the whole Privacy Act compilation, not only the excerpt
(issue #97).

`privacy-act-1988-C2026C00227.constructs.html` is cut verbatim from the full
C2026C00227 compilation (sha256 below, fetched 2026-10-11 from the Register's
unpacked ePub): every section whose markup broke the excerpt-trained parser,
plus s6D/s6DA/s6E/s6EA — the small business exemption #93 is built on. Each
test names the construct it pins.

Issue #178 added four s6(1) definitions whose words follow their paragraph
list (*record*, *credit reporting body*, *Commonwealth officer*,
*court/tribunal order*) and s8(2), a non-definition list with closing words —
the shapes the excerpt never held, so the parser had never been asked about
them. Issue #186 added every other section that has a connector between two
paragraph groups outside a definition (s7(1) and 14 more sections, 21
connectors), plus APP 7 and Schedule 2 so that both of the Act's schedules are
represented.

`test_the_whole_compilation_parses` runs over the full 2.3 MB file when
`PRIVACY_ACT_COMPILATION_HTML` points at a copy whose sha256 matches; CI
doesn't download it.
"""

from __future__ import annotations

import hashlib
import html as html_module
import os
import re
from collections import Counter
from pathlib import Path
from typing import NamedTuple

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
    assert apps == {"app7", "app11"}


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


S6_1_CHAPEAU = "In this Act, unless the contrary intention appears:"


def test_s6_1_is_the_bare_chapeau(clauses: dict[str, ClauseRecord]) -> None:
    """The words after a definition's paragraphs used to land on s6(1), whose
    stored text then read "...appears: but does not include a person ... and
    includes an order ... that carries on a credit reporting business. but
    does not include:" (#178). s6(1) holds hundreds of definitions; none of
    them speaks for the subsection."""
    assert clauses["s6/1"].text == S6_1_CHAPEAU


CLOSED_BY_THEIR_OWN_WORDS = {
    # The words after the list close the whole definition, so they join its own
    # clause, as s6C(1)'s and APP 11.2's closing words join theirs.
    "s6/1/credit-reporting-body": (
        "credit reporting body means: that carries on a credit reporting business."
    ),
    "s6/1/court-tribunal-order": (
        "court/tribunal order means an order, direction or other instrument made by: "
        "and includes an order, direction or other instrument that is of an interim or "
        "interlocutory nature."
    ),
    "s6/1/commonwealth-officer": (
        "Commonwealth officer means a person who holds office under, or is employed by, "
        "the Commonwealth, and includes: but does not include a person permanently or "
        "temporarily employed in the Australian Capital Territory Government Service or in "
        "the Public Service of the Northern Territory."
    ),
}


@pytest.mark.parametrize("path", sorted(CLOSED_BY_THEIR_OWN_WORDS))
def test_a_definition_holds_the_words_that_close_it(
    clauses: dict[str, ClauseRecord], path: str
) -> None:
    """Each of the four used to lose these words to s6(1): *credit reporting
    body* read as "(a) an organisation; or (b) an agency prescribed by the
    regulations", so every organisation was one (#178)."""
    assert clauses[path].text == CLOSED_BY_THEIR_OWN_WORDS[path]


def test_the_paragraphs_of_a_closed_definition_are_as_they_were(
    clauses: dict[str, ClauseRecord],
) -> None:
    """Closing words go to the definition, not to its last paragraph, and no
    paragraph borrows them: each is only its own words."""
    assert clauses["s6/1/credit-reporting-body/a"].text == "an organisation; or"
    assert (
        clauses["s6/1/credit-reporting-body/b"].text == "an agency prescribed by the regulations;"
    )
    assert clauses["s6/1/court-tribunal-order/e"].text == "a member or an officer of a tribunal;"
    assert clauses["s6/1/commonwealth-officer/d"].text == (
        "a member, staff member or special member of the Australian Federal Police;"
    )


def test_a_connector_between_two_paragraph_groups_leads_the_paragraph_after_it(
    clauses: dict[str, ClauseRecord],
) -> None:
    """*record*: "(a) a document; or (b) an electronic or other device; but does
    not include: (d) a generally available publication; or ...". The words
    govern (d) onward, so they are read with (d); on the definition they would
    sit between inclusions and exclusions that nothing tells apart, and
    "record includes:" would own all of them (#178, ADR-0039)."""
    assert clauses["s6/1/record"].text == "record includes:"
    assert clauses["s6/1/record/a"].text == "a document; or"
    assert clauses["s6/1/record/b"].text == "an electronic or other device;"
    assert clauses["s6/1/record/d"].text == (
        "but does not include: a generally available publication; or"
    )
    # Said once: (e)-(h) are as they were. A chunk is one clause (corpus/chunker.py),
    # and repeating the connector on each would write words the Act doesn't.
    for letter in ("e", "f", "fa", "g", "h"):
        assert not clauses[f"s6/1/record/{letter}"].text.startswith("but does not include")
    # What a retrieved chunk of (d) says, on its own: the chunk's text is the
    # clause's canonical text.
    assert clauses["s6/1/record/d"].canonical_text.startswith("but does not include:")


_P_BLOCK = re.compile(r"<p\b[^>]*>.*?</p>", re.DOTALL)
_P_CLASS = re.compile(r'<p\b[^>]*\bclass="([^"]*)"')
_PARAGRAPH_TOKEN = re.compile(r"^\([a-z]+\)\s*")


def _plain(block: str) -> str:
    return html_module.unescape(re.sub(r"<[^>]+>", "", block))


def _class_of(block: str) -> str:
    match = _P_CLASS.match(block)
    return match.group(1) if match else ""


def _source_words(term: str) -> list[str]:
    """Every word of one s6(1) definition, read straight off the fixture's
    markup in source order. A paragraph's "(a)" is its clause path, not its
    text, so it is left out; notes are not operative text."""
    blocks = _P_BLOCK.findall(CONSTRUCTS.read_text(encoding="utf-8"))
    start = next(
        i
        for i, b in enumerate(blocks)
        if _class_of(b) == "Definition" and _plain(b).split()[: len(term.split())] == term.split()
    )
    words = _plain(blocks[start]).split()
    for block in blocks[start + 1 :]:
        kind = _class_of(block)
        if kind in ("Definition", "subsection") or kind.startswith("ActHead"):
            break
        if kind == "notetext":
            continue
        text = _plain(block)
        if kind == "paragraph":
            text = _PARAGRAPH_TOKEN.sub("", text.strip())
        words += text.split()
    return words


def _stored_words(clauses: dict[str, ClauseRecord], path: str) -> list[str]:
    """The same definition (or subsection) as stored: its clause, then the clauses
    inside it, in the order the parser emits them."""
    return [
        word
        for p, clause in clauses.items()
        if p == path or p.startswith(f"{path}/")
        for word in clause.text.split()
    ]


@pytest.mark.parametrize(
    ("term", "path"),
    [
        ("record", "s6/1/record"),
        ("credit reporting body", "s6/1/credit-reporting-body"),
        ("Commonwealth officer", "s6/1/commonwealth-officer"),
        ("court/tribunal order", "s6/1/court-tribunal-order"),
    ],
)
def test_every_word_of_a_definition_is_stored_exactly_once(
    clauses: dict[str, ClauseRecord], term: str, path: str
) -> None:
    """No word of the Act is lost to a neighbouring clause, written by the
    parser, or stored twice (#178's four definitions lost theirs to s6(1))."""
    assert sorted(_stored_words(clauses, path)) == sorted(_source_words(term))


def test_a_connector_keeps_source_order_across_the_stored_clauses(
    clauses: dict[str, ClauseRecord],
) -> None:
    """Read *record*'s clauses in order — the definition, then (a), (b), (d)
    ... (h) — and the Act's own sentence comes out, "but does not include:"
    between (b) and (d). That holds because the connector leads (d); closing
    words on the definition would put them before (a)."""
    assert _stored_words(clauses, "s6/1/record") == _source_words("record")


_OPERATIVE = (
    "subsection",
    "subsection2",
    "Definition",
    "paragraph",
    "paragraphsub",
    "paragraphsub-sub",
)
_LEADING_TOKEN = {
    "subsection": re.compile(r"^(\(\d+[A-Z]*\)|\d+\.\d+[A-Z]*)\s*"),
    "paragraph": re.compile(r"^\([a-z]+\)\s*"),
    "paragraphsub": re.compile(r"^\([ivx]+[a-z]*\)\s*"),
    "paragraphsub-sub": re.compile(r"^\([A-Z]\)\s*"),
}


def _operative_words(source: str) -> Counter[str]:
    """Every word of the operative text from the first section on, read off the
    markup with no parser code: the blocks that hold provisions, minus the
    "(a)" or "11.1" each starts with (a clause's number is its path, not its
    text). Headings, notes and tables are not operative text, and the recitals
    before section 1 belong to no section."""
    words: Counter[str] = Counter()
    in_a_section = False
    for block in _P_BLOCK.findall(source):
        kind = _class_of(block)
        in_a_section = in_a_section or kind == "ActHead5"
        if not in_a_section or kind not in _OPERATIVE:
            continue
        text = _plain(block).strip()
        if kind in _LEADING_TOKEN:
            text = _LEADING_TOKEN[kind].sub("", text, count=1)
        words.update(text.split())
    return words


def _stored_word_counts(source: str) -> Counter[str]:
    parsed = parse_compilation(source, instrument_id="privacy-act-1988", version_id="C2026C00227")
    return Counter(word for clause in parsed.clauses for word in clause.text.split())


@pytest.mark.parametrize("name", ["constructs", "excerpt"])
def test_every_operative_word_of_a_fixture_is_stored_exactly_once(name: str) -> None:
    """The parser may put a word on the wrong clause (#178), but it may never
    lose one, write one or store one twice. Checked on every real fixture, so
    every Definition, list and closing line in them is covered, not just the
    ones a test above names."""
    source = (FIXTURES / f"privacy-act-1988-C2026C00227.{name}.html").read_text(encoding="utf-8")
    assert _stored_word_counts(source) == _operative_words(source)


def test_closing_words_after_a_non_definition_list_stay_on_their_subsection(
    clauses: dict[str, ClauseRecord],
) -> None:
    """s8(2): "Where: (a) ...; (b) ...; that act or practice shall be treated as
    ...". Not a definition, so it is as it always was (#178 leaves it alone)."""
    assert clauses["s8/2"].text == (
        "Where: that act or practice shall be treated as the act or the practice of the "
        "agency that holds that record."
    )
    assert clauses["s8/2/a"].text.startswith("an act done or a practice engaged in by a person")
    assert clauses["s8/2/b"].text == "that agency does not hold that record;"


# --------------------------------------------------------------------------
# A connector between two paragraph groups of a subsection with no definition
# (#186, ADR-0041). The Act has 21, in 15 sections; the constructs fixture
# holds them all, cut verbatim. Each splits one lettered list: the letters
# carry on across it ((ee) then (f); (b) then (c)).
# --------------------------------------------------------------------------

# The paragraph each connector sits in front of, in document order.
CONNECTOR_PARAGRAPHS = [
    "s7/1/f",
    "s11/2/d",
    "s20k/4/d",
    "s23c/2/c",
    "s23c/3/c",
    "s26wc/1/d",
    "s26wc/2/c",
    "s26we/2/c",
    "s26wf/1/e",
    "s26wf/2/e",
    "s26wf/3/e",
    "s26wf/4/e",
    "s26wf/5/e",
    "s26wg/c",
    "s26wn/d",
    "s26wq/1/c",
    "s30/4/c",
    "s46/2/c",
    "s50/2/c",
    "sch1/app7/6/c",
    "sch2/s11/5/c",
]
# Three of them sit in a subsection whose last words close it: "this Part does
# not require: (e) ...; (f) ...; to take steps to notify ...". Those words
# complete the connector's sentence, and closing words stay on the subsection,
# so the connector stays with them: the subsection is stored as it always was,
# until the human decides what such closing words attach to (ADR-0041).
HELD_WITH_CLOSING_WORDS = {"s26wf/2/e", "s26wf/5/e", "s30/4/c"}
_PARAGRAPH_KINDS = ("paragraph", "paragraphsub", "paragraphsub-sub")


class _Connector(NamedTuple):
    """One connector and the subsection around it, read off the markup with no
    parser code."""

    words: str  # the connector itself
    chapeau: str  # the subsection's opening words, its "(2)" left off
    connectors: tuple[str, ...]  # every connector of the subsection, in order
    closing: tuple[str, ...]  # words that close the same subsection: no paragraph follows them
    paragraph: str  # the paragraph right after the connector, its "(c)" left off
    source_order: tuple[str, ...]  # every word of the subsection, in the Act's order
    tokenless: bool  # holds a paragraph with no "(c)": it continues the one above


def _subsections_without_definitions(source: str) -> list[list[tuple[str, str]]]:
    """The operative blocks, as (class, words), of each subsection that holds no
    Definition: from its `subsection` block to the next subsection or heading."""
    subsections: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] | None = None
    for block in _P_BLOCK.findall(source):
        kind = _class_of(block)
        if kind == "subsection" or kind.startswith("ActHead"):
            current = [] if kind == "subsection" else None
            if current is not None:
                subsections.append(current)
        if current is not None and kind in _OPERATIVE:
            current.append((kind, " ".join(_plain(block).split())))
    return [s for s in subsections if all(kind != "Definition" for kind, _ in s)]


def _connectors(source: str) -> list[_Connector]:
    """Every `subsection2` outside a Definition that sits directly before a
    paragraph, in document order. One that no paragraph follows closes its
    subsection instead, and is reported as `closing`."""
    found: list[_Connector] = []
    for blocks in _subsections_without_definitions(source):
        tokenless = any(
            kind in _PARAGRAPH_KINDS and not _LEADING_TOKEN[kind].match(text)
            for kind, text in blocks
        )
        bare = [
            (kind, _LEADING_TOKEN[kind].sub("", text, count=1) if kind in _LEADING_TOKEN else text)
            for kind, text in blocks
        ]
        leads = [
            kind == "subsection2" and i + 1 < len(bare) and bare[i + 1][0] in _PARAGRAPH_KINDS
            for i, (kind, _) in enumerate(bare)
        ]
        connectors = tuple(bare[i][1] for i, lead in enumerate(leads) if lead)
        closing = tuple(
            words for i, (kind, words) in enumerate(bare) if kind == "subsection2" and not leads[i]
        )
        source_order = tuple(" ".join(words for _, words in bare).split())
        found += [
            _Connector(
                words=bare[i][1],
                chapeau=bare[0][1],
                connectors=connectors,
                closing=closing,
                paragraph=bare[i + 1][1],
                source_order=source_order,
                tokenless=tokenless,
            )
            for i, lead in enumerate(leads)
            if lead
        ]
    return found


CONSTRUCTS_CONNECTORS = _connectors(CONSTRUCTS.read_text(encoding="utf-8"))
# Matched by position; test_the_constructs_fixture_holds_every_connector_in_the_act
# fails if the two lists are not the same length.
_PAIRS = list(zip(CONNECTOR_PARAGRAPHS, CONSTRUCTS_CONNECTORS, strict=False))
LEADING = [pytest.param(p, c, id=p) for p, c in _PAIRS if p not in HELD_WITH_CLOSING_WORDS]
HELD = [pytest.param(p, c, id=p) for p, c in _PAIRS if p in HELD_WITH_CLOSING_WORDS]
IN_SOURCE_ORDER = [pytest.param(p, c, id=p) for p, c in _PAIRS if not c.closing and not c.tokenless]


def _assert_connector(clauses: dict[str, ClauseRecord], path: str, connector: _Connector) -> None:
    """A connector leads the paragraph after it, and the subsection is its
    chapeau alone; unless closing words follow it, when both are as they were:
    the connector with the closing words on the subsection, the paragraph its
    own words. Either way, no other clause of the subsection says it."""
    subsection = path.rsplit("/", 1)[0]
    if connector.closing:
        assert clauses[path].text == connector.paragraph
        assert clauses[subsection].text == " ".join(
            (connector.chapeau, *connector.connectors, *connector.closing)
        )
        holder = subsection
    else:
        assert clauses[path].text == f"{connector.words} {connector.paragraph}"
        assert clauses[subsection].text == connector.chapeau
        holder = path
    said = [
        p
        for p, clause in clauses.items()
        if (p == subsection or p.startswith(f"{subsection}/")) and connector.words in clause.text
    ]
    assert said == [holder]


def test_the_constructs_fixture_holds_every_connector_in_the_act() -> None:
    """21 connectors in 15 sections (#186), the count the whole compilation gives:
    a fixture that holds fewer is testing less than the Act has."""
    assert len(CONSTRUCTS_CONNECTORS) == len(CONNECTOR_PARAGRAPHS) == 21
    # A section is "s7", or "sch1/app7" / "sch2/s11" inside a schedule.
    sections = {
        "/".join(p.split("/")[: 2 if p.startswith("sch") else 1]) for p in CONNECTOR_PARAGRAPHS
    }
    assert len(sections) == 15
    # The three held back are exactly the ones whose subsection has closing words.
    assert {p for p, c in _PAIRS if c.closing} == HELD_WITH_CLOSING_WORDS


@pytest.mark.parametrize(("path", "connector"), LEADING)
def test_a_mid_list_connector_leads_the_paragraph_after_it(
    clauses: dict[str, ClauseRecord], path: str, connector: _Connector
) -> None:
    """The words govern the paragraphs from this one on and nothing before, so
    this paragraph carries them: s7(1)'s "but does not include a reference to an
    act done ... received from:" is no longer the subsection's, with (f)-(h) left
    bare noun phrases that read as inclusions (#186)."""
    _assert_connector(clauses, path, connector)


@pytest.mark.parametrize(("path", "connector"), IN_SOURCE_ORDER)
def test_a_mid_list_connector_keeps_source_order_across_the_stored_clauses(
    clauses: dict[str, ClauseRecord], path: str, connector: _Connector
) -> None:
    """Read the subsection's clauses in order, the subsection and then its
    paragraphs, and the Act's own sentence comes out, with the connector between
    the two groups. Not asked of a subsection with closing words, which sit on
    the subsection ahead of its paragraphs whatever the source order, nor of
    s26WG and s26WQ(1), whose tokenless paragraph joins the one above and so moves
    words ahead of its subparagraphs: those are pinned below."""
    subsection = path.rsplit("/", 1)[0]
    assert tuple(_stored_words(clauses, subsection)) == connector.source_order


@pytest.mark.parametrize(("path", "connector"), HELD)
def test_a_connector_beside_closing_words_stays_with_them(
    clauses: dict[str, ClauseRecord], path: str, connector: _Connector
) -> None:
    """s26WF(2) and (5), s30(4): "this Part does not require: (e) ...; (f) ...;
    to take steps to notify the individual ...". The closing words complete the
    connector's sentence and stay on the subsection, as every closing words do
    (s8(2)). Leading (e) with the connector would put "does not require" on (e)
    and leave the subsection as "If: to take steps to notify ...", with the
    negation gone from it. So the connector stays with them, and the subsection
    and its paragraphs are stored as they always were (ADR-0041)."""
    _assert_connector(clauses, path, connector)


def test_a_subsection_held_with_its_closing_words_reads_as_it_always_did(
    clauses: dict[str, ClauseRecord],
) -> None:
    assert clauses["s26wf/2"].text == (
        "If: this Part does not require: to take steps to notify the individual of the "
        "contents of a statement that relates to the access or disclosure."
    )
    assert clauses["s26wf/2/e"].text == (
        "the APP entity, credit reporting body, credit provider or file number recipient, "
        "as the case may be; or"
    )
    assert clauses["s26wf/5"].text == (
        "If: this Part does not require: to take steps to notify the individual of the "
        "contents of a statement that relates to the loss."
    )
    assert clauses["s30/4"].text == (
        "Where, at the end of 60 days after a copy of a report about an act or practice of "
        "an agency, file number recipient, credit reporting body or credit provider was "
        "served under subsection (3), the Commissioner: the Commissioner shall give to the "
        "Minister a further report that: and shall serve a copy of the report on the "
        "Minister (if any) responsible for the agency, recipient, credit reporting body or "
        "credit provider."
    )


def test_s7_1_does_not_read_its_exclusions_as_inclusions(
    clauses: dict[str, ClauseRecord],
) -> None:
    """The reason for #186. s7(1): "... is a reference to: (a) ... (ee) ...; but
    does not include a reference to an act done ... received from: (f) an
    intelligence agency; ...". Stored on the subsection, (f)-(h) were bare noun
    phrases under "a reference to:", and read as acts and practices the Act
    covers."""
    assert clauses["s7/1"].text == (
        "Except so far as the contrary intention appears, a reference in this Act "
        "(other than section 8) to an act or to a practice is a reference to:"
    )
    assert clauses["s7/1/ee"].text == (
        "an act done, or a practice engaged in, by an organisation, other than an exempt "
        "act or exempt practice (see sections 7B and 7C);"
    )
    assert clauses["s7/1/f"].text == (
        "but does not include a reference to an act done, or a practice engaged in, in "
        "relation to a record that has originated with, or has been received from: "
        "an intelligence agency;"
    )
    # Said once. A chunk is one clause and the Act prints the words once.
    for letter in ("g", "ga", "gb", "h"):
        assert not clauses[f"s7/1/{letter}"].text.startswith("but does not include")
    assert clauses["s7/1/h"].text == "the ACC or the Board of the ACC."
    # What a retrieved chunk of (f) says, on its own.
    assert clauses["s7/1/f"].canonical_text.startswith("but does not include a reference")


def test_a_connector_that_also_finishes_the_sentence_before_it_still_leads(
    clauses: dict[str, ClauseRecord],
) -> None:
    """s26WG and s50(2): the connector's first words complete the sentence the
    first group began ("... information: (a) would be likely; or (b) would not be
    likely; **to result in serious harm ..., have regard to the following:** (c)")
    and its last words open the second group. One source block, and the parser
    does not cut a block, so it leads (c) whole (ADR-0041). Tokenless paragraphs
    inside s26WG(h) are as they were (#97)."""
    assert clauses["s26wg"].text == (
        "For the purposes of this Division, in determining whether a reasonable person "
        "would conclude that an access to, or a disclosure of, information:"
    )
    assert clauses["s26wg/c"].text == (
        "to result in serious harm to any of the individuals to whom the information "
        "relates, have regard to the following: the kind or kinds of information;"
    )
    assert clauses["s26wg/a"].text == "would be likely; or"
    assert clauses["s26wg/d"].text == "the sensitivity of the information;"
    assert clauses["s26wg/h"].text == (
        "if a security technology or methodology: the likelihood that the persons, or the "
        "kinds of persons, who: have obtained, or could obtain, information or knowledge "
        "required to circumvent the security technology or methodology;"
    )
    assert clauses["s50/2/c"].text.startswith(
        "and that that matter could be more conveniently or effectively dealt with by "
        "the alternative complaint body, the Commissioner may decide not to investigate "
        "the matter, or not to investigate the matter further, as the case may be, and, "
        "if the Commissioner so decides, he or she shall: transfer the complaint to "
    )
    assert clauses["s50/2/d"].text.startswith("give notice in writing to the complainant")
    assert clauses["s50/2"].text.endswith("the Commissioner forms the opinion that:")


def test_a_connector_before_a_tokenless_continuation_keeps_the_continuation_where_it_was(
    clauses: dict[str, ClauseRecord],
) -> None:
    """s26WQ(1)(d) ends with a tokenless paragraph ("as if that subsection
    required compliance ...") that joins (d), as #97 made it. The connector
    before (c) leads (c) and does not disturb it."""
    assert clauses["s26wq/1"].text == "If the Commissioner:"
    assert clauses["s26wq/1/c"].text == (
        "the Commissioner may, by written notice given to the entity: declare that "
        "sections 26WK and 26WL do not apply in relation to:"
    )
    assert clauses["s26wq/1/d"].text == (
        "declare that subsection 26WL(3) has effect in relation to: as if that subsection "
        "required compliance with subsection 26WL(2) before the end of a period "
        "specified in the declaration."
    )


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
    text = raw.decode("utf-8")
    clauses = _parse(text)
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
    # Every operative word is stored exactly once, bar the one heading set in
    # subsection style that is not a provision (s80U).
    stored, operative = _stored_word_counts(text), _operative_words(text)
    assert stored - operative == Counter()
    assert operative - stored == Counter(["Enforceable", "civil", "penalty", "provisions"])
    # The words after a definition's paragraphs are the definition's (#178).
    assert clauses["s6/1"].text == S6_1_CHAPEAU
    for path, expected in CLOSED_BY_THEIR_OWN_WORDS.items():
        assert clauses[path].text == expected
    assert clauses["s6/1/record"].text == "record includes:"
    assert clauses["s6/1/record/d"].text.startswith("but does not include: ")
    # A connector between two paragraph groups of an ordinary subsection leads the
    # paragraph after it (#186): all 21, exactly the ones the fixture holds.
    connectors = _connectors(text)
    assert len(connectors) == len(CONNECTOR_PARAGRAPHS)
    for path, connector in zip(CONNECTOR_PARAGRAPHS, connectors, strict=True):
        _assert_connector(clauses, path, connector)
    assert connectors == CONSTRUCTS_CONNECTORS
