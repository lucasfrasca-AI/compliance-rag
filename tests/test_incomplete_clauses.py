"""An incomplete clause (#211, ADR-0047): the parser marks the provision an
image, a table or a penalty line falls in, with why, and never puts an image's
`alt` text into a clause; the state is stored with the clause, compared by an
ingest and a revision like its text, marked in the prompt, and carried by any
answer that cites it.

The parser is checked on every real fixture (the excerpt, the constructs cut,
the six Acme policies) and, with `PRIVACY_ACT_COMPILATION_HTML` set, on the
whole pinned compilation: every clause it marks, in the selection and out.
The orchestration across a real Neo4j and Qdrant is
`tests/test_incomplete_clauses_stores.py`."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from compliance_rag.adapters.fakes import FakeClauseStore, FakeGenerator, FakeRetrievalStore
from compliance_rag.adapters.gcs_clause_store import GCSClauseStore
from compliance_rag.adapters.interfaces import ClauseStore
from compliance_rag.assessment.prompt import SYSTEM_PROMPT, SourceClause, fence
from compliance_rag.corpus.chunker import chunk_clause
from compliance_rag.corpus.ingest import IngestDriftError, ingest_compilation
from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    ClauseRecord,
    Incompleteness,
    IncompleteReason,
    Scope,
    canonical_sha256,
    incomplete_json,
    incomplete_of_json,
)
from compliance_rag.corpus.parser import (
    FORMULA_NOT_TEXT,
    IMAGE_NOT_TEXT,
    PENALTY_NOT_PARSED,
    TABLE_NOT_TEXT,
    parse_compilation,
)
from compliance_rag.corpus.policy_parser import parse_policy_markdown
from compliance_rag.corpus.revision import (
    RevisedClause,
    RevisionDrift,
    RevisionMode,
    RevisionRecord,
    check_revision_hashes,
    plan_revision,
)
from compliance_rag.corpus.selection_manifest import SelectionManifest
from compliance_rag.eval.compilation_clauses import load_compilation_clauses
from compliance_rag.eval.golden_corpus import gate_chunks, gate_clauses, gate_law_clauses
from tests.test_api_answers import (
    QUESTION,
    _ask,  # pyright: ignore[reportPrivateUsage] — the API tests' own request helpers
    _ledger,  # pyright: ignore[reportPrivateUsage]
    _reply,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_gcs_clause_store import _FakeBucket  # pyright: ignore[reportPrivateUsage]

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "docs" / "research" / "fixtures"
POLICIES = FIXTURES / "synthetic-policies"
INSTRUMENT, VERSION = "privacy-act-1988", "C2026C00227"
S6DA_2 = f"{INSTRUMENT}:{VERSION}:s6da/2"
ALT = "Start formula Amount that would be the annual turnover"
FULL_SHA256 = "6d0a43e5da103d1eeb5317ae9ac213c5b51acab2357dc01b981a91e1052c5c3b"


def _parse(html: str) -> dict[str, ClauseRecord]:
    parsed = parse_compilation(html, instrument_id=INSTRUMENT, version_id=VERSION)
    return {c.clause_path: c for c in parsed.clauses}


def _fixture(name: str) -> str:
    return (FIXTURES / f"privacy-act-1988-C2026C00227.{name}.html").read_text(encoding="utf-8")


def _section(body: str) -> str:
    return f'<p class="ActHead5"><span class="CharSectno">99</span> Test</p>{body}'


# ---- the parser, on every real fixture ---------------------------------------


def test_s6da_2_is_stored_as_it_is_and_marked_incomplete_for_its_formula() -> None:
    """The constructs cut holds s6DA verbatim, its formula an `<img>` in a
    `Formula` block. The clause ends where the Act's words end; the formula
    marks it incomplete; nothing else in the cut is."""
    assert _fixture("constructs").count("<img") == 1  # every image in the cut is accounted for
    clauses = _parse(_fixture("constructs"))
    s6da_2 = clauses["s6da/2"]
    assert s6da_2.text.endswith("is the amount worked out using the formula:")
    assert s6da_2.incomplete == (FORMULA_NOT_TEXT,)
    assert s6da_2.incomplete[0].reason is IncompleteReason.IMAGE_NOT_TEXT
    assert s6da_2.incomplete[0].detail == "the formula is not available in text form"
    assert [p for p, c in clauses.items() if FORMULA_NOT_TEXT in c.incomplete] == ["s6da/2"]
    # The cut's only other marks are its penalty lines' provisions (s20K).
    others = {p for p, c in clauses.items() if c.incomplete} - {"s6da/2"}
    assert others and others <= PENALTY_CLAUSES
    assert all(clauses[p].incomplete == (PENALTY_NOT_PARSED,) for p in others)


@pytest.mark.parametrize("name", ["constructs", "excerpt"])
def test_an_images_alt_text_never_enters_a_clause(name: str) -> None:
    """The alt text is a transcription by whoever published the HTML, not the
    Act's words (the human's decision on #211)."""
    for clause in _parse(_fixture(name)).values():
        assert "formula" not in clause.text.lower() or not clause.text.lower().count("fraction")
        assert ALT.lower() not in clause.text.lower()
        assert "start fraction" not in clause.canonical_text.lower()


