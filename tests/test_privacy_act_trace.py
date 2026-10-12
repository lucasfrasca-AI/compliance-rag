""" "Does the Privacy Act apply to us?" end to end (issue #118, the #93 worked
example), on the drafted mapping and labels (`corpus/mappings/
privacy-act-1988.yaml`, `corpus/golden/applicability.yaml`) through Track A
over a real Neo4j — the same path the live trace takes on Aura.

- Acme at $4M: applies, with the citation chain s15 -> s 6(1) "app-entity" ->
  s 6C(1)(b) -> s 6D(1).
- Acme at $2.5M: possibly. Under the threshold (s 6D(1), cited: it is why the
  answer isn't "no"), with all four routes back under the APPs unresolved and
  named — s 6D(4), s 6D(9), s 6E, s 6EA — each with its clauses, and every
  question that would settle them asked.
- Acme at $2.5M with the questionnaire answered: s 6D(4)(b) and (c) remain,
  asked rather than inferred from the policies that speak to them.
"""

from __future__ import annotations

from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.assessment.coverage import Outcome, Truth
from compliance_rag.assessment.evidence import Facts
from compliance_rag.eval.applicability import load_applicability_set
from compliance_rag.graph.mappings import load_mapping_set
from tests.test_applicability_gate import CORPUS_MAPPINGS, graph_for
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
    assert len(app11.questions) == 15


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
