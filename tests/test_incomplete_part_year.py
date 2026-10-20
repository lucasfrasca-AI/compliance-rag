"""Track A over an incomplete clause, narrowed to a part year (#211, ADR-0047),
and the signed reading that narrows it (`incomplete.yaml`).

s6DA(2)'s formula is an image, so the clause is incomplete. The three signed
small-business conditions that cite it read a tenant-supplied turnover figure,
and s6DA(2) governs that figure only for a financial year the business was
carried on for part of. So, for the year a leaf reads (the human's decision 2
on #211):

- **part year established** (the business started during it): unassessable,
  and no answer settles it;
- **open** (when it started isn't known): unassessable, and asked;
- **full year**: decided from the figure, as before.

The reading that says so is a reviewed artefact, never the signed mapping,
and it also carries the words a reader is shown (decision 3).
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.assessment.coverage import Explanation, Outcome, Truth
from compliance_rag.assessment.evidence import Facts
from compliance_rag.corpus.incomplete_review import (
    INSTRUMENTS_ROOT,
    IncompleteReviews,
    UnreviewedIncompleteness,
    committed_incomplete_reviews,
    load_incomplete_reviews,
    parse_incomplete_review,
)
from compliance_rag.corpus.models import IncompleteReason
from compliance_rag.corpus.parser import FORMULA_NOT_TEXT, TABLE_NOT_TEXT
from compliance_rag.eval.applicability import GoldenFact, load_applicability_set
from compliance_rag.eval.compilation_clauses import load_compilation_clauses
from compliance_rag.graph.applicability import Snapshot, explain
from compliance_rag.graph.mappings import load_mapping_set
from compliance_rag.graph.track_a import snapshot_of_mappings
from tests.test_applicability_gate import CORPUS_MAPPINGS, fake_for, graph_for, stored_incomplete
from tests.test_neo4j_graph import graph as graph
from tests.test_neo4j_graph import needs_neo4j

S6DA_2 = "privacy-act-1988:C2026C00227:s6da/2"
REVIEW = INSTRUMENTS_ROOT / "privacy-act-1988" / "C2026C00227" / "incomplete.yaml"
SIGNED = (
    "the turnover formula in s 6DA(2) is published as an image in the compilation "
    "and has no text form in our corpus"
)
PARSER = "the formula is not available in text form"
GAP = ((S6DA_2, SIGNED),)

PREVIOUS = "COND-PA-TURNOVER-PREVIOUS-YEAR-UNDER"
CURRENT = "COND-PA-TURNOVER-CURRENT-YEAR-UNDER"
EVER = "COND-PA-S6D4A-EVER-OVER-THRESHOLD"
THE_THREE = (PREVIOUS, CURRENT, EVER)

AS_OF = dt.date(2026, 10, 12)
"""The golden set's date: the current financial year is FY2026-27, the
previous FY2025-26."""
FULL_YEAR_START = dt.date(2015, 3, 2)
"""Acme's start: long before either year the two single-year leaves read."""


def _snapshot(reviews: IncompleteReviews | None = None) -> Snapshot:
    labels = load_applicability_set()
    mappings = load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml")))
    snapshot = snapshot_of_mappings(
        mappings,
        AS_OF,
        clause_validity=labels.clause_validity(set(mappings.clause_ids)),
        incomplete=stored_incomplete(),
    )
    return snapshot if reviews is None else replace(snapshot, reviews=reviews)


def _facts(started: dt.date | None, turnover: dict[int, int]) -> Facts:
    """GA-04's answered profile, with when it started and its turnover each
    financial year (`2025` is FY2025-26) replaced."""
    case = next(c for c in load_applicability_set().cases if c.id == "GA-04")
    kept = [f for f in case.facts if f.field not in {"business_started_on", "annual_turnover_aud"}]
    if started is not None:
        kept.append(GoldenFact(field="business_started_on", value=started, since=started))
    kept += [
        GoldenFact(field="annual_turnover_aud", value=v, financial_year=y)
        for y, v in turnover.items()
    ]
    return Facts.of(case.model_copy(update={"facts": tuple(kept)}).evidence())


def _explain(condition_id: str, started: dt.date | None, turnover: dict[int, int]) -> Explanation:
    return explain(_snapshot(), _facts(started, turnover), condition_id)


EVERY_YEAR_UNDER = {year: 2_000_000 for year in range(2001, 2026)}
"""A figure under $3,000,000 for every financial year s6D(4)(a) can read."""
UNDER_SINCE_2014 = {y: v for y, v in EVERY_YEAR_UNDER.items() if y >= 2014}
"""The same, from FY2014-15, the year Acme started in."""


