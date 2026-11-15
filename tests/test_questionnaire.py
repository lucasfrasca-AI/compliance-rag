"""The questionnaire's derivation (issue #304, #123, ADR-0053 I5): what is
asked is Track A's unknown leaves and nothing else, one question per fact, for
the period the law reads, grouped by the provision it decides.

Run over the real Privacy Act mappings through the fake graph (the same
evaluator the graph uses), on a fixed date, and over every golden case's
facts: a derivation tested on one profile is not tested (CLAUDE.md).
"""

from __future__ import annotations

import datetime as dt

import pytest

from compliance_rag.adapters.fakes import FakeGraphStore
from compliance_rag.assessment.coverage import (
    AnswerKind,
    Coverage,
    Explanation,
    ObligationCoverage,
    Outcome,
    Question,
    Truth,
    question_json,
    question_of_json,
)
from compliance_rag.assessment.evidence import (
    Evidence,
    EvidenceProvenance,
    Facts,
    Period,
)
from compliance_rag.assessment.questionnaire import (
    NO_ROUTE,
    STATE,
    Questionnaire,
    SlotStatus,
    answer_kind_of,
    answer_period,
    build,
    on_record,
    route_of,
    year_key,
    year_of_key,
)
from compliance_rag.eval.applicability import ApplicabilityCase, load_applicability_set
from compliance_rag.graph.mappings import load_mapping_set
from tests.test_applicability_gate import CORPUS_MAPPINGS, fake_for

TENANT = "acme"
AS_OF = dt.date(2026, 10, 12)
"""Financial year FY2026-27; the previous one is FY2025-26."""
SINCE_2000 = Period(dt.date(2000, 1, 1))


def graph() -> FakeGraphStore:
    return fake_for(
        load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), load_applicability_set()
    )


def fact(
    n: int,
    field: str,
    value: object,
    period: Period = SINCE_2000,
    *,
    provenance: EvidenceProvenance = EvidenceProvenance.ASSERTED,
    supersedes: str | None = None,
) -> Evidence:
    return Evidence(
        tenant_id=TENANT,
        evidence_id=f"e{n}",
        field=field,
        value=value,  # pyright: ignore[reportArgumentType] — a test hands over any fact
        period=period,
        provenance=provenance,
        source=f"test {n}",
        asserted_by="admin@acme.example",
        supersedes=supersedes,
        trace_id="1" * 32,
    )


async def asked(*evidence: Evidence, as_of: dt.date = AS_OF) -> tuple[Questionnaire, Coverage]:
    coverage = await graph().coverage(Facts.of(evidence), tenant_id=TENANT, as_of=as_of)
    return build(coverage, evidence), coverage


def fields(q: Questionnaire) -> set[str]:
    return {i.field for i in q.items}


def routes(q: Questionnaire) -> dict[str, list[str]]:
    return {r.label: [i.field for i in r.items] for r in q.routes}


# ---- the questions are Track A's, and only Track A's ---------------------------------------


async def test_a_workspace_with_no_evidence_is_asked_every_fact_track_a_names() -> None:
    q, coverage = await asked()
    track_a = {question.field for question in coverage.questions}
    # beside them, only a fact a question says it also needs
    also = {f for question in coverage.questions for f in question.also_needs}
    assert track_a <= fields(q) <= track_a | also
    assert q.unanswerable == () and len(coverage.with_outcome(Outcome.UNASSESSABLE)) == 37
    # every condition Track A asks about is shown, under its fact
    assert {c for i in q.items for c in (a.condition_id for a in i.asked)} >= {
        question.condition_id for question in coverage.questions
    }


async def test_each_fact_is_asked_once_however_many_conditions_read_it() -> None:
    q, coverage = await asked()
    assert len(q.items) == len(fields(q))
    (legal_form,) = [i for i in q.items if i.field == "legal_form"]
    # s6C(1)(a) (organisation) and s6D(9) (a body corporate) both read it
    assert {a.condition_id for a in legal_form.asked} >= {
        "COND-PA-LEGAL-FORM",
        "COND-PA-BODY-CORPORATE",
    }
    assert legal_form.answer_kind is AnswerKind.TEXT
    assert "body_corporate" in legal_form.options and "trust" in legal_form.options
    assert len(legal_form.obligation_ids) == len(coverage.with_outcome(Outcome.UNASSESSABLE)) - 2