def test_the_excerpt_has_no_incomplete_clause() -> None:
    assert not [c for c in _parse(_fixture("excerpt")).values() if c.incomplete]


@pytest.mark.parametrize("policy", sorted(p.name for p in POLICIES.glob("*.md")))
def test_no_acme_policy_clause_is_incomplete(policy: str) -> None:
    """The policy parser reads Markdown text and marks nothing (every one of
    the six fixtures, CLAUDE.md)."""
    clauses = parse_policy_markdown(
        (POLICIES / policy).read_text(encoding="utf-8"),
        instrument_id=f"acme/{policy.removesuffix('.md')}",
        version_id="v1",
        tenant_id="acme",
    )
    assert clauses and not [c for c in clauses if c.incomplete]


def test_the_committed_gate_corpus_marks_s6da_2_alone() -> None:
    """`clauses.json` is the parser's output for the 779 selected clauses: of
    them, only s6da/2 is incomplete (no table or penalty is selected)."""
    committed = load_compilation_clauses()
    assert len(committed.clauses) == 779
    marked = {c.clause_path: c.incomplete for c in committed.clauses if c.incomplete}
    assert marked == {"s6da/2": (FORMULA_NOT_TEXT,)}


# ---- the parser, on the whole pinned compilation -----------------------------

TABLE_PROVISIONS = {
    # s16A(1): permitted general situations; s20F(1): permitted CRB disclosures;
    # s20W: retention periods; s20X(1); s21H: permitted CP uses; s37.
    "s16a/1": ("a", "b", "c"),
    "s20f/1": ("a", "b"),
    "s20w": ("a", "b"),
    "s20x/1": (),
    "s21h": ("a", "b"),
    "s37": (),
}
PENALTY_CLAUSES = frozenset(
    [
        "s20c/1",
        "s20d/4",
        "s20e/1",
        "s20e/4",
        "s20e/4/a",
        "s20e/4/b",
        "s20e/4a",
        "s20e/4a/a",
        "s20e/4a/a/i",
        "s20e/4a/a/ii",
        "s20e/4a/a/iii",
        "s20e/4a/b",
        "s20e/5",
        "s20g/1",
        "s20g/7",
        "s20h/1",
        "s20h/3",
        "s20h/4",
        "s20h/6",
        "s20j/1",
        "s20j/1/a",
        "s20j/1/b",
        "s20k/1",
        "s20k/1/a",
        "s20k/1/b",
        "s20k/1/c",
        "s20k/4",
        "s20k/4/a",
        "s20k/4/b",
        "s20k/4/c",
        "s20k/4/d",
        "s20k/4/e",
        "s20l/1",
        "s20l/1/a",
        "s20l/1/b",
        "s20p/1",
        "s20p/1/a",
        "s20p/1/b",
        "s20p/2",
        "s20v/2",
        "s20v/3",
        "s20v/3/a",
        "s20v/3/b",
        "s20v/5",
        "s20v/5/a",
        "s20v/5/a/i",
        "s20v/5/a/ii",
        "s20v/5/b",
        "s20v/6",
        "s20v/6/a",
        "s20v/6/b",
        "s20y/2",
        "s20y/2/a",
        "s20y/2/b",
        "s20y/2/b/i",
        "s20y/2/b/ii",
        "s20y/4",
        "s20y/4/a",
        "s20y/4/b",
        "s20z/2",
        "s20z/3",
        "s20z/5",
        "s20z/7",
        "s20za/2",
        "s20za/4",
        "s21d/1",
        "s21d/6",
        "s21e",
        "s21e/a",
        "s21e/b",
        "s21ea",
        "s21ea/a",
        "s21ea/b",
        "s21f/2",
        "s21g/1",
        "s21g/4",
        "s21g/6",
        "s21r/1",
        "s21r/1/a",
        "s21r/1/b",
        "s21r/2",
        "s21r/2/a",
        "s21r/2/b",
        "s21r/3",
        "s21r/4",
        "s21s/2",
        "s21s/2/a",
        "s21s/2/b",
        "s21s/2/c",
        "s22c/1",
        "s22c/1/a",
        "s22c/1/b",
        "s22d/1",
        "s22d/1/a",
        "s22d/1/b",
        "s22e/1",
        "s22e/1/a",
        "s22e/1/b",
        "s22f/1",
        "s22f/1/a",
        "s22f/1/a/i",
        "s22f/1/a/ii",
        "s22f/1/a/iii",
        "s22f/1/b",
        "s24/1",
        "s24/1/a",
        "s24/1/b",
        "s24/1/c",
        "s24/1/c/i",
        "s24/1/c/ii",
        "s24/2",
        "s24/2/a",
        "s24/2/b",
        "s24/2/c",
        "s24/3",
        "s24/3/a",
        "s24/3/b",
        "s24/4",
        "s24a/1",
        "s24a/1/a",
        "s24a/1/b",
        "s24a/1/c",
        "s24a/1/c/i",
        "s24a/1/c/ii",
        "s24a/2",
        "s24a/2/a",
        "s24a/2/b",
        "s24a/2/c",
        "s24a/3",
        "s24a/3/a",
        "s24a/3/b",
        "s24a/4",
        "s26xc/1",
        "s26xc/1/a",
        "s26xc/1/b",
        "s65/1",
        "s65/1/a",
        "s65/1/b",
        "s65/3",
        "s66/1",
        "s66/1/a",
        "s66/1/b",
        "s66/1aa",
        "s66/1aa/a",
        "s66/1aa/b",
        "s66/1aa/c",
        "s80q/1",
        "s80q/1/a",
        "s80q/1/b",
        "s80q/1/c",
    ]
)
"""The clauses of the provisions the compilation's 61 penalty lines follow."""


