"""The policy parser: synthetic Acme policies to clause records, Markdown
and PDF (issue #20).

Markdown and PDF are two different mechanisms for the same document
(`CONTEXT.md`: Position map) — never joined. Canonical text always comes
from Markdown; a PDF companion only ever adds `page`/`char_start`/`char_end`
click-to-clause metadata on top of the same Markdown-sourced text, never
its own extracted text, because PDF extraction fabricates whitespace and
would make the verifier fail against its own corpus (D-02).

Tested against the real synthetic fixtures from issue #11
(`docs/research/fixtures/synthetic-policies/`), including the one document
checked in as both Markdown and PDF.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from compliance_rag.corpus.models import (
    CANONICALISATION_VERSION,
    PUBLIC_TENANT_ID,
    ClauseRecord,
    Scope,
    canonicalise,
)
from compliance_rag.corpus.policy_parser import (
    attach_pdf_positions,
    parse_policy,
    parse_policy_markdown,
)

FIXTURES = Path(__file__).parent.parent / "docs" / "research" / "fixtures" / "synthetic-policies"
INSTRUMENT_ID = "acme/privacy-policy"
VERSION_ID = "v1"
TENANT_ID = "acme"


@pytest.fixture(scope="module")
def privacy_policy_md() -> str:
    return (FIXTURES / "acme-privacy-policy.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def privacy_policy_pdf() -> bytes:
    return (FIXTURES / "acme-privacy-policy.pdf").read_bytes()


def _by_path(clauses: tuple[ClauseRecord, ...], clause_path: str) -> ClauseRecord:
    matches = [c for c in clauses if c.clause_path == clause_path]
    assert len(matches) == 1, f"expected exactly one clause at {clause_path!r}, found {matches}"
    return matches[0]


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------


def test_markdown_produces_clause_records_with_tenant_scope(privacy_policy_md: str) -> None:
    clauses = parse_policy_markdown(
        privacy_policy_md, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID, tenant_id=TENANT_ID
    )
    assert clauses
    for clause in clauses:
        assert clause.tenant_id == TENANT_ID
        assert clause.instrument_id == INSTRUMENT_ID
        assert clause.version_id == VERSION_ID
        assert clause.canonicalisation_version == CANONICALISATION_VERSION
        assert clause.canonical_text == canonicalise(clause.text)


def test_numbered_sections_and_subsections(privacy_policy_md: str) -> None:
    clauses = parse_policy_markdown(
        privacy_policy_md, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID, tenant_id=TENANT_ID
    )
    clause = _by_path(clauses, "1/1")
    assert "This policy applies to personal information" in clause.text
    assert _by_path(clauses, "1/2").text.startswith("If you have a complaint")
    assert "opt-in consent" in _by_path(clauses, "3/2").text


def test_no_clause_for_the_document_title_or_section_headings(privacy_policy_md: str) -> None:
    """Only numbered paragraphs are clauses — the document title and bare
    '## N. Heading' lines are containers, not citable units, matching
    #17's same rule for law's section headings."""
    clauses = parse_policy_markdown(
        privacy_policy_md, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID, tenant_id=TENANT_ID
    )
    all_text = " ".join(c.text for c in clauses)
    assert "Acme Pty Ltd — Privacy Policy" not in all_text
    assert not any(c.clause_path in ("1", "2", "3", "4", "5", "6") for c in clauses)


def test_policy_clauses_never_carry_the_public_tenant(privacy_policy_md: str) -> None:
    """`ClauseRecord`'s own validator already enforces this (`CONTEXT.md`:
    Tenant id) — this test proves the parser doesn't route around it."""
    with pytest.raises(ValueError, match="reserved"):
        parse_policy_markdown(
            privacy_policy_md,
            instrument_id="acme/privacy-policy",
            version_id=VERSION_ID,
            tenant_id=PUBLIC_TENANT_ID,
        )


def test_a_blank_line_inside_a_numbered_item_does_not_truncate_it() -> None:
    """A real bug, found by running this parser against every #11 fixture,
    not a hypothetical: the vendor policy's clause 2.1 ('The following
    vendors...') is followed by a blank line, then a bulleted sub-list
    (two vendor names), then another blank line, then 2.2. Flushing on
    every blank line silently dropped both vendor names — only a new
    heading or a new numbered line is a real clause boundary."""
    source = (FIXTURES / "acme-vendor-cross-border-transfer-policy.md").read_text(encoding="utf-8")
    clauses = parse_policy_markdown(
        source, instrument_id="acme/vendor-policy", version_id=VERSION_ID, tenant_id=TENANT_ID
    )
    clause = _by_path(clauses, "2/1")
    assert "CloudAnalytics Inc" in clause.text
    assert "GlobalSupport Ltd" in clause.text


def test_a_mis_numbered_line_fails_loudly_rather_than_trusting_the_heading() -> None:
    """`_SUBSECTION_RE` captures the line's own claimed section number as
    well as the heading's — a source where they disagree is malformed and
    must be rejected, not silently filed under whichever one the code
    happened to trust."""
    source = "## 2. Heading\n\n1.3 stray line claiming the wrong section\n"
    with pytest.raises(ValueError, match="section"):
        parse_policy_markdown(
            source, instrument_id="acme/x", version_id=VERSION_ID, tenant_id=TENANT_ID
        )


def test_a_tenant_prefixed_instrument_id_is_required_to_be_consistent(
    privacy_policy_md: str,
) -> None:
    """`ClauseRecord`'s existing prefix-consistency check (issue #4) — an
    instrument_id prefixed for a different tenant than the one given must
    fail, not silently ingest under the wrong tenant."""
    with pytest.raises(ValueError):
        parse_policy_markdown(
            privacy_policy_md,
            instrument_id="acme/privacy-policy",
            version_id=VERSION_ID,
            tenant_id="other-corp",
        )