async def test_a_leaf_a_decided_sibling_short_circuits_is_never_asked() -> None:
    """s6D(4) is true if any of (a)-(f) is: once one holds, the rest decide
    nothing and Track A keeps no question for them."""
    before, _ = await asked()
    s6d4 = [
        "provides_health_service",
        "holds_health_information_outside_employee_records",
        "discloses_personal_information_for_benefit",
        "provides_benefit_to_collect_personal_information",
        "commonwealth_contracted_service_provider",
    ]
    assert set(s6d4) <= fields(before)
    after, coverage = await asked(fact(1, "credit_reporting_body", True))
    assert fields(after).isdisjoint([*s6d4, "credit_reporting_body"])
    # and what is left is exactly what Track A still asks
    assert {q.field for q in coverage.questions} <= fields(after)
    assert fields(after) <= {q.field for q in coverage.questions} | {
        f for q in coverage.questions for f in q.also_needs
    }
    assert "s 6D(4)" not in routes(after)


async def test_a_leaf_a_false_all_of_short_circuits_is_never_asked() -> None:
    """An entity that is not a body corporate (a trust) can't be a related
    body corporate (s6D(9)), so that is not asked."""
    before, _ = await asked()
    assert "related_body_corporate_not_small" in fields(before)
    after, coverage = await asked(fact(1, "legal_form", "trust"))
    asked_after = {q.field for q in coverage.questions}
    assert "related_body_corporate_not_small" not in asked_after
    assert "related_body_corporate_not_small" not in fields(after)


@pytest.mark.parametrize("case", load_applicability_set().cases, ids=lambda c: c.id)
async def test_every_golden_profile_is_asked_what_track_a_asks_and_nothing_is_dropped(
    case: ApplicabilityCase,
) -> None:
    """Each real profile in the golden set: every question Track A keeps is
    placed under a fact, and no fact is asked that Track A, or a question's
    own `also_needs`, doesn't name."""
    evidence = case.evidence()
    coverage = await graph().coverage(Facts.of(evidence), tenant_id=TENANT, as_of=case.as_of)
    q = build(coverage, evidence)
    placed = {a.condition_id for i in q.items for a in i.asked}
    assert {question.condition_id for question in coverage.questions} <= placed
    named = {question.field for question in coverage.questions} | {
        f for question in coverage.questions for f in question.also_needs
    }
    assert fields(q) <= named
    # an unassessable obligation that no answer settles is said so, not asked about
    no_question = {
        o.obligation_id for o in coverage.with_outcome(Outcome.UNASSESSABLE) if not o.questions
    }
    assert {o.obligation_id for o in q.unanswerable} == no_question
    for item in q.items:
        assert item.slots and all(s.status is not None for s in item.slots)


# ---- grouped by the route they decide ----------------------------------------------------


async def test_the_questions_are_grouped_by_the_provision_they_decide() -> None:
    q, _ = await asked()
    grouped = routes(q)
    # the four routes the issue names, each with the facts that decide it
    assert grouped["s 6D(4)"] == [
        "provides_health_service",
        "holds_health_information_outside_employee_records",
        "discloses_personal_information_for_benefit",
        "provides_benefit_to_collect_personal_information",
        "commonwealth_contracted_service_provider",
        "credit_reporting_body",
    ]
    assert grouped["s 6D(9)"] == ["related_body_corporate_not_small"]
    assert grouped["s 6E(1)"] == ["prescribed_under_s6e_1"]
    assert grouped["s 6EA(1)"] == ["opted_in_under_s6ea"]
    # in the order a reader finds them in the Act
    labels = list(grouped)
    assert labels.index("s 6D(4)") < labels.index("s 6D(9)") < labels.index("s 6E(1)")
    assert labels.index("s 6E(1)") < labels.index("s 6E(1C)") < labels.index("s 6EA(1)")


def test_a_route_is_the_nearest_provision_not_a_definition() -> None:
    health = "privacy-act-1988:C2026C00227:s6/1/health-service"
    rule = "privacy-act-1988:C2026C00227:s6d/4/b"
    assert route_of([(health,), (rule,)]) == "s 6D(4)"
    assert route_of([(rule,), (health,)]) == "s 6D(4)"
    assert route_of([("privacy-act-1988:C2026C00227:s6d/4/a/i",)]) == "s 6D(4)"
    assert route_of([(health,)]) == NO_ROUTE and route_of([]) == NO_ROUTE
    assert route_of([("not a clause",)]) == NO_ROUTE


# ---- each fact for the period the law reads ----------------------------------------------