@pytest.mark.skipif(
    not os.environ.get("PRIVACY_ACT_COMPILATION_HTML"),
    reason="PRIVACY_ACT_COMPILATION_HTML not set — the full 2.3 MB compilation",
)
def test_every_clause_the_whole_compilation_marks() -> None:
    """Over all 4114 clauses: s6DA(2)'s formula; the six operative tables, each
    marking its provision (every clause of which names "the table" or sits in
    a provision that does); and the 61 penalty lines' provisions — 166
    clauses. The coat of arms and the endnotes' tables are in no provision and
    mark nothing. Of the 779 the selection keeps, s6da/2 alone."""
    raw = Path(os.environ["PRIVACY_ACT_COMPILATION_HTML"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FULL_SHA256, "a different compilation file"
    clauses = _parse(raw.decode("utf-8"))
    marked = {p: c.incomplete for p, c in clauses.items() if c.incomplete}
    tables = {
        path
        for top, paragraphs in TABLE_PROVISIONS.items()
        for path in (top, *(f"{top}/{p}" for p in paragraphs))
    }
    assert {p for p, i in marked.items() if i == (TABLE_NOT_TEXT,)} == tables
    assert {p for p, i in marked.items() if i == (PENALTY_NOT_PARSED,)} == PENALTY_CLAUSES
    assert {p for p, i in marked.items() if i == (FORMULA_NOT_TEXT,)} == {"s6da/2"}
    assert len(marked) == 1 + len(tables) + len(PENALTY_CLAUSES) == 166
    selected = {c.clause_path for c in load_compilation_clauses().clauses}
    assert set(marked) & selected == {"s6da/2"}
    # What the conservation test (ADR-0039) can't see, accounted for (#211):
    # every image and table between section 1 and the endnotes marks a
    # provision; the only others are the coat of arms before section 1 and
    # the endnotes' four tables, which belong to no provision.
    text = raw.decode("utf-8")
    first_section, endnotes = text.find('class="ActHead5"'), text.find('class="ENotesHeading1"')
    tables = [m.start() for m in re.finditer(r"<table\b", text)]
    images = [m.start() for m in re.finditer(r"<img\b", text)]
    assert len([t for t in tables if first_section < t < endnotes]) == len(TABLE_PROVISIONS)
    assert len([t for t in tables if t > endnotes]) == 4
    assert len([i for i in images if first_section < i < endnotes]) == 1  # the formula
    assert len([i for i in images if i < first_section]) == 1  # the coat of arms
    assert len(re.findall(r'<p class="Penalty"', text)) == 61
    assert not any(ALT.lower() in c.text.lower() for c in clauses.values())


# ---- the parser's rule, on small constructed sources -------------------------


def test_a_table_after_a_subsections_paragraphs_marks_the_whole_provision() -> None:
    clauses = _parse(
        _section(
            '<p class="subsection">(1) An entity may do it if:</p>'
            '<p class="paragraph">(a) column 1 of the table applies; and</p>'
            '<p class="paragraph">(b) column 2 of the table is met.</p>'
            '<table><tr><td><p class="Tabletext">Item 1</p></td></tr></table>'
            '<p class="subsection">(2) Something else.</p>'
        )
    )
    assert {p: c.incomplete for p, c in clauses.items()} == {
        "s99/1": (TABLE_NOT_TEXT,),
        "s99/1/a": (TABLE_NOT_TEXT,),
        "s99/1/b": (TABLE_NOT_TEXT,),
        "s99/2": (),
    }
    assert "Item 1" not in " ".join(c.text for c in clauses.values())


def test_an_image_inline_in_a_paragraph_marks_its_provision() -> None:
    clauses = _parse(
        _section(
            '<p class="subsection">(1) The amount is:</p>'
            '<p class="paragraph">(a) worked out as <img src="x.png" alt="x times y" />; or</p>'
            '<p class="paragraph">(b) nil.</p>'
        )
    )
    assert clauses["s99/1/a"].text == "worked out as ; or"
    assert {p: c.incomplete for p, c in clauses.items()} == {
        "s99/1": (IMAGE_NOT_TEXT,),
        "s99/1/a": (IMAGE_NOT_TEXT,),
        "s99/1/b": (IMAGE_NOT_TEXT,),
    }


def test_a_definitions_table_marks_that_definition_alone() -> None:
    """Each of several Definitions is a provision of its own (ADR-0045)."""
    clauses = _parse(
        _section(
            '<p class="subsection">(1) In this Act:</p>'
            '<p class="Definition"><span style="font-weight:bold">alpha</span>'
            "<span> means the table:</span></p>"
            '<table><tr><td><p class="Tabletext">row</p></td></tr></table>'
            '<p class="Definition"><span style="font-weight:bold">beta</span>'
            "<span> means a thing.</span></p>"
        )
    )
    assert {p: c.incomplete for p, c in clauses.items()} == {
        "s99/1": (),
        "s99/1/alpha": (TABLE_NOT_TEXT,),
        "s99/1/beta": (),
    }


@pytest.mark.parametrize(
    "between",
    [
        '<p class="notetext">Note: see the figure.</p>',
        '<p class="SubsectionHead">A heading</p>',
        '<p class="ActHead5"><span class="CharSectno">100</span> Next</p>',
    ],
)
def test_an_image_after_a_note_or_a_heading_marks_nothing(between: str) -> None:
    """No note or example in C2026C00227 is operative, and what follows a
    heading is in no provision yet."""
    clauses = _parse(
        _section(
            f'<p class="subsection">(1) One.</p>{between}'
            '<p class="Formula"><img src="x.png" alt="a formula" /></p>'
        )
    )
    assert not [c for c in clauses.values() if c.incomplete]


def test_an_image_before_any_section_marks_nothing() -> None:
    clauses = _parse(
        '<p><img src="arms.png" alt="Coat of Arms" /></p>'
        + _section('<p class="subsection">(1) One.</p>')
    )
    assert not [c for c in clauses.values() if c.incomplete]


def test_a_penalty_line_marks_the_provision_it_follows() -> None:
    clauses = _parse(
        _section(
            '<p class="subsection">(1) An entity must not:</p>'
            '<p class="paragraph">(a) do it.</p>'
            '<p class="Penalty">Civil penalty: 2,000 penalty units.</p>'
            '<p class="subsection">(2) Another.</p>'
        )
    )
    assert {p: c.incomplete for p, c in clauses.items()} == {
        "s99/1": (PENALTY_NOT_PARSED,),
        "s99/1/a": (PENALTY_NOT_PARSED,),
        "s99/2": (),
    }
    assert "penalty units" not in " ".join(c.text for c in clauses.values())


def test_two_kinds_in_one_provision_are_each_named_once_in_source_order() -> None:
    clauses = _parse(
        _section(
            '<p class="subsection">(1) The amount is:</p>'
            '<p class="Formula"><img src="x.png" alt="f" /></p>'
            '<table><tr><td><p class="Tabletext">t</p></td></tr></table>'
            '<table><tr><td><p class="Tabletext"><img src="y.png" alt="in a cell" /></p>'
            "</td></tr></table>"
        )
    )
    assert clauses["s99/1"].incomplete == (FORMULA_NOT_TEXT, TABLE_NOT_TEXT)


# ---- the state itself ---------------------------------------------------------


@pytest.mark.parametrize(
    "detail", ["the formula]", "line one\nline two", "Capitalised", "", "a [B end] marker"]
)
def test_a_detail_is_plain_lowercase_words_so_it_cant_reshape_a_marker(detail: str) -> None:
    with pytest.raises(ValidationError, match="lowercase words"):
        Incompleteness(reason=IncompleteReason.IMAGE_NOT_TEXT, detail=detail)


def test_the_stored_form_round_trips_and_absent_is_complete() -> None:
    both = (FORMULA_NOT_TEXT, TABLE_NOT_TEXT)
    assert incomplete_of_json(json.loads(json.dumps(incomplete_json(both)))) == both
    assert incomplete_of_json(None) == ()
    record = _parse(_fixture("constructs"))["s6da/2"]
    assert ClauseRecord.model_validate_json(record.model_dump_json()) == record
    # A clause object stored before ADR-0047 has no field: it reads as complete.
    legacy = json.loads(record.model_dump_json())
    del legacy["incomplete"]
    assert ClauseRecord.model_validate(legacy).incomplete == ()


def test_a_chunk_carries_its_clauses_incompleteness() -> None:
    record = _parse(_fixture("constructs"))["s6da/2"]
    assert chunk_clause(record, count_tokens=lambda t: len(t.split())).incomplete == (
        FORMULA_NOT_TEXT,
    )


# ---- the bucket: on the manifest, compared by ingest ----------------------------

S6DA = SelectionManifest(
    author="Lucas Frasca", reviewed_by="Lucas Frasca", date="2026-10-20", prefixes=("s6da/",)
)


@pytest.fixture(params=["fake", "gcs"])
def bucket(request: pytest.FixtureRequest) -> ClauseStore:
    return FakeClauseStore() if request.param == "fake" else GCSClauseStore(_FakeBucket())


async def test_the_manifest_lists_each_incomplete_clause_with_why(bucket: ClauseStore) -> None:
    await ingest_compilation(
        _fixture("constructs"),
        instrument_id=INSTRUMENT,
        version_id=VERSION,
        tenant_id=PUBLIC_TENANT_ID,
        scope=Scope.PUBLIC,
        selection=S6DA,
        store=bucket,
    )
    assert await bucket.get_incomplete(INSTRUMENT, VERSION, tenant_id=PUBLIC_TENANT_ID) == {
        "s6da/2": (FORMULA_NOT_TEXT,)
    }
    stored = await bucket.get_clause(S6DA_2, tenant_id=PUBLIC_TENANT_ID)
    assert stored is not None and stored.incomplete == (FORMULA_NOT_TEXT,)


async def test_a_reingest_that_marks_a_stored_clause_differently_is_drift(
    bucket: ClauseStore,
) -> None:
    """The deployed bucket holds s6da/2 as the parser before ADR-0047 stored
    it: the same text, no state. A re-ingest refuses, and names the revision."""
    unmarked = tuple(
        c.model_copy(update={"incomplete": ()}) for c in S6DA.select(_parsed_constructs())
    )
    await bucket.put_clauses(unmarked)
    await bucket.put_manifest(
        INSTRUMENT,
        VERSION,
        tenant_id=PUBLIC_TENANT_ID,
        canonicalisation_version=2,
        source_html=parse_compilation(
            _fixture("constructs"), instrument_id=INSTRUMENT, version_id=VERSION
        ).annotated_html,
    )
    with pytest.raises(IngestDriftError, match=r"marks \['s6da/2'\] incomplete differently"):
        await ingest_compilation(
            _fixture("constructs"),
            instrument_id=INSTRUMENT,
            version_id=VERSION,
            tenant_id=PUBLIC_TENANT_ID,
            scope=Scope.PUBLIC,
            selection=S6DA,
            store=bucket,
        )


def _parsed_constructs() -> tuple[ClauseRecord, ...]:
    return parse_compilation(
        _fixture("constructs"), instrument_id=INSTRUMENT, version_id=VERSION
    ).clauses


# ---- the revision: a state is part of a clause's form --------------------------


def test_a_clause_newly_marked_incomplete_is_one_change_with_its_text_unchanged() -> None:
    fresh = S6DA.select(_parsed_constructs())
    plan = plan_revision(
        fresh,
        source_html=_fixture("constructs"),
        annotated_html="a",
        stored_version_id=VERSION,
        stored_manifest={c.clause_path: canonical_sha256(c.canonical_text) for c in fresh},
        stored_texts={},
        stored_source_html_sha256="a",
        stored_incomplete={},
    )
    [change] = plan.changed
    assert change.new_clause_id == S6DA_2 and plan.acknowledgeable == {S6DA_2}
    assert (change.text_changed, change.incompleteness_changed) == (False, True)
    assert (change.old_incomplete, change.new_incomplete) == ((), (FORMULA_NOT_TEXT,))
    assert change.old_text == change.new_text and plan.unchanged == len(fresh) - 1


def test_a_clause_the_parse_completes_is_unmarked_by_the_same_path() -> None:
    """What un-marks a clause (ADR-0047): a parse of the same pinned source
    that no longer finds content missing — the human's signed transcription,
    say, or a PDF extraction the parser takes in — is a correction like any
    other, acknowledged and recorded."""
    stored = S6DA.select(_parsed_constructs())
    completed = tuple(c.model_copy(update={"incomplete": ()}) for c in stored)
    plan = plan_revision(
        completed,
        source_html=_fixture("constructs"),
        annotated_html="a",
        stored_version_id=VERSION,
        stored_manifest={c.clause_path: canonical_sha256(c.canonical_text) for c in stored},
        stored_texts={},
        stored_source_html_sha256="a",
        stored_incomplete={"s6da/2": (FORMULA_NOT_TEXT,)},
    )
    [change] = plan.changed
    assert (change.clause_path, change.old_incomplete, change.new_incomplete) == (
        "s6da/2",
        (FORMULA_NOT_TEXT,),
        (),
    )


def _state_record() -> RevisionRecord:
    sha = canonical_sha256(_parse(_fixture("constructs"))["s6da/2"].canonical_text)
    return RevisionRecord(
        revision_id="r-211",
        mode=RevisionMode.CORRECT,
        instrument_id=INSTRUMENT,
        version_id=VERSION,
        supersedes=None,
        at="2026-10-20T00:00:00+00:00",
        by="Lucas Frasca",
        reason="#211: s6DA(2)'s formula is an image",
        source_sha256="s",
        corpus_sha="c" * 40,
        corpus_tag=None,
        findings_citing=0,
        clauses=(RevisedClause("s6da/2", sha, sha, (), (FORMULA_NOT_TEXT,)),),
    )


def test_the_record_says_the_state_before_and_after_and_reads_back_the_same() -> None:
    record = _state_record()
    stored = json.loads(json.dumps(record.to_json()))
    assert stored["clauses"]["s6da/2"]["incomplete"] == incomplete_json((FORMULA_NOT_TEXT,))
    assert "previous_incomplete" not in stored["clauses"]["s6da/2"]
    assert RevisionRecord.from_json(stored) == record
    trail = json.loads(record.trail_entry(record.clauses[0]))
    assert trail["previous"] == {"canonical_sha256": record.clauses[0].sha256, "incomplete": []}


def test_a_clause_held_complete_moves_one_already_marked_is_done_another_state_is_drift() -> None:
    record = _state_record()
    sha = record.clauses[0].sha256
    assert sha is not None
    held = {"s6da/2": sha}
    assert check_revision_hashes(record, held, held_incomplete={}) == ["s6da/2"]
    marked = {"s6da/2": (FORMULA_NOT_TEXT,)}
    assert check_revision_hashes(record, held, held_incomplete=marked) == []
    with pytest.raises(RevisionDrift, match=r"incomplete .table_not_text"):
        check_revision_hashes(record, held, held_incomplete={"s6da/2": (TABLE_NOT_TEXT,)})


# ---- the prompt and the answer ------------------------------------------------


def test_an_incomplete_clause_is_marked_in_its_fence_and_the_boundary_covers_it() -> None:
    plain = SourceClause(S6DA_2, "using the formula:")
    marked = SourceClause(S6DA_2, "using the formula:", incomplete=(FORMULA_NOT_TEXT,))
    assert marked.marker == (
        f"clause {S6DA_2} incomplete (the formula is not available in text form)"
    )
    assert fence("retrieved_clauses", [plain]) != fence("retrieved_clauses", [marked])
    assert "] incomplete (" not in fence("retrieved_clauses", [plain])
    assert "must never state, guess or reconstruct the missing" in " ".join(SYSTEM_PROMPT.split())


async def _ask_the_gate_corpus(
    generator: FakeGenerator, question: str
) -> tuple[httpx.Response, list[tuple[str, dict[str, Any]]]]:  # Any: JSON
    """`test_api_answers`'s request, over the whole selected compilation (the
    committed parser output, so s6da/2 as the parser marks it) and Acme's
    policies, in the fake clause and retrieval stores."""
    ledger = await _ledger()
    clauses = FakeClauseStore()
    for clause in gate_clauses():
        await clauses.put_clause(clause)
    store = FakeRetrievalStore(gate_chunks(), ledger=ledger)
    return await _ask(generator, ledger=ledger, clauses=clauses, store=store, question=question)


TURNOVER = "How is annual turnover worked out for a business carried on for only part of a year?"


async def test_an_answer_citing_s6da_2_carries_the_flag_to_the_model_and_the_client() -> None:
    """End to end through the API: the fake graph's Track A scopes the law to
    GA-05's citation chain, which includes s6da/2; the real stores' fakes
    hold the committed corpus; the real verifier checks the quote against
    the stored text. The quote verifies, and the answer still says the clause
    isn't all of the law."""
    text = next(c.canonical_text for c in gate_law_clauses() if c.clause_id == S6DA_2)
    generator = FakeGenerator(_reply((S6DA_2, text[:60]), answer="It is worked out by formula."))
    response, events = await _ask_the_gate_corpus(generator, TURNOVER)
    assert response.status_code == 200
    answer = dict(events)["answer"]
    assert answer["verified"] is True
    [citation] = answer["citations"]
    assert citation["clause_id"] == S6DA_2 and citation["canonical_text"] == text
    reasons = [{"reason": "image_not_text", "detail": "the formula is not available in text form"}]
    assert citation["incomplete"] == reasons
    assert answer["incomplete"] == [{"clause_id": S6DA_2, "cite": "s 6DA(2)", "reasons": reasons}]
    # The model was told, in the fence, beside the clause.
    user = str(generator.calls[0]["user"])
    assert f"clause {S6DA_2} incomplete (the formula is not available in text form)]" in user
    assert ALT not in user


async def test_an_answer_citing_only_complete_clauses_carries_no_flag() -> None:
    app11 = "privacy-act-1988:C2026C00227:sch1/app11/1"
    text = next(c.canonical_text for c in gate_law_clauses() if c.clause_id == app11)
    _, events = await _ask_the_gate_corpus(FakeGenerator(_reply((app11, text[:60]))), QUESTION)
    answer = dict(events)["answer"]
    assert answer["verified"] is True and answer["incomplete"] == []
    assert answer["citations"][0]["incomplete"] == []


# ---- Track A (held: it changes signed labels GA-01, GA-02, GA-04, GA-05) -------

THE_THREE = (
    "COND-PA-TURNOVER-PREVIOUS-YEAR-UNDER",
    "COND-PA-TURNOVER-CURRENT-YEAR-UNDER",
    "COND-PA-S6D4A-EVER-OVER-THRESHOLD",
)
REASON = "s 6DA(2) is incomplete: the formula is not available in text form"


@pytest.mark.parametrize("condition_id", THE_THREE)
def test_each_small_business_condition_on_s6da_2_is_unknown_whatever_the_evidence(
    condition_id: str,
) -> None:
    """Derived from the stored clause's state, not from the signed mapping
    (which is unchanged): GA-04 answers every turnover question, and each
    of the three is still unknown, with no question that would settle it."""
    from compliance_rag.assessment.coverage import Truth
    from compliance_rag.assessment.evidence import Facts
    from compliance_rag.eval.applicability import load_applicability_set
    from compliance_rag.graph.applicability import explain
    from compliance_rag.graph.mappings import load_mapping_set
    from compliance_rag.graph.track_a import snapshot_of_mappings
    from tests.test_applicability_gate import CORPUS_MAPPINGS, stored_incomplete

    labels = load_applicability_set()
    case = next(c for c in labels.cases if c.id == "GA-04")
    mappings = load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml")))
    assert S6DA_2 in next(c for c in mappings.conditions if c.id == condition_id).clause_ids
    snapshot = snapshot_of_mappings(
        mappings,
        case.as_of,
        clause_validity=labels.clause_validity(set(mappings.clause_ids)),
        incomplete=stored_incomplete(),
    )
    found = explain(snapshot, Facts.of(case.evidence()), condition_id)
    assert found.truth is Truth.UNKNOWN and found.question is None
    assert found.incomplete == ((S6DA_2, "the formula is not available in text form"),)


async def test_a_small_business_whose_exemption_rests_on_s6da_2_is_unassessable() -> None:
    """GA-04: every question answered no, a small business operator under the
    signed labels. With s6da/2 incomplete, the APPs are unassessable, and the
    reason names the clause and what is missing."""
    from compliance_rag.assessment.coverage import Outcome
    from compliance_rag.assessment.evidence import Facts
    from compliance_rag.eval.applicability import load_applicability_set
    from compliance_rag.graph.mappings import load_mapping_set
    from tests.test_applicability_gate import CORPUS_MAPPINGS, fake_for

    labels = load_applicability_set()
    case = next(c for c in labels.cases if c.id == "GA-04")
    store = fake_for(load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), labels)
    coverage = await store.coverage(
        Facts.of(case.evidence()), tenant_id=case.tenant_id, as_of=case.as_of
    )
    app11 = coverage.obligation("OB-PA-APP11-1")
    assert app11.outcome is Outcome.UNASSESSABLE
    assert app11.reason == f"Unassessable: {REASON}"
    assert coverage.questions == ()
