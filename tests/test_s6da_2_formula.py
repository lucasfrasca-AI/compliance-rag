"""s 6DA(2)'s formula, completed from the PDF compilation against the signed
form (#338, ADR-0058).

The PDF supplies the words; `formula.yaml` (signed by Lucas Frasca,
2026-11-11) supplies the structure. CI reads the formula from the committed
cut of the PDF page (`privacy-act-1988-C2026C00227.pdf-page-62.json`), as it
reads the ePub's constructs from a committed cut. With the PDF on disk,
`PRIVACY_ACT_COMPILATION_PDF` turns on the test that the real PDF reads to
exactly that cut and the signed form; CI doesn't download it, as it doesn't
download the ePub.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest
from scripts.ingest_compilation import PDF_SHA256, SECOND_SOURCES, VERSION_DIR

from compliance_rag.adapters.fakes import FakeClauseStore, FakeLedgerStore
from compliance_rag.assessment.draft import DraftCitation
from compliance_rag.corpus.models import ClauseRecord, DocumentClass, canonical_sha256
from compliance_rag.corpus.parser import FORMULA_NOT_TEXT, parse_compilation
from compliance_rag.corpus.pdf_formula import (
    FormulaGeometryError,
    FormulaMismatch,
    PdfPage,
    PdfRule,
    PdfSpan,
    SignedFormula,
    UnsignedFormula,
    completion_from_page,
    extract_page,
    formula_completion,
    load_signed_formulas,
    parse_signed_formula_file,
)
from compliance_rag.corpus.revision import RevisedClause, RevisionMode, RevisionRecord
from compliance_rag.corpus.second_source import (
    ClauseCompletion,
    CompletionRefused,
    apply_completions,
)
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Outcome, Verifier

FIXTURES = Path(__file__).resolve().parents[1] / "docs" / "research" / "fixtures"
PAGE_CUT = FIXTURES / "privacy-act-1988-C2026C00227.pdf-page-62.json"
CONSTRUCTS = FIXTURES / "privacy-act-1988-C2026C00227.constructs.html"
FORMULA_FILE = VERSION_DIR / "formula.yaml"

S6DA_2 = "privacy-act-1988:C2026C00227:s6da/2"
LEAD_IN = (
    "However, if a business has been carried on for only part of a financial year, its annual "
    "turnover for the financial year is the amount worked out using the formula:"
)
SIGNED_FORMULA = (
    "Amount that would be the annual turnover of the business under subsection (1) if the "
    "part were a whole financial year \u00d7 (Number of days in the whole financial year "
    "\u00f7 Number of days in the part)"
)
SIGNED = f"{LEAD_IN} {SIGNED_FORMULA}"
"""The canonical text Lucas Frasca signed on 2026-11-11 (#338), verbatim."""


def signed() -> SignedFormula:
    (formula,) = load_signed_formulas(VERSION_DIR)
    return formula


def page_cut() -> PdfPage:
    return PdfPage.from_json(json.loads(PAGE_CUT.read_text(encoding="utf-8")))


def cut_completions() -> tuple[ClauseCompletion, ...]:
    """The version's completion, read from the committed page cut: what CI
    parses the compilation with."""
    return (completion_from_page(page_cut(), signed()),)


def _parse(completions: tuple[ClauseCompletion, ...] = ()) -> dict[str, ClauseRecord]:
    parsed = parse_compilation(
        CONSTRUCTS.read_text(encoding="utf-8"),
        instrument_id="privacy-act-1988",
        version_id="C2026C00227",
        completions=completions,
    )
    return {c.clause_path: c for c in parsed.clauses}


# ---- the signed form ------------------------------------------------------------------


def test_the_signed_form_is_the_text_lucas_frasca_signed_and_pins_the_pdf() -> None:
    formula = signed()
    assert formula.clause_id == S6DA_2
    assert formula.canonical_text == SIGNED
    assert (formula.source.sha256, formula.source.page) == (PDF_SHA256, 62)
    assert formula.convention.numerator == "above_the_bar"
    operator = formula.convention.operator
    assert (operator.font, operator.extracted, operator.reads_as) == (
        "SymbolMT",
        "\u00b4",
        "\u00d7",
    )
    assert formula.completes == FORMULA_NOT_TEXT
    # The version's pin and the signed form's agree.
    assert {"s6da/2": formula.source.sha256} == SECOND_SOURCES


def test_the_signed_form_names_the_model_as_author_and_a_person_as_reviewer() -> None:
    text = FORMULA_FILE.read_text(encoding="utf-8")
    assert re.search(r'^author: "model:claude-opus-5-5', text, re.MULTILINE)
    assert re.search(r'^reviewed_by: "Lucas Frasca"$', text, re.MULTILINE)
    assert re.search(r"^date: 2026-11-11$", text, re.MULTILINE)


def _variant(old: str, new: str) -> str:
    text = FORMULA_FILE.read_text(encoding="utf-8")
    assert old in text
    return text.replace(old, new, 1)


def test_a_pending_signed_form_is_refused() -> None:
    pending = _variant('reviewed_by: "Lucas Frasca"', 'reviewed_by: "PENDING — Lucas Frasca"')
    with pytest.raises(UnsignedFormula):
        parse_signed_formula_file(
            pending, instrument_id="privacy-act-1988", version_id="C2026C00227"
        )


def test_a_model_never_signs_the_form() -> None:
    reviewed = _variant('reviewed_by: "Lucas Frasca"', 'reviewed_by: "model:claude-opus-5-5"')
    with pytest.raises(ValueError, match="never a model"):
        parse_signed_formula_file(
            reviewed, instrument_id="privacy-act-1988", version_id="C2026C00227"
        )


def test_a_form_that_drops_a_block_is_refused() -> None:
    form = _variant("({numerator} \u00f7 {denominator})", "({numerator})")
    with pytest.raises(ValueError, match="names each of"):
        parse_signed_formula_file(form, instrument_id="privacy-act-1988", version_id="C2026C00227")


def test_a_form_for_another_versions_clause_is_refused() -> None:
    with pytest.raises(ValueError, match="not a clause of"):
        parse_signed_formula_file(
            FORMULA_FILE.read_text(encoding="utf-8"),
            instrument_id="privacy-act-1988",
            version_id="C2099C00001",
        )


# ---- the reading, from the committed page cut ----------------------------------------


def test_the_page_reads_as_the_signed_form() -> None:
    completion = completion_from_page(page_cut(), signed())
    assert completion.text == SIGNED_FORMULA
    assert completion.source.locator == "page 62"
    assert completion.source.sha256 == PDF_SHA256


def _moved(page: PdfPage, text: str, dy: float) -> PdfPage:
    """`page` with the span reading `text` moved down by `dy` points."""
    spans = tuple(
        replace(s, bbox=(s.x0, s.y0 + dy, s.x1, s.y1 + dy)) if s.text == text else s
        for s in page.spans
    )
    assert spans != page.spans
    return replace(page, spans=spans)


def test_a_numerator_and_denominator_swapped_by_geometry_are_refused() -> None:
    """The one mistake that would change a turnover figure: the bar test read
    the other way. Move the denominator above the bar and the numerator's two
    lines below it, and the words no longer match the signed blocks."""
    page = _moved(page_cut(), "Number of days in the part", -24.0)
    page = _moved(page, "Number of days in the", 24.0)
    page = _moved(page, "whole financial year", 24.0)
    with pytest.raises(FormulaMismatch, match="numerator"):
        completion_from_page(page, signed())


def test_a_line_that_straddles_the_bar_is_refused() -> None:
    page = _moved(page_cut(), "whole financial year", 6.0)
    with pytest.raises(FormulaGeometryError, match="straddles"):
        completion_from_page(page, signed())


def test_two_lines_of_a_block_in_another_order_are_refused() -> None:
    page = _moved(page_cut(), "Amount that would be the", 35.0)
    with pytest.raises(FormulaMismatch, match="left"):
        completion_from_page(page, signed())


def test_no_bar_or_two_bars_are_refused() -> None:
    cut = page_cut()
    (bar,) = [r for r in cut.rules if 120 < r.y < 170]
    without = replace(cut, rules=tuple(r for r in cut.rules if r is not bar))
    with pytest.raises(FormulaGeometryError, match="0 rule"):
        completion_from_page(without, signed())
    second = replace(cut, rules=(*cut.rules, PdfRule(bar.x0, bar.x1, bar.y + 4)))
    with pytest.raises(FormulaGeometryError, match="2 rule"):
        completion_from_page(second, signed())


def test_an_operator_in_another_font_or_glyph_is_refused() -> None:
    cut = page_cut()
    (operator,) = [s for s in cut.spans if s.font == "SymbolMT"]
    other_font = replace(
        cut,
        spans=tuple(
            replace(s, font="TimesNewRomanPSMT") if s is operator else s for s in cut.spans
        ),
    )
    with pytest.raises(FormulaGeometryError, match="0 span"):
        completion_from_page(other_font, signed())
    other_glyph = replace(
        cut, spans=tuple(replace(s, text="+") if s is operator else s for s in cut.spans)
    )
    with pytest.raises(FormulaGeometryError, match="reads '\\+'"):
        completion_from_page(other_glyph, signed())


def test_a_word_the_pdf_sets_otherwise_is_refused() -> None:
    cut = page_cut()
    edited = replace(
        cut,
        spans=tuple(
            replace(s, text="Number of days in a part")
            if s.text == "Number of days in the part"
            else s
            for s in cut.spans
        ),
    )
    with pytest.raises(FormulaMismatch, match="denominator"):
        completion_from_page(edited, signed())


def test_a_page_of_other_bytes_or_another_page_is_refused() -> None:
    with pytest.raises(FormulaMismatch, match="not the signed page"):
        completion_from_page(replace(page_cut(), sha256="0" * 64), signed())
    with pytest.raises(FormulaMismatch, match="not the signed page"):
        completion_from_page(replace(page_cut(), page=61), signed())


def test_other_pdf_bytes_are_refused_before_anything_is_read() -> None:
    with pytest.raises(FormulaMismatch, match="other bytes"):
        formula_completion(b"%PDF-1.7 not the compilation", signed())


def test_a_span_beside_the_fraction_is_refused() -> None:
    cut = page_cut()
    stray = PdfSpan(text="stray", font="TimesNewRomanPSMT", size=9.92, bbox=(440, 140, 460, 150))
    with pytest.raises(FormulaGeometryError, match="neither left of the operator"):
        completion_from_page(replace(cut, spans=(*cut.spans, stray)), signed())


# ---- the completion, applied to the parse ---------------------------------------------


def test_the_completed_parse_holds_the_signed_text_and_s6da_2_is_complete() -> None:
    before, after = _parse(), _parse(cut_completions())
    assert before["s6da/2"].canonical_text == LEAD_IN
    assert before["s6da/2"].incomplete == (FORMULA_NOT_TEXT,)
    assert after["s6da/2"].canonical_text == SIGNED
    assert after["s6da/2"].text == SIGNED
    assert after["s6da/2"].incomplete == ()
    # No other clause changes.
    assert {p for p in before if before[p] != after[p]} == {"s6da/2"}


def test_a_completion_of_a_clause_that_is_not_incomplete_is_refused() -> None:
    (completion,) = cut_completions()
    clauses = tuple(_parse(cut_completions()).values())
    with pytest.raises(CompletionRefused, match="not marked image_not_text"):
        apply_completions(clauses, (completion,))


def test_a_completion_that_comes_out_other_than_signed_is_refused() -> None:
    (completion,) = cut_completions()
    clauses = tuple(_parse().values())
    with pytest.raises(CompletionRefused, match="not the signed"):
        apply_completions(clauses, (replace(completion, canonical_text=f"{SIGNED}."),))
    elsewhere = replace(completion, source=replace(completion.source, clause_path="s6da/9"))
    with pytest.raises(CompletionRefused, match="no clause"):
        apply_completions(clauses, (elsewhere,))
    with pytest.raises(CompletionRefused, match="once"):
        apply_completions(clauses, (completion, completion))


# ---- ADR-0039's conservation, with a second source -------------------------------------

_P_BLOCK = re.compile(r"<p\b[^>]*>.*?</p>", re.DOTALL)


def test_every_word_of_the_completed_parse_is_the_epubs_or_the_pdfs() -> None:
    """ADR-0039's conservation test counts the ePub's operative words against
    the stored ones (tests/test_parser_full_compilation.py). With a second
    source, the stored words are the ePub's plus exactly the formula's: each
    of the PDF's words once, and the signed form's own characters (the times and divide signs and
    the brackets) and nothing else. And they are all on s6da/2."""
    before, after = _parse(), _parse(cut_completions())
    words = Counter(w for c in after.values() for w in c.text.split())
    epub = Counter(w for c in before.values() for w in c.text.split())
    added = words - epub
    assert epub - words == Counter()
    assert added == Counter(SIGNED_FORMULA.split())
    assert Counter(after["s6da/2"].text.split()) - Counter(before["s6da/2"].text.split()) == added
    # Take the signed form's marks away and what is left is the PDF's words.
    form_marks = SIGNED_FORMULA.replace("\u00d7 (", "").replace("\u00f7 ", "").removesuffix(")")
    unmarked = Counter(form_marks.split())
    formula_spans = [
        s for s in page_cut().spans if abs(s.size - 9.92) < 0.01 and s.font != "SymbolMT"
    ]
    assert unmarked == Counter(w for s in formula_spans for w in s.text.split())


# ---- the verifier ---------------------------------------------------------------------


async def _verifier(clause: ClauseRecord) -> Verifier:
    ledger = FakeLedgerStore()
    await ledger.record_version(
        tenant_id="_public",
        instrument_id="privacy-act-1988",
        version_id="C2026C00227",
        valid_from=dt.date(2026, 6, 4),
        supersedes=None,
        document_class=DocumentClass.LIBRARY_LAW,
        title="Privacy Act 1988",
    )
    clauses = FakeClauseStore()
    await clauses.put_clause(clause)
    return Verifier(clauses, ledger, CanonicalCitationVerifier())


async def test_the_verifier_verifies_a_quote_from_the_formula() -> None:
    """D-02's check, against the completed clause as the bucket will hold it:
    a quote from the formula verifies; one from the ePub's alt text, which is
    not the Act's words (ADR-0047), does not."""
    verifier = await _verifier(_parse(cut_completions())["s6da/2"])

    async def check(quote: str) -> Outcome:
        checked = await verifier.check(
            DraftCitation(clause_id=S6DA_2, quote=quote),
            context=frozenset({S6DA_2}),
            as_of=dt.date(2026, 11, 11),
        )
        return checked.outcome

    assert (
        await check("Number of days in the whole financial year \u00f7 Number of days in the part")
        is Outcome.VERIFIED
    )
    assert await check(f"the formula: {SIGNED_FORMULA[:60]}") is Outcome.VERIFIED
    assert await check("times start fraction Number of days") is Outcome.QUOTE_MISMATCH
    # Before the completion the same quote had nothing to match.
    before = await _verifier(_parse()["s6da/2"])
    checked = await before.check(
        DraftCitation(clause_id=S6DA_2, quote="Number of days in the part"),
        context=frozenset({S6DA_2}),
        as_of=dt.date(2026, 11, 11),
    )
    assert checked.outcome is Outcome.QUOTE_MISMATCH


# ---- the real PDF -----------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("PRIVACY_ACT_COMPILATION_PDF"),
    reason="PRIVACY_ACT_COMPILATION_PDF not set — the full 1.9 MB PDF compilation",
)
def test_the_real_pdf_reads_to_the_committed_cut_and_the_signed_form() -> None:
    raw = Path(os.environ["PRIVACY_ACT_COMPILATION_PDF"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PDF_SHA256, "a different PDF"
    assert extract_page(raw, page=62) == page_cut()
    assert formula_completion(raw, signed()) == completion_from_page(page_cut(), signed())
    tampered = raw[:-1] + bytes([raw[-1] ^ 1])
    with pytest.raises(FormulaMismatch, match="other bytes"):
        formula_completion(tampered, signed())


# ---- the record and the plan name the second source (ADR-0058) ------------------------


def _record(*, second: bool) -> RevisionRecord:
    (completion,) = cut_completions()
    return RevisionRecord(
        revision_id="r-338",
        mode=RevisionMode.CORRECT,
        instrument_id="privacy-act-1988",
        version_id="C2026C00227",
        supersedes=None,
        at="2026-11-11T00:00:00+00:00",
        by="Lucas Frasca",
        reason="#338",
        source_sha256="6d0a43e5" + "0" * 56,
        corpus_sha="c" * 40,
        corpus_tag=None,
        findings_citing=0,
        clauses=(
            RevisedClause(
                "s6da/2",
                canonical_sha256(LEAD_IN),
                canonical_sha256(SIGNED),
                (FORMULA_NOT_TEXT,),
                (),
            ),
        ),
        second_sources=(completion.source,) if second else (),
    )


def test_the_record_names_the_second_source_and_reads_back_the_same() -> None:
    record = _record(second=True)
    stored = json.loads(json.dumps(record.to_json()))
    assert stored["second_sources"] == {
        "s6da/2": {"sha256": PDF_SHA256, "url": signed().source.url, "locator": "page 62"}
    }
    assert stored["clauses"]["s6da/2"]["previous_incomplete"] == [
        {"reason": "image_not_text", "detail": "the formula is not available in text form"}
    ]
    assert RevisionRecord.from_json(stored) == record
    trail = json.loads(record.trail_entry(record.clauses[0]))
    assert trail["second_source"]["sha256"] == PDF_SHA256
    assert trail["previous"]["incomplete"] == stored["clauses"]["s6da/2"]["previous_incomplete"]


def test_a_record_with_one_source_reads_as_it_did_before() -> None:
    record = _record(second=False)
    stored = record.to_json()
    assert "second_sources" not in stored
    assert RevisionRecord.from_json(stored) == record
    assert "second_source" not in json.loads(record.trail_entry(record.clauses[0]))


def test_the_dry_run_names_the_second_source() -> None:
    from scripts.revise_compilation import render_plan

    from compliance_rag.corpus.revision import plan_revision

    before = {c.clause_path: c for c in _parse().values() if c.clause_path.startswith("s6da/")}
    fresh = tuple(c for p, c in _parse(cut_completions()).items() if p.startswith("s6da/"))
    plan = plan_revision(
        fresh,
        source_html="s",
        annotated_html="a",
        stored_version_id="C2026C00227",
        stored_manifest={p: canonical_sha256(c.canonical_text) for p, c in before.items()},
        stored_texts={"s6da/2": LEAD_IN},
        stored_source_html_sha256="a",
        stored_incomplete={"s6da/2": (FORMULA_NOT_TEXT,)},
        second_sources=tuple(c.source for c in cut_completions()),
    )
    assert [c.clause_path for c in plan.changed] == ["s6da/2"]
    table = render_plan(plan)
    assert "1 changed, 0 added, 0 removed" in table
    assert f"second source for s6da/2: sha256 {PDF_SHA256}, page 62 of https://" in table
    assert "image_not_text: the formula is not available in text form) -> complete" in table