async def test_a_state_is_asked_on_the_date_and_a_year_for_that_year() -> None:
    q, _ = await asked()
    state = q.item("legal_form")
    assert state is not None and [(s.key, s.label) for s in state.slots] == [(STATE, f"On {AS_OF}")]
    turnover = q.item("annual_turnover_aud")
    assert turnover is not None and turnover.answer_kind is AnswerKind.NUMBER
    # s6D(1) reads the previous year, s6D(2) the current one, and s6D(4)(a), with
    # no start date recorded, every year ended since 21 December 2001: since #338
    # (ADR-0058) none of them is a part year left open, so each is asked
    assert [(s.key, s.label, s.period) for s in turnover.slots[:2]] == [
        ("fy2025", "FY2025-26", Period.financial_year(2025)),
        ("fy2026", "FY2026-27", Period.financial_year(2026)),
    ]
    assert [s.key for s in turnover.slots[2:]] == [f"fy{y}" for y in range(2001, 2025)]
    assert all(s.status is SlotStatus.OPEN for s in q.items[0].slots)


async def test_s6d4a_asks_each_financial_year_since_the_business_started_that_lacks_a_figure() -> (
    None
):
    """A "never over $3M" shortcut can't stand in for the years (#123): each is
    asked, the one the business started in too, its figure worked out by
    s6DA(2)'s formula, which the corpus holds since #338 (ADR-0058)."""
    started = fact(1, "business_started_on", dt.date(2015, 3, 2), Period(dt.date(2015, 3, 2)))
    q, _ = await asked(started)
    turnover = q.item("annual_turnover_aud")
    assert turnover is not None
    keys = [s.key for s in turnover.slots]
    assert keys[0] == "fy2025"
    assert set(keys) >= {f"fy{y}" for y in range(2015, 2026)}
    labels = [s.label for s in turnover.slots]
    assert "FY2014-15" in labels and "FY2013-14" not in labels  # the start year, not before
    assert "FY2015-16" in labels and "FY2025-26" in labels
    assert "business_started_on" not in fields(q)  # known, so not asked again


async def test_a_figure_recorded_for_a_year_takes_that_year_off_the_list() -> None:
    started = fact(1, "business_started_on", dt.date(2015, 3, 2), Period(dt.date(2015, 3, 2)))
    q, _ = await asked(started)
    turnover = q.item("annual_turnover_aud")
    assert turnover is not None
    before = {s.label for s in turnover.slots}
    figure = fact(2, "annual_turnover_aud", 1_000_000, Period.financial_year(2018))
    after_q, _ = await asked(started, figure)
    after = after_q.item("annual_turnover_aud")
    assert after is not None
    assert {s.label for s in after.slots} == before - {"FY2018-19"}


async def test_history_never_reaches_before_s6d_commenced() -> None:
    old = fact(1, "business_started_on", dt.date(1990, 1, 1), Period(dt.date(1990, 1, 1)))
    q, _ = await asked(old)
    turnover = q.item("annual_turnover_aud")
    assert turnover is not None
    labels = [s.label for s in turnover.slots]
    assert "FY2000-01" not in labels and "FY2001-02" in labels  # s6D commenced 21 Dec 2001


async def test_a_start_date_is_asked_beside_the_years_when_it_is_unknown() -> None:
    q, _ = await asked()
    started = q.item("business_started_on")
    assert started is not None and started.answer_kind is AnswerKind.DATE
    assert [s.key for s in started.slots] == [STATE]
    assert {a.condition_id for a in started.asked} >= {"COND-PA-CARRIED-ON-LAST-YEAR"}


# ---- a disagreement is flagged, never resolved -------------------------------------------


async def test_two_current_answers_that_disagree_are_flagged_and_neither_is_chosen() -> None:
    first, second = fact(1, "legal_form", "trust"), fact(2, "legal_form", "partnership")
    q, coverage = await asked(first, second)
    item = q.item("legal_form")
    assert item is not None
    (slot,) = item.slots
    assert slot.status is SlotStatus.DISAGREE and not slot.status.takes_answer
    assert {e.evidence_id for e in slot.current} == {"e1", "e2"}  # both stay current
    # Track A reads it as unknown, with the same flag
    assert any(
        question.conflict for question in coverage.questions if question.field == "legal_form"
    )
    (record,) = [r for r in on_record([first, second]) if r.field == "legal_form"]
    assert record.disagree and {e.evidence_id for e in record.current} == {"e1", "e2"}


