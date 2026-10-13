""" "Does the Privacy Act apply to us?" end to end (issue #118, the #93 worked
example), on the drafted mapping and labels (`corpus/mappings/
privacy-act-1988.yaml`, `corpus/golden/applicability.yaml`) through Track A
over a real Neo4j — the same path the live trace takes on Aura.

- Acme at $4M: applies, with the citation chain s15 -> s 6(1) "app-entity" ->
  s 6C(1)(b) -> s 6D(1).
- Acme at $2.5M: possibly. Under the threshold (s 6D(1), cited: it is why the
  answer isn't "no"), with all four routes back under the APPs unresolved and
  named — s 6D(4), s 6D(9), s 6E as a whole, s 6EA — each with its clauses,
  and every question that would settle them asked.
- Acme at $2.5M with the questionnaire answered: s 6D(4)(b) and (c) remain,
  asked rather than inferred from the policies that speak to them.
- Acme at $2.5M, once over $3M (GA-05): the APPs apply because of one dated
  fact, and each way of breaking what s 6D(4)(a) reads breaks the answer.
"""

from __future__ import annotations

import datetime as dt

import pytest

from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.assessment.coverage import ObligationCoverage, Outcome, Truth
from compliance_rag.assessment.evidence import Facts
from compliance_rag.eval.applicability import (
    ApplicabilityCase,
    GoldenFact,
    load_applicability_set,
)
from compliance_rag.graph.mappings import load_mapping_set
from tests.test_applicability_gate import CORPUS_MAPPINGS, fake_for, graph_for
from tests.test_neo4j_graph import graph as graph
from tests.test_neo4j_graph import needs_neo4j

P = "privacy-act-1988:C2026C00227:"
APP11 = "OB-PA-APP11-1"
ROUTES = {
    "COND-PA-S6D4-EXCEPTION": f"{P}s6d/4",
    "COND-PA-RELATED-TO-NON-SMALL": f"{P}s6d/9",
    "COND-PA-S6E-TREATED-AS-ORGANISATION": f"{P}s6e/1",
    "COND-PA-S6EA-OPTED-IN": f"{P}s6ea/1",
}


async def _coverage(graph: Neo4jGraph, case_id: str):
    labels = load_applicability_set()
    store = await graph_for(graph, load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), labels)
    case = next(c for c in labels.cases if c.id == case_id)
    return await store.coverage(
        Facts.of(case.evidence()), tenant_id=case.tenant_id, as_of=case.as_of
    )


@needs_neo4j
async def test_at_4m_the_act_applies_with_its_chain(graph: Neo4jGraph) -> None:
    app11 = (await _coverage(graph, "GA-01")).obligation(APP11)
    assert app11.outcome is Outcome.APPLICABLE
    chain = app11.citations
    for clause in ("sch1/app11/1", "s15", "s6/1/app-entity", "s6c/1/b", "s6d/1", "s6da/1"):
        assert f"{P}{clause}" in chain
    # Short-circuit: the threshold decided it, so no exception was consulted.
    assert app11.explanation.find("COND-PA-S6D4-EXCEPTION") is None


@needs_neo4j
async def test_at_2_5m_possibly_with_four_routes_named_and_cited(graph: Neo4jGraph) -> None:
    app11 = (await _coverage(graph, "GA-02")).obligation(APP11)
    assert app11.outcome is Outcome.UNASSESSABLE
    for route, clause in ROUTES.items():
        node = app11.explanation.find(route)
        assert node is not None and node.truth is Truth.UNKNOWN, route
        assert clause in node.clause_ids and clause in app11.citations, route
    met = app11.explanation.find("COND-PA-TURNOVER-PREVIOUS-YEAR-UNDER")
    assert met is not None and met.truth is Truth.TRUE
    assert f"{P}s6d/1" in app11.citations  # why it's "possibly", not "no"
    assert len(app11.questions) == 11