# --------------------------------------------------------------------------
# PDF position capture
# --------------------------------------------------------------------------


def test_pdf_positions_are_captured_at_parse_time(
    privacy_policy_md: str, privacy_policy_pdf: bytes
) -> None:
    clauses = parse_policy_markdown(
        privacy_policy_md, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID, tenant_id=TENANT_ID
    )
    with_positions = attach_pdf_positions(clauses, privacy_policy_pdf)
    assert len(with_positions) == len(clauses)
    for clause in with_positions:
        assert clause.page is not None
        assert clause.char_start is not None
        assert clause.char_end is not None
        assert clause.char_end > clause.char_start
        # Position capture must not disturb anything else the acceptance
        # criteria name — checked directly on the PDF-positioned records,
        # not just inferred from model_copy's immutability guarantee.
        assert clause.tenant_id == TENANT_ID
        assert clause.instrument_id == INSTRUMENT_ID


def test_pdf_positions_never_change_the_canonical_text(
    privacy_policy_md: str, privacy_policy_pdf: bytes
) -> None:
    """Position capture is metadata only — text and canonical_text must be
    byte-identical to the Markdown-only parse, never re-derived from PDF
    extraction (`CONTEXT.md`: Position map, D-02)."""
    md_only = parse_policy_markdown(
        privacy_policy_md, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID, tenant_id=TENANT_ID
    )
    with_positions = attach_pdf_positions(md_only, privacy_policy_pdf)
    for before, after in zip(md_only, with_positions, strict=True):
        assert before.text == after.text
        assert before.canonical_text == after.canonical_text
        assert before.clause_path == after.clause_path


def test_the_same_document_in_both_formats_produces_the_same_canonical_text(
    privacy_policy_md: str, privacy_policy_pdf: bytes
) -> None:
    md_only = parse_policy(
        privacy_policy_md,
        instrument_id=INSTRUMENT_ID,
        version_id=VERSION_ID,
        tenant_id=TENANT_ID,
    )
    md_and_pdf = parse_policy(
        privacy_policy_md,
        instrument_id=INSTRUMENT_ID,
        version_id=VERSION_ID,
        tenant_id=TENANT_ID,
        pdf_bytes=privacy_policy_pdf,
    )
    assert [c.canonical_text for c in md_only] == [c.canonical_text for c in md_and_pdf]
    assert all(c.page is None for c in md_only)
    assert all(c.page is not None for c in md_and_pdf)


def _reconstruct(doc: pymupdf.Document, clause: ClauseRecord) -> str:
    """Read from `clause.page` onward, matching `attach_pdf_positions`'s own
    documented reconstruction rule — a cross-page clause's `char_end` can
    exceed its starting page's own text length by design."""
    assert clause.page is not None
    assert clause.char_start is not None
    assert clause.char_end is not None
    available = "".join(
        str(page.get_text())  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
        for page in doc[clause.page - 1 :]
    )
    return available[clause.char_start : clause.char_end]


def test_pdf_page_and_offsets_actually_locate_the_clause_text_in_the_pdf(
    privacy_policy_md: str, privacy_policy_pdf: bytes
) -> None:
    """Not just non-None — the captured offsets must genuinely point at
    text derived from the clause, proving `page`/`char_start`/`char_end`
    are real positions and not placeholders."""
    clauses = parse_policy_markdown(
        privacy_policy_md, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID, tenant_id=TENANT_ID
    )
    with_positions = attach_pdf_positions(clauses, privacy_policy_pdf)
    doc = pymupdf.open(stream=privacy_policy_pdf, filetype="pdf")
    try:
        clause = _by_path(with_positions, "1/1")
        located = _reconstruct(doc, clause)
        assert "".join(located.split()) == "".join(clause.text.split())
    finally:
        doc.close()


def test_pdf_offsets_correctly_reconstruct_a_clause_that_spans_a_page_break(
    privacy_policy_md: str, privacy_policy_pdf: bytes
) -> None:
    """Clause 3/2 straddles the real fixture's page 1/2 boundary — found by
    running this test against real PDF bytes, not asserted from a
    hand-picked example. Proves `char_end` extending past its starting
    page's own text length is handled, not truncated."""
    clauses = parse_policy_markdown(
        privacy_policy_md, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID, tenant_id=TENANT_ID
    )
    with_positions = attach_pdf_positions(clauses, privacy_policy_pdf)
    clause = _by_path(with_positions, "3/2")
    assert clause.page is not None
    assert clause.char_end is not None
    doc = pymupdf.open(stream=privacy_policy_pdf, filetype="pdf")
    try:
        starting_page_text = str(
            doc[clause.page - 1].get_text()  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
        )
        assert clause.char_end > len(starting_page_text)
        located = _reconstruct(doc, clause)
        assert "".join(located.split()) == "".join(clause.text.split())
    finally:
        doc.close()


def test_a_clause_not_present_in_the_pdf_fails_loudly() -> None:
    """A PDF that doesn't actually contain a clause's text must not
    silently attach a wrong or empty position."""
    clause = ClauseRecord.build(
        instrument_id=INSTRUMENT_ID,
        version_id=VERSION_ID,
        clause_path="1/1",
        text="this text does not appear in the pdf at all",
        scope=Scope.TENANT,
        tenant_id=TENANT_ID,
    )
    empty_pdf = pymupdf.open()
    empty_pdf.new_page()
    empty_pdf_bytes: bytes = empty_pdf.tobytes()  # pyright: ignore[reportUnknownMemberType]
    empty_pdf.close()

    with pytest.raises(ValueError, match="not found"):
        attach_pdf_positions((clause,), empty_pdf_bytes)