async def test_answers_that_agree_are_not_a_disagreement() -> None:
    q, coverage = await asked(fact(1, "legal_form", "trust"), fact(2, "legal_form", "trust"))
    assert q.item("legal_form") is None  # settled: not asked
    assert not any(r.disagree for r in on_record([fact(1, "legal_form", "trust")]))
    assert not any(
        r.disagree
        for r in on_record([fact(1, "legal_form", "trust"), fact(2, "legal_form", "trust")])
    )
    assert coverage.with_outcome(Outcome.UNASSESSABLE)


async def test_an_int_and_a_float_are_different_answers_so_numbers_are_normalised_upstream() -> (
    None
):
    year = Period.financial_year(2025)
    q, _ = await asked(
        fact(1, "annual_turnover_aud", 5_000_000, year),
        fact(2, "annual_turnover_aud", 5_000_000.5, year),
    )
    item = q.item("annual_turnover_aud")
    assert item is not None
    slot = item.slot("fy2025")
    assert slot is not None and slot.status is SlotStatus.DISAGREE


async def test_a_figure_for_part_of_a_year_is_flagged_as_partial() -> None:
    part = Period(dt.date(2025, 9, 1), dt.date(2026, 7, 1))
    q, _ = await asked(fact(1, "annual_turnover_aud", 100_000, part))
    item = q.item("annual_turnover_aud")
    assert item is not None
    slot = item.slot("fy2025")
    assert slot is not None and slot.status is SlotStatus.PARTIAL


async def test_an_answer_track_a_cannot_read_is_flagged_as_unreadable() -> None:
    q, _ = await asked(fact(1, "annual_turnover_aud", "plenty", Period.financial_year(2025)))
    item = q.item("annual_turnover_aud")
    assert item is not None
    slot = item.slot("fy2025")
    assert slot is not None and slot.status is SlotStatus.UNREADABLE
    assert not slot.status.takes_answer


# ---- a correction supersedes ---------------------------------------------------------------


async def test_a_correction_supersedes_and_the_old_answer_stays_as_history() -> None:
    old = fact(1, "legal_form", "trust")
    new = fact(2, "legal_form", "partnership", supersedes="e1")
    wrong = fact(3, "legal_form", "individual")
    q, _ = await asked(old, wrong, new)  # old is superseded; wrong disagrees with new
    item = q.item("legal_form")
    assert item is not None
    (slot,) = item.slots
    assert {e.evidence_id for e in slot.current} == {"e2", "e3"}
    assert [e.evidence_id for e in slot.superseded] == ["e1"]
    # once the disagreement is corrected too, the question is settled
    fixed = fact(4, "legal_form", "partnership", supersedes="e3")
    settled, _ = await asked(old, wrong, new, fixed)
    assert settled.item("legal_form") is None
    (record,) = [r for r in on_record([old, wrong, new, fixed]) if r.field == "legal_form"]
    assert {e.evidence_id for e in record.current} == {"e2", "e4"}
    assert {e.evidence_id for e in record.superseded} == {"e1", "e3"}
    assert not record.disagree


async def test_a_claim_from_a_document_neither_settles_nor_is_asked_about() -> None:
    claim = fact(1, "legal_form", "trust", provenance=EvidenceProvenance.INFERRED)
    q, _ = await asked(claim)
    item = q.item("legal_form")
    assert item is not None and item.slots[0].status is SlotStatus.OPEN
    assert on_record([claim]) == ()


# ---- what nothing here can settle ----------------------------------------------------------


async def test_an_obligation_no_answer_settles_is_listed_apart() -> None:
    obligation = ObligationCoverage(
        obligation_id="OB-X",
        instrument_id="x",
        summary="no compilation is loaded",
        clause_ids=(),
        outcome=Outcome.UNASSESSABLE,
        explanation=Explanation("OB-X", "grounding", Truth.UNKNOWN, "no text", ()),
        reason="No compilation is in force.",
    )
    coverage = Coverage(TENANT, AS_OF, (obligation,))
    q = build(coverage, ())
    assert q.items == () and q.unanswerable == (obligation,)
    assert dict(q.counts)[Outcome.UNASSESSABLE] == 1


async def test_a_question_whose_period_is_not_one_the_law_reads_is_not_offered() -> None:
    odd = Question(
        condition_id="C",
        field="x",
        period="whenever",
        question="?",
        unassessable_reason="why",
        clause_ids=(),
    )
    explanation = Explanation("OB-X", "leaf", Truth.UNKNOWN, "d", (), question=odd)
    obligation = ObligationCoverage(
        "OB-X", "x", "s", (), Outcome.UNASSESSABLE, explanation, "Depends on"
    )
    q = build(Coverage(TENANT, AS_OF, (obligation,)), ())
    assert q.items == () and q.unanswerable == (obligation,)