def test_the_three_conditions_cite_s6da_2_and_the_signed_mapping_says_nothing_of_part_years() -> (
    None
):
    """The narrowing is derived from the clause's state and its reviewed
    reading, never written into the signed mapping."""
    mappings = load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml")))
    by_id = {c.id: c for c in mappings.conditions}
    for condition_id in THE_THREE:
        assert S6DA_2 in by_id[condition_id].clause_ids
    fields = {leaf.field for c in THE_THREE if (leaf := by_id[c].leaf) is not None}
    assert fields == {"annual_turnover_aud"}
    assert "part_year" not in (CORPUS_MAPPINGS / "privacy-act-1988.yaml").read_text()


# ---- the previous financial year (s6D(1)) -------------------------------------------


def test_previous_year_full_year_is_decided_from_the_figure() -> None:
    under = _explain(PREVIOUS, FULL_YEAR_START, {2025: 2_500_000})
    over = _explain(PREVIOUS, FULL_YEAR_START, {2025: 4_000_000})
    assert (under.truth, over.truth) == (Truth.TRUE, Truth.FALSE)
    assert under.incomplete == over.incomplete == ()


def test_previous_year_a_start_on_1_july_is_a_full_year() -> None:
    found = _explain(PREVIOUS, dt.date(2025, 7, 1), {2025: 2_500_000})
    assert found.truth is Truth.TRUE and found.incomplete == ()


def test_previous_year_part_year_established_is_unassessable_and_nothing_settles_it() -> None:
    """Started 1 September 2025, during FY2025-26: s6DA(2) sets that year's
    turnover, whatever figure is given."""
    found = _explain(PREVIOUS, dt.date(2025, 9, 1), {2025: 2_500_000})
    assert found.truth is Truth.UNKNOWN and found.question is None
    assert found.incomplete == GAP


def test_previous_year_open_is_unassessable_and_asks_when_it_started() -> None:
    found = _explain(PREVIOUS, None, {2025: 2_500_000})
    assert found.truth is Truth.UNKNOWN and found.incomplete == GAP
    assert found.question is not None and found.question.field == "business_started_on"
    assert "FY2025-26" in found.question.question


def test_previous_year_open_with_no_figure_asks_for_both() -> None:
    found = _explain(PREVIOUS, None, {})
    assert found.truth is Truth.UNKNOWN and found.incomplete == GAP
    assert found.question is not None
    assert (found.question.field, found.question.also_needs) == (
        "annual_turnover_aud",
        ("business_started_on",),
    )


# ---- the current financial year (s6D(2)) --------------------------------------------


def test_current_year_full_year_is_decided_from_the_figure() -> None:
    found = _explain(CURRENT, FULL_YEAR_START, {2026: 1_000_000})
    assert found.truth is Truth.TRUE and found.incomplete == ()


def test_current_year_part_year_established_is_unassessable_and_nothing_settles_it() -> None:
    """s6D(2)'s own case: not carried on last year, started this one."""
    found = _explain(CURRENT, dt.date(2026, 8, 1), {2026: 1_000_000})
    assert found.truth is Truth.UNKNOWN and found.question is None
    assert found.incomplete == GAP


def test_current_year_open_is_unassessable_and_asks_when_it_started() -> None:
    found = _explain(CURRENT, None, {2026: 1_000_000})
    assert found.truth is Truth.UNKNOWN and found.incomplete == GAP
    assert found.question is not None and found.question.field == "business_started_on"
    assert "FY2026-27" in found.question.question


# ---- every year since it started (s6D(4)(a)) -----------------------------------------


def test_ever_full_years_only_are_decided_from_the_figures() -> None:
    """Started on 1 July 2015: its first year is a full year, so every year is
    read from its figure."""
    start = dt.date(2015, 7, 1)
    assert _explain(EVER, start, UNDER_SINCE_2014).truth is Truth.FALSE
    assert _explain(EVER, start, UNDER_SINCE_2014 | {2023: 3_200_000}).truth is Truth.TRUE
    assert _explain(EVER, start, UNDER_SINCE_2014).incomplete == ()


def test_ever_part_year_established_the_start_year_is_never_read_from_its_figure() -> None:
    """Started 2 March 2015 (Acme): FY2014-15 is a part year. Every other year
    known and under, so no answer settles it — even the start year's own
    figure over the threshold decides nothing (what s6DA(2) makes of it isn't
    in the corpus)."""
    start = FULL_YEAR_START
    found = _explain(EVER, start, UNDER_SINCE_2014)
    assert found.truth is Truth.UNKNOWN and found.question is None
    assert found.incomplete == GAP
    over_in_start_year = _explain(EVER, start, UNDER_SINCE_2014 | {2014: 5_000_000})
    assert over_in_start_year.truth is Truth.UNKNOWN and over_in_start_year.incomplete == GAP