@needs_neo4j
async def test_answered_only_s6d4_b_and_c_remain_as_questions(graph: Neo4jGraph) -> None:
    app11 = (await _coverage(graph, "GA-03")).obligation(APP11)
    assert app11.outcome is Outcome.UNASSESSABLE
    asked = {q.condition_id: q.clause_ids for q in app11.questions}
    assert set(asked) == {
        "COND-PA-PROVIDES-HEALTH-SERVICE",
        "COND-PA-HOLDS-HEALTH-INFORMATION",
        "COND-PA-S6D4C-DISCLOSES-FOR-BENEFIT",
    }
    assert f"{P}s6d/4/b" in asked["COND-PA-PROVIDES-HEALTH-SERVICE"]
    assert f"{P}s6d/4/c" in asked["COND-PA-S6D4C-DISCLOSES-FOR-BENEFIT"]


# ---- GA-05: s6D(4)(a) is decided by the turnover history ---------------------------
#
# On the fake, which runs the same evaluator as Track A over Neo4j (GA-05 itself
# is checked on both by the gate). As of 12 October 2026 the ended financial
# years since Acme started (2 March 2015) are FY2014-15 to FY2025-26.

S6D4A = "COND-PA-S6D4A-EVER-OVER-THRESHOLD"
S6D1 = "COND-PA-TURNOVER-PREVIOUS-YEAR-UNDER"


def _turnover(financial_year: int, aud: int) -> GoldenFact:
    return GoldenFact(field="annual_turnover_aud", value=aud, financial_year=financial_year)


def _ga05(
    *, turnover: list[GoldenFact] | None = None, started: dt.date | None = None
) -> ApplicabilityCase:
    """GA-05's facts, with its turnover figures or its start date replaced."""
    ga05 = next(c for c in load_applicability_set().cases if c.id == "GA-05")
    facts = [f for f in ga05.facts if turnover is None or f.field != "annual_turnover_aud"]
    if started is not None:
        facts = [
            GoldenFact(field=f.field, value=started, since=started)
            if f.field == "business_started_on"
            else f
            for f in facts
        ]
    return ga05.model_copy(update={"facts": tuple(facts + (turnover or []))})


async def _app11(case: ApplicabilityCase) -> tuple[ObligationCoverage, set[str]]:
    labels = load_applicability_set()
    store = fake_for(load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), labels)
    coverage = await store.coverage(
        Facts.of(case.evidence()), tenant_id=case.tenant_id, as_of=case.as_of
    )
    app11 = coverage.obligation(APP11)
    return app11, {q.condition_id for q in app11.questions}


async def test_ga05_applies_because_of_the_fy2023_24_figure() -> None:
    app11, asked = await _app11(_ga05())
    assert app11.outcome is Outcome.APPLICABLE and not asked
    leaf = app11.explanation.find(S6D4A)
    assert leaf is not None and leaf.truth is Truth.TRUE
    assert f"{P}s6d/4/a" in app11.citations


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(_ga05(turnover=[_turnover(2025, 2_500_000)]), id="no-fy2023-24-figure"),
        pytest.param(
            _ga05(turnover=[_turnover(2025, 2_500_000), _turnover(2026, 3_200_000)]),
            id="over-3m-only-in-a-year-not-yet-ended",
        ),
        pytest.param(_ga05(started=dt.date(2024, 8, 1)), id="fy2023-24-ended-before-it-started"),
        pytest.param(
            _ga05(
                turnover=[_turnover(2000, 3_200_000), _turnover(2025, 2_500_000)],
                started=dt.date(1999, 1, 1),
            ),
            id="over-3m-only-before-s6d-commenced",
        ),
    ],
)
async def test_a_year_s6d4a_does_not_count_leaves_it_asked(case: ApplicabilityCase) -> None:
    app11, asked = await _app11(case)
    assert app11.outcome is Outcome.UNASSESSABLE
    assert S6D4A in asked


async def test_every_ended_year_known_and_under_settles_s6d4a_false() -> None:
    every_year = [_turnover(fy, 2_000_000) for fy in range(2014, 2026)]
    app11, asked = await _app11(_ga05(turnover=every_year))
    assert S6D4A not in asked and S6D1 not in asked
    assert app11.outcome is Outcome.UNASSESSABLE  # the other routes are still open


async def test_s6d1_reads_the_previous_financial_year_only() -> None:
    # FY2023-24 under the threshold, nothing for FY2025-26: s6D(1) can't use it.
    _, asked = await _app11(_ga05(turnover=[_turnover(2023, 2_900_000)]))
    assert S6D1 in asked