# ---- the small rules -----------------------------------------------------------------------


def _legacy(field: str, period: str) -> ObligationCoverage:
    """A question as the ledger kept it before questions carried `answer_kind`."""
    old = Question(
        condition_id="C",
        field=field,
        period=period,
        question="?",
        unassessable_reason="why",
        clause_ids=(),
    )
    assert old.answer_kind is None
    explanation = Explanation("OB-X", "leaf", Truth.UNKNOWN, "d", (), question=old)
    return ObligationCoverage("OB-X", "x", "s", (), Outcome.UNASSESSABLE, explanation, "Depends on")


def test_a_legacy_question_takes_its_kind_from_the_answers_already_recorded() -> None:
    other_year = fact(1, "annual_turnover_aud", 1_000_000, Period.financial_year(2023))
    obligation = _legacy("annual_turnover_aud", "FY2025-26")
    q = build(Coverage(TENANT, AS_OF, (obligation,)), (other_year,))
    item = q.item("annual_turnover_aud")
    assert item is not None and item.answer_kind is AnswerKind.NUMBER


def test_a_legacy_question_with_no_known_kind_is_not_drawn_as_text() -> None:
    """No recorded answer, or recorded answers of two kinds: the shape isn't
    known, so no input is offered and the obligation is listed as one no answer
    here settles."""
    obligation = _legacy("annual_turnover_aud", "FY2025-26")
    mixed = (
        fact(1, "annual_turnover_aud", "plenty", Period.financial_year(2023)),
        fact(2, "annual_turnover_aud", 5, Period.financial_year(2024)),
    )
    for evidence in ((), mixed):
        q = build(Coverage(TENANT, AS_OF, (obligation,)), evidence)
        assert q.items == () and q.unanswerable == (obligation,)


def test_a_financial_year_label_names_a_year_only_if_it_is_one() -> None:
    for start in (1901, 1999, 2000, 2025, 2099):
        year = Period.financial_year(start)
        assert Period.of_financial_year_label(year.label) == year
        assert year_of_key(year_key(year)) == year
    for label in (
        "FY2025-27",
        "FY2025-25",
        "FY25-26",
        "fy2025-26",
        "FY2025-26 ",
        "on 2026-10-12",
        "",
    ):
        assert Period.of_financial_year_label(label) is None
    assert year_of_key(STATE) is None and year_of_key("fy20250") is None


def test_an_answer_is_recorded_for_the_period_it_is_true_for() -> None:
    year = Period.financial_year(2025)
    today = dt.date(2026, 10, 12)
    assert answer_period(AnswerKind.NUMBER, 1, year=year, holds_from=None) == year
    # a date-valued state holds from the date itself
    assert answer_period(
        AnswerKind.DATE, dt.date(2015, 3, 2), year=None, holds_from=None
    ) == Period(dt.date(2015, 3, 2))
    # any other state holds from the date the admin gives
    assert answer_period(AnswerKind.BOOLEAN, True, year=None, holds_from=today) == Period(today)
    with pytest.raises(ValueError, match="holds from"):
        answer_period(AnswerKind.BOOLEAN, True, year=None, holds_from=None)


def test_a_recorded_answer_says_what_kind_of_fact_it_is() -> None:
    assert answer_kind_of(True) is AnswerKind.BOOLEAN
    assert answer_kind_of(0) is AnswerKind.NUMBER and answer_kind_of(1.5) is AnswerKind.NUMBER
    assert answer_kind_of(dt.date(2020, 1, 1)) is AnswerKind.DATE
    assert answer_kind_of(("AU",)) is AnswerKind.LIST and answer_kind_of("x") is AnswerKind.TEXT


def test_a_question_keeps_what_it_asks_for_through_the_ledgers_json() -> None:
    question = Question(
        condition_id="C",
        field="legal_form",
        period="on 2026-10-12",
        question="?",
        unassessable_reason="r",
        clause_ids=("a:b:c",),
        answer_kind=AnswerKind.TEXT,
        options=("trust", "individual"),
    )
    assert question_of_json(question_json(question)) == question
    # a row recorded before the fields existed reads back without them
    old = {k: v for k, v in question_json(question).items() if k not in {"answer_kind", "options"}}
    assert question_of_json(old).answer_kind is None and question_of_json(old).options == ()