def test_ever_part_year_established_a_full_year_over_still_decides() -> None:
    """GA-05's case: over in FY2023-24, a full year."""
    found = _explain(EVER, FULL_YEAR_START, UNDER_SINCE_2014 | {2023: 3_200_000})
    assert found.truth is Truth.TRUE and found.incomplete == ()


def test_ever_part_year_established_with_a_full_year_missing_asks_for_that_year_only() -> None:
    years = {y: v for y, v in UNDER_SINCE_2014.items() if y != 2020}
    found = _explain(EVER, FULL_YEAR_START, years)
    assert found.truth is Truth.UNKNOWN and found.incomplete == GAP
    assert found.question is not None
    assert found.question.missing_periods == ("FY2020-21",)
    assert found.question.also_needs == ()


def test_ever_open_is_unassessable_and_asks_when_it_started() -> None:
    """With no start date every year might be the one it started in."""
    found = _explain(EVER, None, EVERY_YEAR_UNDER)
    assert found.truth is Truth.UNKNOWN and found.incomplete == GAP
    assert found.question is not None
    assert found.question.also_needs == ("business_started_on",)
    assert _explain(EVER, None, EVERY_YEAR_UNDER | {2023: 3_200_000}).truth is Truth.UNKNOWN


def test_ever_a_start_before_s6d_commenced_reads_only_full_years() -> None:
    """Started in 2000: the first year s6D(4)(a) reads (FY2001-02, ended after
    21 December 2001) was carried on throughout."""
    found = _explain(EVER, dt.date(2000, 3, 1), EVERY_YEAR_UNDER)
    assert found.truth is Truth.FALSE and found.incomplete == ()


# ---- with no reviewed reading, every reading is unassessable (ADR-0047) ----------------


@pytest.mark.parametrize("condition_id", THE_THREE)
def test_without_a_reviewed_reading_each_condition_is_unknown_whatever_the_evidence(
    condition_id: str,
) -> None:
    """The general state: missing content nobody has read governs every
    reading, and the reason is the parser's words."""
    found = explain(
        _snapshot(IncompleteReviews()),
        _facts(dt.date(2015, 7, 1), UNDER_SINCE_2014 | {2026: 1_000_000}),
        condition_id,
    )
    assert found.truth is Truth.UNKNOWN and found.question is None
    assert found.incomplete == ((S6DA_2, PARSER),)


# ---- the reason Track A gives ----------------------------------------------------------


async def test_ga_04s_reason_is_the_signed_wording_and_no_question_is_asked() -> None:
    """GA-04: started 2 March 2015, so s6D(4)(a)'s first year is a part year."""
    labels = load_applicability_set()
    case = next(c for c in labels.cases if c.id == "GA-04")
    store = fake_for(load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), labels)
    coverage = await store.coverage(
        Facts.of(case.evidence()), tenant_id=case.tenant_id, as_of=case.as_of
    )
    app11 = coverage.obligation("OB-PA-APP11-1")
    assert app11.outcome is Outcome.UNASSESSABLE
    assert app11.reason == f"Unassessable: s 6DA(2) is incomplete: {SIGNED}"
    assert coverage.questions == ()


@needs_neo4j
async def test_on_neo4j_ga_04s_reason_is_the_signed_wording_too(graph: Neo4jGraph) -> None:
    """The graph's Clause node carries the parser's state; the reading is
    joined by the same evaluator."""
    labels = load_applicability_set()
    case = next(c for c in labels.cases if c.id == "GA-04")
    store = await graph_for(graph, load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), labels)
    coverage = await store.coverage(
        Facts.of(case.evidence()), tenant_id=case.tenant_id, as_of=case.as_of
    )
    app11 = coverage.obligation("OB-PA-APP11-1")
    assert app11.reason == f"Unassessable: s 6DA(2) is incomplete: {SIGNED}"
    assert coverage.questions == ()


async def test_ga_01_a_full_year_over_the_threshold_still_applies() -> None:
    labels = load_applicability_set()
    case = next(c for c in labels.cases if c.id == "GA-01")
    store = fake_for(load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), labels)
    coverage = await store.coverage(
        Facts.of(case.evidence()), tenant_id=case.tenant_id, as_of=case.as_of
    )
    assert coverage.obligation("OB-PA-APP11-1").outcome is Outcome.APPLICABLE


# ---- the reviewed file ------------------------------------------------------------------


def test_the_committed_reading_is_signed_and_says_what_lucas_frasca_signed() -> None:
    review = parse_incomplete_review(
        REVIEW.read_text(encoding="utf-8"),
        instrument_id="privacy-act-1988",
        version_id="C2026C00227",
    )
    assert (review.author, review.reviewed_by, review.date) == (
        "Lucas Frasca",
        "Lucas Frasca",
        "2026-10-20",
    )
    assert review.source_clause_ids == (S6DA_2,)
    [entry] = review.clauses
    assert (entry.clause_id, entry.reason, entry.shown) == (
        S6DA_2,
        IncompleteReason.IMAGE_NOT_TEXT,
        SIGNED,
    )
    assert (
        entry.part_year_only is not None and entry.part_year_only.started == "business_started_on"
    )


def test_every_reading_is_of_a_reason_the_parser_marks_and_a_part_year_one_says_so() -> None:
    """A reading for a clause the corpus holds complete would be dead, and a
    part-year reading must rest on the clause's own words; the fact it names
    must be one the signed mapping reads."""
    clauses = {c.clause_id: c for c in load_compilation_clauses().clauses}
    mapping = (CORPUS_MAPPINGS / "privacy-act-1988.yaml").read_text()
    entries = committed_incomplete_reviews().entries
    assert entries
    for (clause_id, reason), entry in entries.items():
        assert reason in {i.reason for i in clauses[clause_id].incomplete}
        if entry.part_year_only is not None:
            assert "carried on for only part of a financial year" in clauses[clause_id].text
            assert f"field: {entry.part_year_only.started}" in mapping


def _copy_review(tmp_path: Path, text: str) -> Path:
    folder = tmp_path / "privacy-act-1988" / "C2026C00227"
    folder.mkdir(parents=True)
    (folder / "incomplete.yaml").write_text(text, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("signed", "pending"),
    [
        ('reviewed_by: "Lucas Frasca"', 'reviewed_by: "PENDING — Lucas Frasca"'),
        ("date: 2026-10-20", 'date: "PENDING"'),
    ],
)
def test_a_reading_pending_review_is_refused_not_used(
    tmp_path: Path, signed: str, pending: str
) -> None:
    text = REVIEW.read_text(encoding="utf-8")
    assert signed in text
    root = _copy_review(tmp_path, text.replace(signed, pending))
    with pytest.raises(UnreviewedIncompleteness, match="awaiting review"):
        load_incomplete_reviews(root)


def test_the_committed_file_loads_from_a_copy(tmp_path: Path) -> None:
    root = tmp_path / "instruments"
    shutil.copytree(INSTRUMENTS_ROOT, root)
    assert load_incomplete_reviews(root) == committed_incomplete_reviews()


def test_a_model_never_reviews_a_reading(tmp_path: Path) -> None:
    text = REVIEW.read_text(encoding="utf-8").replace(
        'reviewed_by: "Lucas Frasca"', 'reviewed_by: "model: Claude"'
    )
    with pytest.raises(ValidationError, match="never a model"):
        load_incomplete_reviews(_copy_review(tmp_path, text))


def test_a_reading_of_another_versions_clause_is_refused(tmp_path: Path) -> None:
    text = REVIEW.read_text(encoding="utf-8").replace("C2026C00227:s6da/2", "C2099C00001:s6da/2")
    with pytest.raises(ValueError, match="not a clause of"):
        load_incomplete_reviews(_copy_review(tmp_path, text))


def test_source_clause_ids_must_be_the_clauses_read(tmp_path: Path) -> None:
    text = REVIEW.read_text(encoding="utf-8").replace(
        "source_clause_ids:\n  - privacy-act-1988:C2026C00227:s6da/2",
        "source_clause_ids:\n  - privacy-act-1988:C2026C00227:s6da/1",
    )
    with pytest.raises(ValidationError, match="are not the clauses read"):
        load_incomplete_reviews(_copy_review(tmp_path, text))


@pytest.mark.parametrize(
    "shown",
    [
        "the formula] is an image",  # would close the fence marker
        "the formula; an image",  # would read as two reasons
        'the "formula"',
        "the formula (in s 6DA(2) is an image",  # unbalanced
        "the formula\nis an image",
        "",
    ],
)
def test_what_a_reader_is_shown_cant_reshape_the_fence_marker(tmp_path: Path, shown: str) -> None:
    text = REVIEW.read_text(encoding="utf-8").replace(f'"{SIGNED}"', json.dumps(shown))
    with pytest.raises(ValidationError):
        load_incomplete_reviews(_copy_review(tmp_path, text))


def test_the_signed_words_stand_in_only_for_the_reason_they_were_signed_for() -> None:
    reviews = committed_incomplete_reviews()
    assert reviews.shown(S6DA_2, FORMULA_NOT_TEXT) == SIGNED
    # Another reason on the same clause, or an unread clause, keeps the parser's.
    assert reviews.shown(S6DA_2, TABLE_NOT_TEXT) == TABLE_NOT_TEXT.detail
    s16a = "privacy-act-1988:C2026C00227:s16a/1"
    assert reviews.shown(s16a, TABLE_NOT_TEXT) == TABLE_NOT_TEXT.detail
    assert reviews.shown_json(s16a, (TABLE_NOT_TEXT,)) == [
        {"reason": "table_not_text", "detail": TABLE_NOT_TEXT.detail}
    ]
